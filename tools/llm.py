"""
LLM access for TenderLens: one pinned model, one fallback.

Groq's gpt-oss-120b answers every agent call ("strong"); gpt-oss-20b the
label map's quick calls ("fast"). Gemini is used only when Groq fails or its
daily quota is spent. Both speak the OpenAI chat-completions format (Gemini
through its compatibility endpoint), so there is one code path and no SDK to
install: urllib only, nothing for Windows Application Control to block.

Keys go in .env (never committed):

    GROQ_API_KEY=gsk_...      (free: console.groq.com)
    GEMINI_API_KEY=...        (optional fallback, small free quota)

With no key at all, ask() raises NoProvider and the caller falls back to its
rules: the pipeline must never silently stop producing findings.

Groq's free tier is about 6,000 tokens PER MINUTE and 200,000 per day - the
real constraint on design: one question about forty candidates is far
cheaper than forty questions about one. Callers batch; 429s are retried with
the delay the server asks for.

Every answer is cached in reports/llm_cache.json, keyed by the model and a
hash of the prompt, so a re-run costs nothing and gives the same verdicts.

    python tenderlens.py llm           # which models, and one test call
    python tenderlens.py llm --usage   # tokens spent, by caller
"""


import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CACHE_PATH = "reports/llm_cache.json"
USAGE_PATH = "reports/llm_usage.csv"
# Every failed call, one row: when, which provider and model, what went wrong
# and the server's own message - without it, a silent 46-minute gap in an
# agent run could not be explained.
ERRORS_PATH = "reports/llm_errors.csv"

# Tokens per minute each provider may take, a little under its free tier: a
# 429 costs the Retry-After wait plus a round trip, and the retry spends the
# tokens again.
BUDGETS = {"groq": 5500, "gemini": 5500}
_windows = {}         # provider -> [(unix_time, tokens)]
_last_call = {}       # provider -> unix_time
_down_until = {}      # provider -> unix_time; a provider that errored rests
DOWN_FOR = 60         # seconds
DAY_OUT = 6 * 3600    # seconds a provider is skipped after "quota per day"
# Groq first; Gemini only when Groq fails. Sharing every call
# between the two (user decision 2026-10-19) made verdicts differ between
# runs - two models answer differently. TENDERLENS_SHARE=1 restores sharing:
# twice the calls a minute, for a first run of a whole category.
SHARE = os.environ.get("TENDERLENS_SHARE") == "1"
# Native tool calling: the request carries a schema per tool and
# the model's tool call comes back as {"thought", "action", "args"} - the
# shape the agents read, so a JSON reply still works. Measured before it:
# about half the agent steps were lost on tools that don't exist or wrong
# argument names. A provider that rejects tools is asked again without them.
# "off" restores the prompt-only behaviour (and the old cache keys).
NATIVE_TOOLS = os.environ.get("TENDERLENS_NATIVE_TOOLS", "on")
_no_tools = set()     # providers that rejected a tools request this run

# THE MODELS, pinned: the same model gives the same verdicts,
# and every measurement in MEASUREMENT_LOG was made with these. They are the
# models the cache was filled with, so cached answers keep working. A model
# the provider has retired (HTTP 400/404) is replaced, for this run, by the
# first match of PREFER_* in the provider's own list - with a warning,
# because verdicts may change.
PROVIDERS = [
    {"name": "groq", "env": "GROQ_API_KEY",
     "base": "https://api.groq.com/openai/v1",
     "fast": "openai/gpt-oss-20b", "strong": "openai/gpt-oss-120b"},
    {"name": "gemini", "env": "GEMINI_API_KEY",
     "base": "https://generativelanguage.googleapis.com/v1beta/openai",
     "fast": "models/gemini-2.5-flash",
     "strong": "models/gemini-3-flash-preview"},
]
PREFER_FAST = [r"gpt-oss-20b", r"llama.*8b.*instant",
               r"gemini-[3-9][\w.-]*flash-lite", r"flash"]
# Gemini Pro left the free tier in 2026: the strong Gemini is a non-lite Flash.
PREFER_STRONG = [r"gpt-oss-120b", r"llama.*70b",
                 r"gemini-[3-9][\d.]*-flash(?!-lite)", r"flash(?!-lite)"]
# Never pick these for text: speech, embedding, vision-only, moderation.
EXCLUDE_MODELS = re.compile(
    r"whisper|tts|embed|guard|moderation|vision|image|audio|rerank|ocr",
    re.I)
_replaced = {}        # provider -> {"fast", "strong"} found this run


def _auth(provider):
    return "Bearer " + os.environ[provider["env"]]


