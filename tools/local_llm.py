"""
THE LOCAL MODEL - a model running on this computer through Ollama, for the
rules assistant: no key, no quota, nothing leaves the machine.

    ollama pull gemma3:4b          # once
    (Ollama runs in the background on Windows after install)

Settings (environment or .env): TENDERLENS_LOCAL_MODEL (default gemma3:4b),
OLLAMA_URL (default http://localhost:11434). Answers are asked as JSON and
cached in reports/local_llm_cache.json, so a replay needs no model.
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
    return os.environ.get("TENDERLENS_LOCAL_MODEL", "gemma3:4b")


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


def ask_json(system, user, cache_only=False, num_ctx=8192):
    """One question -> the model's JSON answer as a dict ({} if it is not
    JSON). Raises Unavailable."""
    cache = _load()
    k = _key(system, user)
    if k in cache:
        return cache[k]
    if cache_only:
        raise Unavailable("not in the cache (replay)")
    body = json.dumps({
        "model": model(), "stream": False, "format": "json",
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "options": {"temperature": 0, "num_ctx": num_ctx}}).encode("utf-8")
    req = urllib.request.Request(url() + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        raise Unavailable(f"Ollama answered {e.code}: {detail} (is the "
                          f"model pulled? ollama pull {model()})")
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
