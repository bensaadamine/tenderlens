"""
LLM access for TenderLens, with a provider fallback chain.

Groq first - it is free, fast, and allows commercial use. If its key is
missing, rate-limited or erroring, the next configured provider is tried.
Every provider here speaks the OpenAI chat-completions format, including
Gemini through its compatibility endpoint, so there is one code path and no
SDK to install: urllib only, which also means nothing for Windows Application
Control to block.

Set whichever keys you have as environment variables:

    setx GROQ_API_KEY       gsk_...     (free: console.groq.com)
    setx CEREBRAS_API_KEY   csk-...     (free, but a volatile model catalog)
    setx MISTRAL_API_KEY    ...         (free developer tier)
    setx GEMINI_API_KEY     ...         (free quota on flash models)

With no key at all, ask() raises NoProvider and the caller falls back to its
rules. The pipeline must never silently stop producing findings because a key
expired.

Groq's free tier is capped at about 6,000 tokens PER MINUTE, which is the real
constraint on design: it is far cheaper to ask one question about forty
candidates than forty questions about one. Callers should batch. ask_json()
retries on 429 with the delay the server asks for.

Every answer is cached in reports/llm_cache.json, keyed by a hash of the
prompt, so a re-run costs nothing and the team gets identical results. Commit
that file.
"""

