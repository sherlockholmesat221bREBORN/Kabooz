# kabooz/download/downloader.py
from __future__ import annotations

import shlex
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, IO, Optional

import httpx

from ..models.track import Track
from ..models.album import Album, Goodie
from .naming import resolve_track_path, sanitize


# ── Result objects ─────────────────────────────────────────────────────────

@dataclass
class DownloadResult:
    """
    Returned by every track download method.

    Attributes:
        path:          Absolute path where the file was written.
        bytes_written: Bytes written this session (partial for resumes).
        total_bytes:   Total file size in bytes.
        skipped:       File was already complete — not re-downloaded.
        resumed:       Download was resumed from a partial file.
        dev_stub:      True when the file is a stub (not real audio).
                       When True, the full tagging pipeline is skipped.
    """
    path: Path
    bytes_written: int
    total_bytes: int
    skipped: bool = False
    resumed: bool = False
    dev_stub: bool = False


@dataclass
class GoodieResult:
    """Result of downloading a single goodie (bonus file)."""
    path: Path
    goodie: Goodie
    skipped: bool = False
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class AlbumDownloadResult:
    """Aggregate result of downloading a full album."""
    tracks:  list[DownloadResult]       = field(default_factory=list)
    goodies: list[GoodieResult]         = field(default_factory=list)
    failed:  list[tuple[int, str]]      = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.tracks if not r.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.tracks if r.skipped)


# ── Downloader ─────────────────────────────────────────────────────────────

