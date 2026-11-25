"""
THE LOCAL MODELS - models running on this computer through Ollama, for the
rules assistant: no key, no quota, nothing leaves the machine.

    ollama pull gemma4:e4b         # once: answers (ask_json)
    ollama pull embeddinggemma     # once: embeddings for the search (embed)
    (Ollama runs in the background on Windows after install)

Settings (environment or .env): TENDERLENS_LOCAL_MODEL (default gemma4:e4b),
TENDERLENS_EMBED_MODEL (default embeddinggemma), OLLAMA_URL (default
http://localhost:11434). Answers are cached in reports/local_llm_cache.json,
embeddings in reports/laws/embeddings.json, so a replay needs no model.
"""

import hashlib
import io
import json
import os
import urllib.error
import urllib.request

from tools.llm import load_dotenv

load_dotenv()                     # TENDERLENS_LOCAL_MODEL / OLLAMA_URL in .env
CACHE = os.path.join("reports", "local_llm_cache.json")
TIMEOUT = 180


class Unavailable(RuntimeError):
    """Ollama is not running, the model is missing, or (replay) the answer
    is not in the cache."""


def model():
    return os.environ.get("TENDERLENS_LOCAL_MODEL", "gemma4:e4b")


def url():
    return os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def _key(system, user):
    h = hashlib.sha256((system + "\x00" + user).encode("utf-8")).hexdigest()
    return f"{model()}|{h[:32]}"


def _load():
    if os.path.exists(CACHE):
        with io.open(CACHE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save(cache):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    tmp = CACHE + ".part"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, CACHE)


def status(timeout=1.5):
    """Is the local model ready? -> (ready, message for the auditor)."""
    try:
        with urllib.request.urlopen(url() + "/api/tags", timeout=timeout) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
    except (urllib.error.URLError, OSError, ValueError):
        return False, (f"The local model is not running. Start Ollama (open "
                       f"the Ollama app, or run: ollama serve), then ask "
                       f"again.")
    want = model()
    if not any(n == want or n == want + ":latest" for n in names):
        return False, (f"Ollama is running but {want} is not installed. Run: "
                       f"ollama pull {want}")
    return True, f"Answers by {want}, on this computer."


# A small model writing JSON can fall into repeating itself until Ollama
# aborts it ("token repeat limit reached"). Three guards: the answer's shape
# is given as a JSON schema (Ollama constrains the output to it), repetition
# is penalised and the length capped, and a looping answer is asked once
# more with more variety.
OPTIONS = {"temperature": 0, "repeat_penalty": 1.15, "num_predict": 1200}
RETRY = {"temperature": 0.4, "repeat_penalty": 1.3, "num_predict": 1200}


def _chat(body):
    """POST /api/chat -> the reply. Raises urllib errors."""
    req = urllib.request.Request(url() + "/api/chat",
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.load(r)


def ask_json(system, user, cache_only=False, num_ctx=8192, schema=None):
    """One question -> the model's JSON answer as a dict ({} if it is not
    JSON). schema: a JSON schema the answer must follow (else any JSON).
    Raises Unavailable."""
    cache = _load()
    k = _key(system, user)
    if k in cache:
        return cache[k]
    if cache_only:
        raise Unavailable("not in the cache (replay)")
    body = {"model": model(), "stream": False, "format": schema or "json",
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    data = None
    for opts in (OPTIONS, RETRY):
        try:
            data = _chat(dict(body, options=dict(opts, num_ctx=num_ctx)))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:200]
            if "repeat" in detail and opts is OPTIONS:
                continue                    # looping: once more, varied
            hint = (f" (is the model pulled? ollama pull {model()})"
                    if e.code == 404 or "not found" in detail else "")
            raise Unavailable(f"Ollama answered {e.code}: {detail}{hint}")
        except (urllib.error.URLError, OSError) as e:
            raise Unavailable(f"the local model is not running: start Ollama "
                              f"(open the Ollama app, or run: ollama serve)")
    text = (data.get("message") or {}).get("content") or ""
    try:
        reply = json.loads(text)
    except ValueError:
        reply = {}
    if not isinstance(reply, dict):
        reply = {}
    cache[k] = reply
    _save(cache)
    return reply


# ------------------------------------------------------------ embeddings
EMBED_CACHE = os.path.join("reports", "laws", "embeddings.json")
EMBED_BATCH = 32


def embed_model():
    return os.environ.get("TENDERLENS_EMBED_MODEL", "embeddinggemma")


def _prompted(text, kind, title=""):
    """EmbeddingGemma was trained with task prompts: one for a search query,
    one for a document. Other models get the text as it is."""
    if "embeddinggemma" not in embed_model():
        return text
    if kind == "query":
        return f"task: search result | query: {text}"
    return f"title: {title or 'none'} | text: {text}"


def _ekey(text):
    return embed_model() + "|" + hashlib.sha256(
        text.encode("utf-8")).hexdigest()[:32]


def embed(texts, kind="document", titles=None, cache_only=False):
    """Texts -> unit vectors (lists of floats), cached by model and text.
    kind: "query" or "document". Raises Unavailable."""
    titles = titles or [""] * len(texts)
    items = [_prompted(t, kind, ti) for t, ti in zip(texts, titles)]
    cache = {}
    if os.path.exists(EMBED_CACHE):
        with io.open(EMBED_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    todo = [x for x in dict.fromkeys(items) if _ekey(x) not in cache]
    if todo and cache_only:
        raise Unavailable("embeddings not in the cache (replay)")
    for i in range(0, len(todo), EMBED_BATCH):
        part = todo[i:i + EMBED_BATCH]
        body = json.dumps({"model": embed_model(),
                           "input": part}).encode("utf-8")
        req = urllib.request.Request(url() + "/api/embed", data=body,
                                     headers={"Content-Type":
                                              "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                vecs = json.load(r).get("embeddings") or []
        except urllib.error.HTTPError as e:
            raise Unavailable(f"Ollama answered {e.code} for embeddings (is "
                              f"the model pulled? ollama pull "
                              f"{embed_model()})")
        except (urllib.error.URLError, OSError) as e:
            raise Unavailable(f"Ollama not reachable at {url()} ({e})")
        if len(vecs) != len(part):
            raise Unavailable("Ollama returned no embeddings")
        for x, v in zip(part, vecs):
            n = sum(a * a for a in v) ** 0.5 or 1.0
            cache[_ekey(x)] = [round(a / n, 5) for a in v]
    if todo:
        os.makedirs(os.path.dirname(EMBED_CACHE), exist_ok=True)
        tmp = EMBED_CACHE + ".part"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, EMBED_CACHE)
    return [cache[_ekey(x)] for x in items]
