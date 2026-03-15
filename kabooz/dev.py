# kabooz/dev.py
"""
Dev mode utilities: API response caching, embedded dev audio, verbose logging.

When dev mode is active (enable() has been called):
  - Every _request() call checks the cache before hitting the network.
    On a miss the response is fetched and saved to disk.
    On a hit the cached JSON is returned immediately.
  - Every audio download uses the embedded kabooz/data/dev_audio.opus
    clip instead of streaming real audio, saving bandwidth during testing.
    The clip is transcoded on the fly by ffmpeg to match the requested
    format (FLAC or MP3). Since both are real valid audio files, the full
    tagging, lyrics, and MusicBrainz pipeline runs end-to-end.
  - If the embedded clip is missing (not yet baked) or ffmpeg is absent,
    a plain sine wave is generated as a secondary fallback. If that also
    fails, stub bytes are written and tagging is skipped — but this
    should essentially never happen in practice.
  - dev_log() prints [DEV] prefixed messages to stderr from anywhere in
    the codebase without needing to pass a flag through every call site.

Cache location:  ~/.cache/qobuz/responses/

To bake the dev audio clip:
    python scripts/make_dev_audio.py /path/to/source.mp3 --start 0 --duration 4
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

# ── Module-level activation flag ──────────────────────────────────────────

_dev_active: bool = False


def enable() -> None:
    """Activate dev mode globally. Called once by the CLI callback."""
    global _dev_active
    _dev_active = True


def is_active() -> bool:
    return _dev_active


# ── Logging ────────────────────────────────────────────────────────────────

def dev_log(msg: str) -> None:
    """
    Print a [DEV] prefixed message to stderr.
    No-ops silently when dev mode is not active.
    """
    if not _dev_active:
        return
    try:
        from rich.console import Console
        Console(stderr=True).print(f"[dim][DEV] {msg}[/dim]")
    except ImportError:
        print(f"[DEV] {msg}", file=sys.stderr)


# ── Constants ──────────────────────────────────────────────────────────────

CACHE_DIR        = Path.home() / ".cache" / "qobuz" / "responses"
_AUDIO_CACHE_DIR = Path.home() / ".cache" / "qobuz"

# Fallback stub used only when all audio generation paths fail.
# Not valid audio — tagging is skipped for files containing this marker.
DEV_STUB_BYTES  = b"QOBUZ_DEV_STUB\x00\x00"
DEV_STUB_MARKER = b"QOBUZ_DEV_STUB"


# ── Embedded audio ─────────────────────────────────────────────────────────

def _embedded_opus() -> Optional[Path]:
    """
    Return a Path to the embedded dev_audio.opus inside the package.
    Returns None if the asset hasn't been baked in yet.

    importlib.resources.files() works whether the package is installed
    as a directory or a zip (wheel/egg). In the zip case the file is
    extracted to the audio cache dir once and reused.
    """
    try:
        import importlib.resources
        ref = importlib.resources.files("kabooz.data").joinpath("dev_audio.opus")
        if not ref.is_file():
            return None
        # If it's a real path on disk (editable / directory install), use it directly.
        candidate = Path(str(ref))
        if candidate.exists():
            return candidate
        # Zip install — extract once to the cache dir.
        extracted = _AUDIO_CACHE_DIR / "dev_audio_extracted.opus"
        extracted.parent.mkdir(parents=True, exist_ok=True)
        extracted.write_bytes(ref.read_bytes())
        return extracted
    except Exception:
        return None


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _transcode(source: Path, dest: Path, codec: str, extra_args: list[str] | None = None) -> bool:
    """Run ffmpeg to transcode source → dest with the given codec."""
    cmd = [
        "ffmpeg", "-y",
        "-i", str(source),
        "-c:a", codec,
        *(extra_args or []),
        str(dest),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False


def prepare_dev_audio(dest_path: Path) -> bool:
    """
    Write a dev audio file to dest_path. Returns True if the written
    file is real audio (full tagging pipeline can run), False if it is
    stub bytes (tagging must be skipped).

    Priority:
      1. Transcode the embedded dev_audio.opus to the requested format
      2. Generate a plain sine wave via ffmpeg (fallback if not yet baked)
      3. Write stub bytes (last resort — tagging will be skipped)

    Transcoded files are cached at ~/.cache/qobuz/dev_audio.{ext} so
    ffmpeg only runs once per format per machine.
    """
    extension = dest_path.suffix.lower()
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Transcoded output is cached so ffmpeg only runs once per format.
    cached_transcode = _AUDIO_CACHE_DIR / f"dev_audio{extension}"

    # ── Serve from transcode cache if available ────────────────────────────
    if cached_transcode.exists():
        shutil.copy2(cached_transcode, dest_path)
        dev_log(f"dev audio (cached transcode) → {dest_path.name}")
        return True

    opus_source = _embedded_opus()

    # ── Transcode from embedded Opus clip ─────────────────────────────────
    if opus_source is not None and _ffmpeg_available():
        if extension == ".flac":
            codec      = "flac"
            extra_args = None
        else:
            # MP3 — use a reasonable quality for a dev file
            codec      = "libmp3lame"
            extra_args = ["-q:a", "5"]

        _AUDIO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if _transcode(opus_source, cached_transcode, codec, extra_args):
            shutil.copy2(cached_transcode, dest_path)
            dev_log(
                f"dev audio (embedded opus → {extension[1:].upper()}) "
                f"→ {dest_path.name}"
            )
            return True
        dev_log("[yellow]transcode from embedded clip failed — trying sine wave[/yellow]")

    elif opus_source is None:
        dev_log("[yellow]dev_audio.opus not baked yet — run scripts/make_dev_audio.py[/yellow]")

    # ── Sine wave fallback (no embedded clip or transcode failed) ─────────
    if _ffmpeg_available():
        codec = "flac" if extension == ".flac" else "libmp3lame"
        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
            "-c:a", codec,
            str(cached_transcode),
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.copy2(cached_transcode, dest_path)
            dev_log(f"dev audio (sine wave fallback) → {dest_path.name}")
            return True
        except subprocess.CalledProcessError:
            pass

    # ── Last resort: stub bytes — tagging will be skipped ─────────────────
    dev_log("[red]all audio generation failed — writing stub bytes, tagging skipped[/red]")
    dest_path.write_bytes(DEV_STUB_BYTES)
    return False


# ── Stub detection ─────────────────────────────────────────────────────────

def is_stub(path: Path) -> bool:
    """Return True if the file at path is a fake stub (not real audio)."""
    try:
        return path.exists() and path.read_bytes().startswith(DEV_STUB_MARKER)
    except Exception:
        return False


# ── Cache helpers ──────────────────────────────────────────────────────────

def _cache_key(method: str, endpoint: str, params: Optional[dict]) -> str:
    """
    Stable MD5 key for a (method, endpoint, params) triple.
    Params are sorted before hashing. Auth token excluded — it changes
    per session but the response for a given resource is always the same.
    """
    canonical = method.upper() + endpoint
    if params:
        for k in sorted(params):
            if k == "user_auth_token":
                continue
            canonical += f"{k}={params[k]}"
    return hashlib.md5(canonical.encode()).hexdigest()


def _response_is_empty(data: dict[str, Any]) -> bool:
    """
    Return True if the response looks like what Qobuz returns for a bad
    or unsubscribed token: HTTP 200 but every value is empty, null, or
    a zero-total collection.

    Never cache these — doing so would permanently poison the cache entry
    for that query with empty results.
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


def load_cached(
    method: str,
    endpoint: str,
    params: Optional[dict],
) -> Optional[dict[str, Any]]:
    """Return the cached response dict, or None if not cached."""
    path = CACHE_DIR / f"{_cache_key(method, endpoint, params)}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cached(
    method: str,
    endpoint: str,
    params: Optional[dict],
    data: dict[str, Any],
) -> None:
    """
    Persist a response dict to the cache.
    Empty/bad-token responses are rejected and never cached.
    """
    if _response_is_empty(data):
        dev_log(f"[yellow]not caching empty response for {endpoint}[/yellow]")
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{_cache_key(method, endpoint, params)}.json"
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_cache() -> int:
    """Delete all cached API responses. Returns the number of files removed."""
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