class Downloader:
    """
    Downloads audio files and goodies from Qobuz CDN URLs.

    Features
    ────────
    - Skip / resume logic based on Content-Length matching.
    - Optional multithreaded album/playlist downloads via max_workers.
    - Optional external downloader (e.g. aria2c) via a shell template.
    - Naming template support (passed through to resolve_track_path).
    - Goodie (bonus file) downloading into the album folder.
    - Dev mode: writes real audio (transcoded from the embedded Opus clip)
      instead of streaming from Qobuz. Falls back to a sine wave if the
      clip isn't baked yet, then to stub bytes as a last resort.

    External downloader template
    ────────────────────────────
    Set external_downloader to a shell command with {url} and {output}
    placeholders. Examples:

        "aria2c -x 16 -s 16 -d {dir} -o {filename} {url}"
        "wget -O {output} {url}"

    When set, the built-in httpx streaming is bypassed entirely for file
    downloads. Skip/resume logic still runs via a HEAD request first.

    Threading
    ─────────
    max_workers=1 (default) is sequential. Higher values parallelise
    track downloads within an album or playlist. Each worker uses its
    own httpx.Client to avoid connection sharing issues.
    """

    def __init__(
        self,
        http_client: Optional[httpx.Client] = None,
        chunk_size: int = 8192,
        read_timeout: float = 300.0,
        connect_timeout: float = 10.0,
        max_workers: int = 1,
        external_downloader: str = "",
        naming_template: Optional[str] = None,
        dev: bool = False,
    ) -> None:
        self._chunk_size          = chunk_size
        self._read_timeout        = read_timeout
        self._connect_timeout     = connect_timeout
        self._max_workers         = max(1, max_workers)
        self._external_downloader = external_downloader.strip()
        self._naming_template     = naming_template
        self._dev                 = dev
        self._http = http_client or self._make_client()

    def _make_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self._read_timeout, connect=self._connect_timeout),
            follow_redirects=True,
        )

    # ── Public interface ───────────────────────────────────────────────────

    def download_track(
        self,
        track: Track,
        url_info: dict,
        dest_dir: str | Path,
        album: Optional[Album] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        playlist_name: Optional[str] = None,
        playlist_index: Optional[int] = None,
    ) -> DownloadResult:
        """
        Download a single track.

        Parameters:
            track:          Full Track object (not TrackSummary).
            url_info:       Dict from client.get_track_url(). Must have "url";
                            optionally "bit_depth", "sampling_rate", "mime_type".
            dest_dir:       Root directory.
            album:          Album context for folder structure and naming.
            on_progress:    Callback(bytes_done, total_bytes).
            playlist_name:  Passed to naming template as {playlist}.
            playlist_index: Passed to naming template as {index}.
        """
        dest_dir = Path(dest_dir)
        url  = url_info["url"]
        mime = url_info.get("mime_type", "").lower()
        
        # If the URL contains 'mp3' or MIME is mpeg, it's an MP3. Period.
        if "mpeg" in mime or "mp3" in mime or ".mp3" in url.lower():
            extension = ".mp3"
        else:
            extension = ".flac"

        bit_depth     = url_info.get("bit_depth")
        sampling_rate = url_info.get("sampling_rate")

        is_multi_disc = (
            album is not None
            and album.media_count is not None
            and album.media_count > 1
        )

        dest_path = resolve_track_path(
            track=track,
            dest_dir=dest_dir,
            extension=extension,
            album=album,
            is_multi_disc=is_multi_disc,
            bit_depth=bit_depth,
            sampling_rate=sampling_rate,
            template=self._naming_template,
            playlist_name=playlist_name,
            playlist_index=playlist_index,
        )

        from ..dev import dev_log
        dev_log(f"resolved path → {dest_path}")

        if self._external_downloader:
            return self._external_download(url, dest_path, on_progress)

        return self._download_to_path(url, dest_path, on_progress)

    def download_album(
        self,
        tracks: list[tuple[Track, dict]],
        dest_dir: str | Path,
        album: Optional[Album] = None,
        on_track_start: Optional[Callable[[Track, int, int], None]] = None,
        on_track_done: Optional[Callable[[Track, DownloadResult], None]] = None,
        on_track_error: Optional[Callable[[Track, Exception], None]] = None,
        download_goodies: bool = True,
    ) -> AlbumDownloadResult:
        """
        Download multiple tracks, optionally in parallel.

        Callbacks receive the Track object so the caller can update a
        progress display without inspecting the result list mid-flight.

        When max_workers > 1 the callbacks may be called from worker
        threads — make them thread-safe (e.g. use rich's thread-safe
        Console rather than bare print).

        When download_goodies is True and album has goodies, they are
        downloaded sequentially after all tracks finish.
        """
        dest_dir = Path(dest_dir)
        result   = AlbumDownloadResult()
        total    = len(tracks)

        def _do(item: tuple[Track, dict], index: int) -> DownloadResult:
            track, url_info = item
            if on_track_start:
                on_track_start(track, index, total)
            return self.download_track(
                track=track,
                url_info=url_info,
                dest_dir=dest_dir,
                album=album,
            )

        if self._max_workers == 1:
            for i, item in enumerate(tracks, 1):
                track, _ = item
                try:
                    r = _do(item, i)
                    result.tracks.append(r)
                    if on_track_done:
                        on_track_done(track, r)
                except Exception as exc:
                    result.failed.append((track.id, str(exc)))
                    if on_track_error:
                        on_track_error(track, exc)
        else:
            futures = {}
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                for i, item in enumerate(tracks, 1):
                    f = pool.submit(_do, item, i)
                    futures[f] = item[0]
                for f in as_completed(futures):
                    track = futures[f]
                    try:
                        r = f.result()
                        result.tracks.append(r)
                        if on_track_done:
                            on_track_done(track, r)
                    except Exception as exc:
                        result.failed.append((track.id, str(exc)))
                        if on_track_error:
                            on_track_error(track, exc)

        # ── Goodies ───────────────────────────────────────────────────────
        if download_goodies and album and album.goodies:
            album_dir = dest_dir
            if result.tracks:
                candidate = result.tracks[0].path.parent
                if album.media_count and album.media_count > 1:
                    candidate = candidate.parent
                if candidate.is_dir():
                    album_dir = candidate

            for goodie in album.goodies:
                gr = self.download_goodie(goodie, album_dir)
                result.goodies.append(gr)

        return result

    def download_goodie(
        self,
        goodie: Goodie,
        dest_dir: Path,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> GoodieResult:
        """
        Download a single bonus file (booklet PDF, video, etc.).

        The filename is derived from the goodie's URL — typically
        something like "booklet.pdf". If that's unavailable the goodie
        name is sanitized and used instead.
        """
        url = goodie.original_url or goodie.url
        if not url:
            return GoodieResult(
                path=dest_dir / sanitize(goodie.name),
                goodie=goodie,
                error="No URL available for this goodie",
            )

        url_filename = url.split("?")[0].rstrip("/").split("/")[-1]
        filename = sanitize(url_filename) if url_filename else sanitize(goodie.name)
        if not filename:
            filename = f"goodie_{goodie.id}"

        dest_path = dest_dir / filename

        from ..dev import dev_log
        dev_log(f"goodie '{goodie.name}' → {dest_path}")

        try:
            result = self._download_to_path(url, dest_path, on_progress)
            return GoodieResult(path=result.path, goodie=goodie, skipped=result.skipped)
        except Exception as exc:
            return GoodieResult(path=dest_path, goodie=goodie, error=str(exc))

    def download_to_file(
        self,
        url: str,
        dest: IO[bytes],
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> DownloadResult:
        """
        Low-level: stream a URL into any writable binary file-like object.
        No skip/resume logic — the caller controls the destination.
        """
        with self._http.stream("GET", url) as response:
            response.raise_for_status()
            total   = int(response.headers.get("content-length", 0))
            written = 0
            for chunk in response.iter_bytes(chunk_size=self._chunk_size):
                dest.write(chunk)
                written += len(chunk)
                if on_progress:
                    on_progress(written, total)

        return DownloadResult(
            path=Path(),
            bytes_written=written,
            total_bytes=total or written,
        )

    # ── Internal download logic ────────────────────────────────────────────

    def _head(self, url: str) -> int:
        """Return Content-Length from a HEAD request, or 0 on failure."""
        try:
            head = self._http.head(url)
            return int(head.headers.get("content-length", 0))
        except Exception:
            return 0

    def _download_to_path(
        self,
        url: str,
        path: Path,
        on_progress: Optional[Callable[[int, int], None]],
    ) -> DownloadResult:
        # ── Dev mode: write dev audio instead of streaming real audio ─────
        if self._dev:
            from ..dev import prepare_dev_audio, is_stub, DEV_STUB_BYTES, dev_log

            # Already exists — check whether it's a stub or real audio.
            if path.exists():
                if is_stub(path):
                    dev_log(f"stub already exists, skipping → {path.name}")
                else:
                    dev_log(f"dev audio already exists, skipping → {path.name}")
                size = path.stat().st_size
                if on_progress:
                    on_progress(size, size)
                return DownloadResult(
                    path=path,
                    bytes_written=0,
                    total_bytes=size,
                    skipped=True,
                    dev_stub=is_stub(path),
                )

            # Write the dev audio file. Returns True = real audio, False = stub.
            is_real_audio = prepare_dev_audio(path)
            size = path.stat().st_size if path.exists() else len(DEV_STUB_BYTES)
            if on_progress:
                on_progress(size, size)
            return DownloadResult(
                path=path,
                bytes_written=size,
                total_bytes=size,
                dev_stub=not is_real_audio,
            )

        # ── Normal path ────────────────────────────────────────────────────
        total         = self._head(url)
        existing_size = path.stat().st_size if path.exists() else 0

        # Skip if the file is at least as large as the CDN reports.
        # Tagging adds cover art and metadata so the on-disk file will
        # exceed the raw Content-Length — that is expected, not corruption.
        if total > 0 and existing_size >= total:
            if on_progress:
                on_progress(total, total)
            return DownloadResult(
                path=path, bytes_written=0, total_bytes=total, skipped=True,
            )

        # Resume only when HEAD gave a real total and file is partial.
        resumed = total > 0 and existing_size > 0 and existing_size < total
        headers = {"Range": f"bytes={existing_size}-"} if resumed else {}
        written = 0
        mode    = "ab" if resumed else "wb"

        with open(path, mode) as f:
            with self._http.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes(chunk_size=self._chunk_size):
                    f.write(chunk)
                    written += len(chunk)
                    if on_progress:
                        on_progress(existing_size + written, total or written)

        return DownloadResult(
            path=path,
            bytes_written=written,
            total_bytes=total or written,
            resumed=resumed,
        )

    def _external_download(
        self,
        url: str,
        path: Path,
        on_progress: Optional[Callable[[int, int], None]],
    ) -> DownloadResult:
        """
        Invoke an external downloader instead of the built-in one.

        Placeholders in the template:
            {url}      — the CDN URL
            {output}   — full destination path (str)
            {dir}      — parent directory of {output}
            {filename} — basename of {output}
        """
        total         = self._head(url)
        existing_size = path.stat().st_size if path.exists() else 0

        # FIX: use >= (not ==) so that tagged files (which grow slightly beyond
        # the raw CDN Content-Length after cover art + metadata are embedded)
        # are still correctly skipped on subsequent runs.  Consistent with the
        # skip logic in _download_to_path.
        if total > 0 and existing_size >= total:
            if on_progress:
                on_progress(total, total)
            return DownloadResult(
                path=path, bytes_written=0, total_bytes=total, skipped=True,
            )

        cmd_str = self._external_downloader.format(
            url      = url,
            output   = str(path),
            dir      = str(path.parent),
            filename = path.name,
        )

        try:
            subprocess.run(
                shlex.split(cmd_str),
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"External downloader failed (exit {exc.returncode}): {stderr}"
            ) from exc

        written = path.stat().st_size if path.exists() else 0
        if on_progress:
            on_progress(written, written)

        return DownloadResult(
            path=path,
            bytes_written=written,
            total_bytes=written,
        )

    # ── Resource management ────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Downloader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
