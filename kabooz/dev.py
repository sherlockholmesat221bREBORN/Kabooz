# kabooz/dev.py
"""
Dev mode utilities: API response caching and stub download support.

When dev mode is active:
  - Every _request() call checks the cache before hitting the network.
    On a miss the response is fetched, logged, and saved to disk.
    On a hit the cached JSON is returned immediately and logged as such.
  - Every audio download writes a small stub file instead of streaming
    real audio bytes, saving bandwidth during testing.

Cache location: ~/.cache/qobuz/responses/
Each entry is a JSON file named by the MD5 of (method + endpoint + params).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

# ── Constants ──────────────────────────────────────────────────────────────

CACHE_DIR = Path.home() / ".cache" / "qobuz" / "responses"

# Stub files contain this marker so you can recognise them easily.
# 16 bytes — small enough to be essentially free, large enough to be
# identifiable. Not valid audio — tagging is skipped for stubs.
DEV_STUB_BYTES = b"QOBUZ_DEV_STUB\x00\x00"
DEV_STUB_MARKER = b"QOBUZ_DEV_STUB"


# ── Cache helpers ──────────────────────────────────────────────────────────

def _cache_key(method: str, endpoint: str, params: Optional[dict]) -> str:
    """
    Stable MD5 key for a (method, endpoint, params) triple.
    Params are sorted before hashing so key(a=1, b=2) == key(b=2, a=1).
    """
    canonical = method.upper() + endpoint
    if params:
        for k in sorted(params):
            # Exclude the auth token — it changes per session but the
            # response content for a given resource is always the same.
            if k == "user_auth_token":
                continue
            canonical += f"{k}={params[k]}"
    return hashlib.md5(canonical.encode()).hexdigest()


def load_cached(method: str, endpoint: str, params: Optional[dict]) -> Optional[dict[str, Any]]:
    """Return the cached response dict, or None if not cached."""
    path = CACHE_DIR / f"{_cache_key(method, endpoint, params)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _response_is_empty(data: dict[str, Any]) -> bool:
    """
    Return True if the response looks like what Qobuz returns for a
    bad token: HTTP 200 but every value is empty, null, or zero-total.
    A real response will always have at least one non-empty collection.
    """
    if not data:
        return True
    for v in data.values():
        if isinstance(v, dict):
            if v.get("total", 0) > 0 or v.get("items"):
                return False
        elif isinstance(v, list) and v:
            return False
        elif isinstance(v, str) and v:
            return False
        elif isinstance(v, (int, float)) and v:
            return False
    return True


def save_cached(
    method: str,
    endpoint: str,
    params: Optional[dict],
    data: dict[str, Any],
) -> None:
    if _response_is_empty(data):
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{_cache_key(method, endpoint, params)}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def clear_cache() -> int:
    """Delete all cached responses. Returns the number of files removed."""
    if not CACHE_DIR.exists():
        return 0
    removed = 0
    for f in CACHE_DIR.glob("*.json"):
        try:
            f.unlink()
            removed += 1
        except Exception:
            pass
    return removed


# ── Stub helpers ───────────────────────────────────────────────────────────

def is_stub(path: Path) -> bool:
    """Return True if the file at path is a dev stub (not real audio)."""
    try:
        return path.exists() and path.read_bytes().startswith(DEV_STUB_MARKER)
    except Exception:
        return False