def tool(name, description, properties=None, required=None):
    """One tool for ask(tools=...): a name, what it does, its arguments as
    JSON-schema properties."""
    return {"name": name, "description": description,
            "parameters": {"type": "object",
                           "properties": properties or {},
                           "required": list(required or [])}}


class NoProvider(Exception):
    """No API key is configured, so the caller must use its own rules."""


def load_dotenv(path=".env"):
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip().lstrip("export ").strip()
                v = v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
    except OSError:
        pass


load_dotenv()


# ------------------------------------------------------------------- caching
def _load_cache(path=CACHE_PATH):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def _save_cache(cache, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _key(model, system, user):
    h = hashlib.sha256()
    h.update((model + "\x00" + system + "\x00" + user).encode("utf-8"))
    return h.hexdigest()[:32]


# -------------------------------------------------------------- the one call
def available():
    """Providers that have a key set, in fallback order."""
    return [p for p in PROVIDERS if os.environ.get(p["env"])]


def models_for(provider, refresh=False, verbose=False):
    """(fast, strong) model IDs for this provider: the pinned ones, or with
    `refresh` (the pinned one was rejected) the provider's own best match."""
    name = provider["name"]
    if refresh:
        try:
            req = urllib.request.Request(
                provider["base"] + "/models",
                headers={"Authorization": _auth(provider),
                         "User-Agent": "TenderLens/0.1"})
            with urllib.request.urlopen(req, timeout=30) as r:
                ids = sorted(m.get("id", "") for m in json.load(r)
                             .get("data", []) if m.get("id")
                             and not EXCLUDE_MODELS.search(m["id"]))
        except Exception as e:
            if verbose:
                print(f"    [{name}] could not list models: "
                      f"{type(e).__name__}")
            ids = []
        pick = lambda prefer: next((i for pat in prefer for i in ids
                                    if re.search(pat, i, re.I)), None)
        fast, strong = pick(PREFER_FAST), pick(PREFER_STRONG)
        if fast or strong:
            _replaced[name] = {"fast": fast or strong,
                               "strong": strong or fast}
            print(f"    WARNING [{name}] pinned model rejected; using "
                  f"{_replaced[name]} for this run - verdicts may differ "
                  f"from the measurements")
    m = _replaced.get(name) or provider
    return m["fast"], m["strong"]


def _post(provider, model, system, user, max_tokens, temperature, timeout,
          tools=None):
    req_body = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        req_body["tools"] = [{"type": "function", "function": t}
                             for t in tools]
    body = json.dumps(req_body).encode("utf-8")
    req = urllib.request.Request(
        provider["base"] + "/chat/completions", data=body,
        headers={"Authorization": _auth(provider),
                 "Content-Type": "application/json",
                 "User-Agent": "TenderLens/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    # Every provider here speaks the OpenAI schema, so usage comes back the
    # same way - but not all of them send it, so the caller must cope with {}.
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    if tools and msg.get("tool_calls"):
        return tool_answer(msg), data.get("usage") or {}
    answer = msg.get("content") or ""
    if not answer.strip():
        answer = _slip_from_message(msg)
    if not answer.strip():
        # kept for the error log: why was the answer empty?
        _last_empty.update(finish=choice.get("finish_reason"),
                           keys=sorted(k for k, v in msg.items() if v))
    return answer, data.get("usage") or {}


_last_empty = {}


def tool_answer(msg):
    """A native tool call as the JSON the agents read:
    {"thought", "action", "args"}. One action per step: the first call."""
    call = (msg.get("tool_calls") or [{}])[0]
    fn = call.get("function") or {}
    args = fn.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except ValueError:
            args = {}
    return json.dumps({"thought": (msg.get("content") or "")[:300],
                       "action": fn.get("name") or "",
                       "args": args if isinstance(args, dict) else {}},
                      ensure_ascii=False)


def _slip_from_message(msg):
    """An empty answer whose content went elsewhere (gpt-oss on Groq,
    2026-10-22): a native tool call, or JSON left in the reasoning text."""
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        if fn.get("name"):
            return json.dumps({"name": fn["name"], "arguments": args})
    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
    found = ""
    for m in re.finditer(r"\{[^{}]*(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}[^{}]*)*\}",
                         reasoning):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict) and "action" in obj:
            found = m.group(0)          # the last one is the decision
    return found


def _estimate(text):
    """Rough token count for a provider that sends no usage block.

    ~4 characters per token. Only used to keep the throttle honest and to
    stop the log having holes; never reported as if it were measured.
    """
    return max(1, len(text) // 4)


def _wait_needed(name, est, now=None):
    """Seconds before this provider can take a call of `est` tokens."""
    now = time.time() if now is None else now
    win = _windows.setdefault(name, [])
    win[:] = [(t, n) for t, n in win if now - t < 60]
    budget = BUDGETS.get(name, 5500)
    wait = 0.0
    spent = sum(n for _t, n in win)
    if spent + est > budget and win:
        wait = 60 - (now - win[0][0]) + 1
    return max(wait, 0.0)


def _order(provs, est):
    """Fallback order; with SHARE, the provider that can take this call
    soonest first."""
    if not SHARE or len(provs) < 2:
        return provs
    now = time.time()
    return sorted(provs, key=lambda p: (_down_until.get(p["name"], 0) > now,
                                        _wait_needed(p["name"], est, now),
                                        _last_call.get(p["name"], 0)))


def _throttle_p(name, est, verbose=False):
    """Per-provider throttle: sleep until this provider has room."""
    wait = _wait_needed(name, est)
    if wait > 0:
        if verbose:
            print(f"    [{name} throttle] waiting {wait:.0f}s")
        time.sleep(min(wait, 65))
    now = time.time()
    _windows.setdefault(name, []).append((now, est))
    _last_call[name] = now


def _http_body(exc):
    """The error body of an HTTPError, read once. Never raises."""
    try:
        return exc.read(4000).decode("utf-8", "replace")
    except Exception:
        return ""


def _tool_slip(body):
    """The tool call a model emitted by mistake, as JSON text, else None."""
    if "tool_use_failed" not in body:
        return None
    try:
        gen = json.loads(body)["error"].get("failed_generation") or ""
        json.loads(gen)
        return gen
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _log_error(tag, provider, model, error, exc=None, detail=None):
    """Append one failed call to reports/llm_errors.csv. Never raises."""
    if detail is None:
        detail = ""
        try:
            if isinstance(exc, urllib.error.HTTPError):
                detail = _http_body(exc)
            elif exc is not None:
                detail = str(exc)
        except Exception:
            pass
    row = [time.strftime("%Y-%m-%dT%H:%M:%S"), tag or "", provider, model,
           error, " ".join(detail.split())[:300]]
    try:
        os.makedirs(os.path.dirname(ERRORS_PATH) or ".", exist_ok=True)
        new = not os.path.exists(ERRORS_PATH)
        with open(ERRORS_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "tag", "provider", "model", "error",
                            "detail"])
            w.writerow(row)
    except OSError:
        pass


USAGE_COLS = ["ts", "tag", "provider", "model", "cached",
              "prompt_tokens", "completion_tokens", "total_tokens",
              "estimated"]


def _log_usage(tag, provider, model, usage, cached, est_total):
    """Append one row per call to reports/llm_usage.csv.

    Cache hits are logged too, with zero tokens. That is the point: the file
    has to show what the cache SAVED, otherwise there is no way to tell a
    cheap pipeline from one that simply did not run.
    """
    pt = usage.get("prompt_tokens")
    ct = usage.get("completion_tokens")
    tt = usage.get("total_tokens")
    estimated = 0
    if tt is None:
        tt = 0 if cached else est_total
        estimated = 0 if cached else 1
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tag": tag or "", "provider": provider, "model": model,
        "cached": int(bool(cached)),
        "prompt_tokens": pt if pt is not None else "",
        "completion_tokens": ct if ct is not None else "",
        "total_tokens": tt, "estimated": estimated,
    }
    try:
        os.makedirs(os.path.dirname(USAGE_PATH) or ".", exist_ok=True)
        new = not os.path.exists(USAGE_PATH)
        with open(USAGE_PATH, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=USAGE_COLS)
            if new:
                w.writeheader()
            w.writerow(row)
    except OSError:
        pass      # never let bookkeeping break a pipeline run


def usage_summary(path=USAGE_PATH):
    """(calls, cached_calls, total_tokens, by_tag) from the log."""
    if not os.path.exists(path):
        return 0, 0, 0, {}
    calls = cached = total = 0
    by_tag = {}
    with open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            calls += 1
            c = r.get("cached") == "1"
            cached += int(c)
            try:
                t = int(r.get("total_tokens") or 0)
            except ValueError:
                t = 0
            total += t
            d = by_tag.setdefault(r.get("tag", ""), {"calls": 0, "tok": 0,
                                                     "cached": 0})
            d["calls"] += 1
            d["tok"] += t
            d["cached"] += int(c)
    return calls, cached, total, by_tag


def usage_rows(path=USAGE_PATH):
    """Rows of the token log, in order (calls logged by any tag)."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.DictReader(line.replace("\0", "") for line in f))


def agent_tokens(rows):
    """Tokens the agents used in these rows. Count by position in the log
    (usage_rows()[start:]), not by time: the log can be written on another
    clock."""
    n = 0
    for r in rows:
        if (r.get("tag") or "").endswith("_agent"):
            try:
                n += int(float(r.get("total_tokens") or 0))
            except ValueError:
                pass
    return n


def ask(system, user, strong=False, max_tokens=1500, temperature=0.0,
        timeout=60, retries=2, verbose=False, tag="", tools=None):
    """Ask the first working provider. Returns the raw text answer.

    tools: [llm.tool(...)] for native tool calling; a tool call comes back
    as JSON text {"thought", "action", "args"}.

    Raises NotCached offline (TENDERLENS_OFFLINE=1) when the answer is not
    in the cache, NoProvider if no key is set, or RuntimeError if every
    provider failed - the caller decides what to do, because a silent empty
    answer would look like "no findings".
    """
    if NATIVE_TOOLS != "on":
        tools = None
    # the tool list is part of the question: same prompt, other tools,
    # other answer
    sys_key = system if not tools else (
        system + "\x01" + json.dumps(tools, sort_keys=True))

    cache = _load_cache()
    errors = []
    # A saved answer is reused whichever pinned model gave it, key or no key:
    # a replay needs no account, and an answer already paid is never re-asked.
    for p in PROVIDERS:
        fast, strg = models_for(p, verbose=verbose)
        model = (strg if strong else fast)
        if not model:
            continue
        ck = _key(model, sys_key, user)
        # An empty cached answer is a truncated reasoning model, not a result.
        # Caching one poisoned the cache: the next run returned "" instantly
        # and never retried. Ignore them, and they get overwritten below.
        if ck in cache and cache[ck].get("answer", "").strip():
            _log_usage(tag, p["name"], model, {}, True, 0)
            return cache[ck]["answer"]
    if offline():
        raise NotCached(f"not in the cache (offline replay, {tag or 'llm'})")
    provs = available()
    if not provs:
        raise NoProvider("set GROQ_API_KEY (free at console.groq.com)")
    est = _estimate(system + user) + max_tokens // 4
    for p in _order(provs, est):
        if _down_until.get(p["name"], 0) - time.time() > DOWN_FOR:
            errors.append(f"{p['name']}: daily quota used")
            continue
        fast, strg = models_for(p, verbose=verbose)
        model = (strg if strong else fast)
        if not model:
            errors.append(f"{p['name']}: no usable model")
            continue
        ck = _key(model, sys_key, user)
        use_tools = tools if p["name"] not in _no_tools else None
        for attempt in range(retries + 1):
            try:
                _throttle_p(p["name"], est, verbose)
                answer, usage = _post(p, model, system, user, max_tokens,
                                      temperature, timeout, use_tools)
                _log_usage(tag, p["name"], model, usage, False,
                           _estimate(system + user + answer))
                # Reasoning models (gpt-oss, magistral, o-series) spend the
                # budget thinking and return an EMPTY message when it runs
                # out. That is not a refusal, it is truncation, so retry with
                # more room rather than treating it as "no answer".
                if not answer.strip() and attempt < retries:
                    max_tokens = min(max_tokens * 4, 16000)
                    if verbose:
                        print(f"    [{p['name']}] empty answer, retrying with "
                              f"max_tokens={max_tokens}")
                    continue
                if not answer.strip():
                    # Never cache or return an empty answer (it reads as
                    # "no JSON" upstream); try the next provider instead.
                    _log_error(tag, p["name"], model, "empty answer",
                               detail=json.dumps(_last_empty))
                    errors.append(f"{p['name']}: empty answer")
                    break
                cache[ck] = {"provider": p["name"], "model": model,
                             "answer": answer}
                _save_cache(cache)
                if verbose:
                    print(f"    [{p['name']}/{model}]")
                return answer
            except urllib.error.HTTPError as e:
                body = _http_body(e)
                _log_error(tag, p["name"], model, f"HTTP {e.code}", e, body)
                slip = _tool_slip(body) if e.code == 400 else None
                if (e.code == 400 and use_tools and not slip
                        and re.search(r"tool|function", body, re.I)):
                    # this provider/model does not take tools: ask again
                    # with the prompt alone (the JSON fallback)
                    _no_tools.add(p["name"])
                    use_tools = None
                    if verbose:
                        print(f"    [{p['name']}] no native tools, "
                              f"retrying with JSON")
                    continue
                if slip:
                    _log_usage(tag, p["name"], model, {}, False,
                               _estimate(system + user + slip))
                    cache[ck] = {"provider": p["name"], "model": model,
                                 "answer": slip}
                    _save_cache(cache)
                    if verbose:
                        print(f"    [{p['name']}] tool call recovered")
                    return slip
                if e.code == 429 and re.search(r"PerDay|per day", body, re.I):
                    # a daily quota will not come back in a minute: drop the
                    # provider for this run instead of retrying every call
                    errors.append(f"{p['name']}: daily quota used")
                    _down_until[p["name"]] = time.time() + DAY_OUT
                    break
                if e.code == 429 and attempt < retries:
                    # honour Retry-After when the server sends one
                    wait = e.headers.get("retry-after")
                    try:
                        wait = float(wait)
                    except (TypeError, ValueError):
                        wait = 8.0 * (attempt + 1)
                    if verbose:
                        print(f"    [{p['name']}] rate limited, "
                              f"waiting {wait:.0f}s")
                    time.sleep(min(wait, 60))
                    continue
                if e.code in (400, 404) and attempt < retries:
                    # the model ID was retired - rediscover and try once more
                    if verbose:
                        print(f"    [{p['name']}] {model} rejected "
                              f"({e.code}), refreshing catalogue")
                    fast, strg = models_for(p, refresh=True, verbose=verbose)
                    new = (strg if strong else fast)
                    if new and new != model:
                        model, ck = new, _key(new, sys_key, user)
                        continue
                errors.append(f"{p['name']}: HTTP {e.code}")
                _down_until[p["name"]] = time.time() + DOWN_FOR
                break
            except Exception as e:
                _log_error(tag, p["name"], model, type(e).__name__, e)
                errors.append(f"{p['name']}: {type(e).__name__}")
                _down_until[p["name"]] = time.time() + DOWN_FOR
                break
    msg = "every provider failed -> " + "; ".join(errors)
    if any(e.endswith("empty answer") for e in errors):
        raise EmptyAnswer(msg)
    raise RuntimeError(msg)


class NotCached(RuntimeError):
    """Offline, and this question was never answered: nothing is sent. The
    agents stop on it as on an outage, so a replay never pays silently."""


def offline():
    """TENDERLENS_OFFLINE=1: answers come from the cache only, never a
    provider (python tenderlens.py --offline <command>, demo --offline)."""
    return os.environ.get("TENDERLENS_OFFLINE") == "1"


class EmptyAnswer(RuntimeError):
    """A provider answered, but with nothing usable. The caller may try again
    with a different prompt; it is not an outage."""


RE_JSON = re.compile(r"\{.*\}|\[.*\]", re.S)


def ask_json(system, user, **kw):
    # kw passes straight through, so tag= reaches the usage log.
    """ask(), but parse the answer as JSON. Models wrap JSON in prose and in
    ``` fences often enough that stripping them is not optional."""
    raw = ask(system, user, **kw)
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except ValueError:
        m = RE_JSON.search(text)
        if not m:
            raise ValueError(f"no JSON in answer: {raw[:200]}")
        return json.loads(m.group(0))


# ---------------------------------------------------------------------- main
def main():
    provs = available()
    if not provs:
        print("No LLM provider configured.")
        print("Free key: https://console.groq.com  then put this in .env:")
        print("  GROQ_API_KEY=gsk_...")
        return 1

    if "--usage" in sys.argv:
        calls, cached, total, by_tag = usage_summary()
        print(f"{calls} calls logged, {cached} served from cache, "
              f"{total:,} tokens spent\n")
        print(f"  {'tag':24s} {'calls':>6} {'cached':>7} {'tokens':>9}")
        for t, d in sorted(by_tag.items(), key=lambda x: -x[1]["tok"]):
            print(f"  {(t or '(none)'):24s} {d['calls']:6d} "
                  f"{d['cached']:7d} {d['tok']:9,}")
        return 0

    print("configured, in fallback order"
          + (" (sharing calls: TENDERLENS_SHARE=1)" if SHARE else "") + ":")
    for p in provs:
        fast, strong = models_for(p)
        print(f"  {p['name']:10s} fast={fast}  strong={strong}")

    print("\ntesting the first one ...")
    try:
        r = ask_json(
            "You answer only with JSON.",
            'Is "Leica" a company? Answer {"company": true or false}.',
            verbose=True, tag="selftest")
        print("  ->", r)
    except Exception as e:
        print(f"  failed: {type(e).__name__}: {e}")
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