# --- repo layout: find the agents/, tools/, prepare/ folders and run from
# the repository root, whatever folder the script sits in (2026-10-22).
import os as _os
import sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("agents", "tools", "prepare", "tests", ""):
    _p = _os.path.join(_ROOT, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
if __name__ == "__main__":
    _os.chdir(_ROOT)
# ---


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
# and the server's own message. Added 2026-10-22: a 46-minute silent gap in
# the first M2 agent run could not be explained because failures were never
# written anywhere.
ERRORS_PATH = "reports/llm_errors.csv"

# Groq's free tier is ~6,000 tokens per MINUTE. Staying a little under it is
# cheaper than discovering the ceiling: a 429 costs the Retry-After wait plus
# a round trip, and the retry spends the tokens again anyway. _throttle()
# keeps a sliding 60-second window and sleeps rather than colliding.
TPM_BUDGET = 5500
_recent = []          # (unix_time, tokens) within the last 60s - legacy name

# LOAD SHARING (user decision 2026-10-19). With two or more keys, each call
# goes to the provider with the most free capacity in ITS OWN 60-second
# window, instead of always to the first one. A provider that errors still
# falls back to the next. SPREAD = "fallback" restores the earlier behaviour
# (always the first provider, others only on error). Budgets are per provider
# and deliberately under each free tier:
#   groq     ~6,000 tokens/min                  -> 5,500
#   mistral  free tier: about 1 request/second  -> 1 call per 1.1 s
#   others   unknown                            -> 5,500 tokens/min
SPREAD = os.environ.get("TENDERLENS_LLM_SPREAD", "share")
BUDGETS = {"groq": 5500, "mistral": 400000, "cerebras": 5500,
           "gemini": 5500}
MIN_GAP = {"mistral": 1.1}     # seconds between two calls
_windows = {}         # provider -> [(unix_time, tokens)]
_last_call = {}       # provider -> unix_time
_down_until = {}      # provider -> unix_time; a provider that errored rests
DOWN_FOR = 60         # seconds
# Groq's gpt-oss sometimes answers a plain prompt with a native tool call
# ("Tool choice is none, but model called a tool", HTTP 400). The call it
# meant is in the error body; "recover" returns it as the answer so the
# caller can read it. "fail" restores the old behaviour (provider failed).
TOOL_SLIP = "recover"
DAY_OUT = 6 * 3600    # seconds a provider is skipped after "quota per day"

# Order matters: first with a key present wins, and the rest are fallbacks.
# model is the cheap/fast one for triage; model_strong for judgement calls.
PROVIDERS = [
    {
        "name": "groq",
        "env": "GROQ_API_KEY",
        "base": "https://api.groq.com/openai/v1",
    },
    {
        "name": "cerebras",
        "env": "CEREBRAS_API_KEY",
        "base": "https://api.cerebras.ai/v1",
    },
    {
        "name": "mistral",
        "env": "MISTRAL_API_KEY",
        "base": "https://api.mistral.ai/v1",
    },
    {
        "name": "gemini",
        "env": "GEMINI_API_KEY",
        "base": "https://generativelanguage.googleapis.com/v1beta/openai",
    },
]

# Model IDs are not stable. Groq retires them, and Cerebras once went from a
# dozen models to two without notice. So nothing is hardcoded: the catalogue is
# read from each provider's /models endpoint and the first pattern that matches
# wins. Add a pattern rather than an ID when a new model appears.
PREFER_FAST = [
    r"llama.*8b.*instant", r"llama.*3\.1.*8b", r"gpt-oss-20b",
    r"qwen3-32b", r"llama.*scout", r"mistral-small",
    # Gemini 2.5 Flash has a tiny free quota (about 20 requests a day); prefer 3.x.
    r"gemini-[3-9][\w.-]*flash-lite", r"gemini-[3-9][\w.-]*flash", r"gemini.*flash",
    r"8b", r"small", r"mini", r"instant", r"flash",
]
PREFER_STRONG = [
    r"llama.*3\.3.*70b", r"llama.*70b", r"gpt-oss-120b", r"zai-glm",
    r"llama.*maverick", r"qwen3-32b", r"mistral-large", r"magistral",
    # Gemini Pro left the free tier in 2026: the strong Gemini is the newest
    # non-lite Flash. Picking a paid-only model just errors and rests the
    # provider, which defeats load sharing.
    r"gemini-[3-9][\d.]*-flash(?!-lite)", r"gemini.*flash(?!-lite)",
    r"120b", r"70b", r"large",
]
# Never pick these for text: speech, embedding, vision-only, moderation.
EXCLUDE_MODELS = re.compile(
    r"whisper|tts|embed|guard|moderation|vision|image|audio|rerank|ocr",
    re.I)

MODELS_CACHE = "reports/llm_models.json"


class NoProvider(Exception):
    """No API key is configured, so the caller must use its own rules."""


def load_dotenv(path=".env"):
    """Read KEY=value lines from .env into the environment.

    Done by hand rather than with python-dotenv so there is nothing to install.
    A real environment variable always wins, so an exported key overrides the
    file. Called automatically on import; .env must stay out of git.
    """
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


def list_models(provider, timeout=30):
    """Ask the provider what it actually serves today."""
    req = urllib.request.Request(
        provider["base"] + "/models",
        headers={"Authorization": "Bearer " + os.environ[provider["env"]],
                 "User-Agent": "TenderLens/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    ids = [m.get("id", "") for m in data.get("data", [])]
    return sorted(i for i in ids if i and not EXCLUDE_MODELS.search(i))


def _choose(ids, prefer):
    for pat in prefer:
        for i in ids:
            if re.search(pat, i, re.I):
                return i
    return ids[0] if ids else None


def models_for(provider, refresh=False, verbose=False):
    """(fast, strong) model IDs for this provider, discovered and cached."""
    cache = _load_cache(MODELS_CACHE)
    name = provider["name"]
    if not refresh and name in cache:
        c = cache[name]
        return c.get("fast"), c.get("strong")
    try:
        ids = list_models(provider)
    except Exception as e:
        if verbose:
            print(f"    [{name}] could not list models: {type(e).__name__}")
        return None, None
    fast = _choose(ids, PREFER_FAST)
    strong = _choose(ids, PREFER_STRONG) or fast
    cache[name] = {"fast": fast, "strong": strong, "all": ids}
    _save_cache(cache, MODELS_CACHE)
    if verbose:
        print(f"    [{name}] {len(ids)} models | fast={fast} strong={strong}")
    return fast, strong


def _post(provider, model, system, user, max_tokens, temperature, timeout):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    req = urllib.request.Request(
        provider["base"] + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + os.environ[provider["env"]],
                 "Content-Type": "application/json",
                 "User-Agent": "TenderLens/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    # Every provider here speaks the OpenAI schema, so usage comes back the
    # same way - but not all of them send it, so the caller must cope with {}.
    choice = data["choices"][0]
    msg = choice.get("message") or {}
    answer = msg.get("content") or ""
    if not answer.strip() and TOOL_SLIP == "recover":
        answer = _slip_from_message(msg)
    if not answer.strip():
        # kept for the error log: why was the answer empty?
        _last_empty.update(finish=choice.get("finish_reason"),
                           keys=sorted(k for k, v in msg.items() if v))
    return answer, data.get("usage") or {}


_last_empty = {}


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
    budget = BUDGETS.get(name, TPM_BUDGET)
    wait = 0.0
    spent = sum(n for _t, n in win)
    if spent + est > budget and win:
        wait = 60 - (now - win[0][0]) + 1
    gap = MIN_GAP.get(name, 0) - (now - _last_call.get(name, 0))
    return max(wait, gap, 0.0)


def _order(provs, est):
    """Providers sorted by how soon they can take this call (share mode)."""
    if SPREAD != "share" or len(provs) < 2:
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


def _throttle(est, verbose=False):
    """Sleep if this call would breach the tokens-per-minute budget."""
    now = time.time()
    _recent[:] = [(t, n) for t, n in _recent if now - t < 60]
    spent = sum(n for _t, n in _recent)
    if spent + est <= TPM_BUDGET:
        return
    wait = 60 - (now - _recent[0][0]) + 1 if _recent else 0
    if wait > 0:
        if verbose:
            print(f"    [throttle] {spent} tok in the last minute, "
                  f"waiting {wait:.0f}s before spending ~{est} more")
        time.sleep(min(wait, 65))
        now = time.time()
        _recent[:] = [(t, n) for t, n in _recent if now - t < 60]


USAGE_COLS = ["ts", "tag", "provider", "model", "cached",
              "prompt_tokens", "completion_tokens", "total_tokens",
              "estimated"]


def _http_body(exc):
    """The error body of an HTTPError, read once. Never raises."""
    try:
        return exc.read(4000).decode("utf-8", "replace")
    except Exception:
        return ""


def _tool_slip(body):
    """The tool call a model emitted by mistake, as JSON text, else None."""
    if TOOL_SLIP != "recover" or "tool_use_failed" not in body:
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
    if not cached:
        _recent.append((time.time(), tt or 0))
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


def ask(system, user, strong=False, max_tokens=1500, temperature=0.0,
        timeout=60, retries=2, verbose=False, tag=""):
    """Ask the first working provider. Returns the raw text answer.

    Raises NoProvider if no key is set, or RuntimeError if every provider
    failed - the caller decides what to do, because a silent empty answer
    would look like "no findings".
    """
    provs = available()
    if not provs:
        raise NoProvider("set GROQ_API_KEY (free at console.groq.com)")

    cache = _load_cache()
    errors = []
    # A saved answer is reused whichever provider gave it - otherwise sharing
    # the load would re-ask, on the other provider, questions already paid for.
    for p in provs:
        fast, strg = models_for(p, verbose=verbose)
        model = (strg if strong else fast)
        if not model:
            continue
        ck = _key(model, system, user)
        # An empty cached answer is a truncated reasoning model, not a result.
        # Caching one poisoned the cache: the next run returned "" instantly
        # and never retried. Ignore them, and they get overwritten below.
        if ck in cache and cache[ck].get("answer", "").strip():
            _log_usage(tag, p["name"], model, {}, True, 0)
            return cache[ck]["answer"]
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
        ck = _key(model, system, user)
        for attempt in range(retries + 1):
            try:
                if SPREAD == "share":
                    _throttle_p(p["name"], est, verbose)
                else:
                    _throttle(est, verbose)
                answer, usage = _post(p, model, system, user, max_tokens,
                                      temperature, timeout)
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
                        model, ck = new, _key(new, system, user)
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
if __name__ == "__main__":
    provs = available()
    if not provs:
        print("No LLM provider configured.")
        print("Free key: https://console.groq.com  then put this in .env:")
        print("  GROQ_API_KEY=gsk_...")
        sys.exit(1)

    if "--usage" in sys.argv:
        calls, cached, total, by_tag = usage_summary()
        print(f"{calls} calls logged, {cached} served from cache, "
              f"{total:,} tokens spent\n")
        print(f"  {'tag':24s} {'calls':>6} {'cached':>7} {'tokens':>9}")
        for t, d in sorted(by_tag.items(), key=lambda x: -x[1]["tok"]):
            print(f"  {(t or '(none)'):24s} {d['calls']:6d} "
                  f"{d['cached']:7d} {d['tok']:9,}")
        sys.exit(0)

    refresh = "--refresh" in sys.argv
    show_all = "--list" in sys.argv
    print("configured, in fallback order:")
    for p in provs:
        fast, strong = models_for(p, refresh=refresh)
        print(f"  {p['name']:10s} fast={fast}  strong={strong}")
        if show_all:
            cat = _load_cache(MODELS_CACHE).get(p["name"], {}).get("all", [])
            for i in cat:
                print(f"      {i}")
    if show_all:
        sys.exit(0)

    print("\ntesting the first one ...")
    try:
        r = ask_json(
            "You answer only with JSON.",
            'Is "Leica" a company? Answer {"company": true or false}.',
            verbose=True, tag="selftest")
        print("  ->", r)
    except Exception as e:
        print(f"  failed: {type(e).__name__}: {e}")
        sys.exit(1)
    print("\nOK")
