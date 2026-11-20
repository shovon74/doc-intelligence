"""
LLM settings saved from the UI (POST /settings/llm).

Stored as JSON in CONFIG_DIR (bind-mounted outside the container, shared by the API and
the worker), so the choice survives restarts and needs no .env edit. When present, it
overrides the matching .env values. Reads are cached by file mtime, so a key saved
through the API is picked up by the worker on its next job without a restart.

Shape: {"provider": "gemini", "keys": {"gemini": "..."}, "models": {"gemini": "..."}}
"""
import json
import os
import threading
from pathlib import Path

_lock = threading.Lock()
_cache: tuple[str, float, dict] | None = None     # (path, mtime, data)


def _path() -> Path:
    return Path(os.environ.get("CONFIG_DIR", "/data/config")) / "llm.json"


def load() -> dict:
    global _cache
    p = _path()
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return {}
    with _lock:
        if _cache and _cache[0] == str(p) and _cache[1] == mtime:
            return _cache[2]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            data = {}
        _cache = (str(p), mtime, data)
        return data


def save(provider: str, model: str, api_key: str | None) -> None:
    """Persist the choice; ``api_key=None`` keeps the key already stored for the provider."""
    global _cache
    p = _path()
    data = dict(load())
    keys = dict(data.get("keys") or {})
    models = dict(data.get("models") or {})
    if api_key:
        keys[provider] = api_key
    models[provider] = model
    data = {"provider": provider, "keys": keys, "models": models}
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)
    with _lock:
        _cache = None


def clear() -> None:
    global _cache
    try:
        _path().unlink()
    except OSError:
        pass
    with _lock:
        _cache = None
