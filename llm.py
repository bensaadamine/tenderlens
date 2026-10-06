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

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

CACHE_PATH = "reports/llm_cache.json"

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
    r"qwen3-32b", r"llama.*scout", r"mistral-small", r"gemini.*flash",
    r"8b", r"small", r"mini", r"instant", r"flash",
]
PREFER_STRONG = [
    r"llama.*3\.3.*70b", r"llama.*70b", r"gpt-oss-120b", r"zai-glm",
    r"llama.*maverick", r"qwen3-32b", r"mistral-large", r"magistral",
    r"gemini.*pro", r"120b", r"70b", r"large",
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
    return data["choices"][0]["message"]["content"]


def ask(system, user, strong=False, max_tokens=1500, temperature=0.0,
        timeout=60, retries=2, verbose=False):
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
    for p in provs:
        fast, strg = models_for(p, verbose=verbose)
        model = (strg if strong else fast)
        if not model:
            errors.append(f"{p['name']}: no usable model")
            continue
        ck = _key(model, system, user)
        # An empty cached answer is a truncated reasoning model, not a result.
        # Caching one poisoned the cache: the next run returned "" instantly
        # and never retried. Ignore them, and they get overwritten below.
        if ck in cache and cache[ck].get("answer", "").strip():
            return cache[ck]["answer"]
        for attempt in range(retries + 1):
            try:
                answer = _post(p, model, system, user, max_tokens,
                               temperature, timeout)
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
                cache[ck] = {"provider": p["name"], "model": model,
                             "answer": answer}
                _save_cache(cache)
                if verbose:
                    print(f"    [{p['name']}/{model}]")
                return answer
            except urllib.error.HTTPError as e:
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
                break
            except Exception as e:
                errors.append(f"{p['name']}: {type(e).__name__}")
                break
    raise RuntimeError("every provider failed -> " + "; ".join(errors))


RE_JSON = re.compile(r"\{.*\}|\[.*\]", re.S)


def ask_json(system, user, **kw):
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
            verbose=True)
        print("  ->", r)
    except Exception as e:
        print(f"  failed: {type(e).__name__}: {e}")
        sys.exit(1)
    print("\nOK")
