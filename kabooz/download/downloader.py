# kabooz/download/downloader.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, IO, Optional

import httpx

from ..models.track import Track
from ..models.album import Album
from .naming import resolve_track_path


# ── Result object ──────────────────────────────────────────────────────────

@dataclass
class DownloadResult:
    """
    Returned by every download method. Carries enough information for
    the caller to decide what to do next — tag the file, log it, skip
    taggering if it was a resume, etc.

    Attributes:
        path:          The absolute path where the file was written.
        bytes_written: How many bytes were written in this session.
                       For a resumed download this is only the new bytes,
                       not the total file size.
        total_bytes:   Total file size in bytes. For a fresh download this
                       equals bytes_written. For a resume it's larger.
        skipped:       True if the file already existed with the correct
                       size and was not re-downloaded.
        resumed:       True if the download was resumed from a partial file.
    """
    path: Path
    bytes_written: int
    total_bytes: int
    skipped: bool = False
    resumed: bool = False


# ── Downloader ─────────────────────────────────────────────────────────────

class Downloader:
    """
    Downloads audio files from Qobuz CDN URLs to the local filesystem.

    Handles three cases automatically:
      - Fresh download: file doesn't exist yet
      - Skip: file exists and its size matches Content-Length
      - Resume: file exists but is smaller than Content-Length
        (uses HTTP Range header to continue from where it left off)

    Progress is reported via an optional callback function with the
    signature: (bytes_downloaded: int, total_bytes: int) -> None.
    The callback receives cumulative bytes, not per-chunk bytes, so
    you can wire it directly to a progress bar.

    Usage:
        downloader = Downloader()

        result = downloader.download_track(
            track=track,
            url_info=url_info,       # dict from client.get_track_url()
            dest_dir=Path("/music"),
            album=album,             # optional, for album folder structure
            on_progress=lambda done, total: print(f"{done}/{total}"),
        )
    """

    def __init__(
        self,
        http_client: Optional[httpx.Client] = None,
        chunk_size: int = 8192,
    ) -> None:
        # Accept an injected client for testability, same pattern as QobuzClient.
        self._http = http_client or httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
        )
        self._chunk_size = chunk_size

    # ── Public interface ───────────────────────────────────────────────────

    def download_track(
        self,
        track: Track,
        url_info: dict,
        dest_dir: str | Path,
        album: Optional[Album] = None,
        filename_template: Optional[Callable[[Track], str]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> DownloadResult:
        """
        Download a track to the filesystem using the naming conventions
        defined in naming.py.

        Parameters:
            track:             The Track object (for metadata and naming).
            url_info:          The dict returned by client.get_track_url().
                               Must contain "url" and ideally "bit_depth",
                               "sampling_rate", and "mime_type".
            dest_dir:          Root directory to download into.
            album:             If provided, the track is placed inside an
                               album subfolder. For multi-disc albums the
                               disc subfolder is added automatically when
                               track.media_number > 1.
            filename_template: Optional callable(Track) -> str to override
                               the default filename logic.
            on_progress:       Optional callback(bytes_done, total_bytes).
        """
        dest_dir = Path(dest_dir)
        url = url_info["url"]

        # Determine the file extension from the MIME type in url_info.
        # Fall back to .flac if not present — Qobuz is almost always FLAC.
        mime = url_info.get("mime_type", "audio/flac")
        extension = ".mp3" if "mpeg" in mime else ".flac"

        bit_depth = url_info.get("bit_depth")
        sampling_rate = url_info.get("sampling_rate")

        # Determine if this is a multi-disc album.
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
            filename_template=filename_template,
        )

        return self._download_to_path(url, dest_path, on_progress)

    def download_to_file(
        self,
        url: str,
        dest: IO[bytes],
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> DownloadResult:
        """
        Low-level download: stream a URL into any file-like object.
        No skip/resume logic here — the caller controls the destination.
        Useful for downloading into a BytesIO buffer or a temp file.

        Parameters:
            url:         The CDN URL to download from.
            dest:        Any writable binary file-like object.
            on_progress: Optional callback(bytes_done, total_bytes).
        """
        with self._http.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            written = 0
            for chunk in response.iter_bytes(chunk_size=self._chunk_size):
                dest.write(chunk)
                written += len(chunk)
                if on_progress:
                    on_progress(written, total)

        return DownloadResult(
            path=Path(),   # no path in this context
            bytes_written=written,
            total_bytes=total or written,
        )

    # ── Internal download logic ────────────────────────────────────────────

    def _download_to_path(
        self,
        url: str,
        path: Path,
        on_progress: Optional[Callable[[int, int], None]],
    ) -> DownloadResult:
        """
        Stream a URL to a specific path, with skip and resume support.

        Skip logic:
            If the file exists, we first make a HEAD request to get the
            Content-Length without downloading the body. If the existing
            file size matches, we skip entirely.

        Resume logic:
            If the file exists but is smaller than Content-Length, we
            send a Range request starting from the current file size.
            The server responds with the remaining bytes (HTTP 206),
            and we append them to the existing file.
        """
        # HEAD request to check what's on the server without downloading.
        head = self._http.head(url)
        total = int(head.headers.get("content-length", 0))

        existing_size = path.stat().st_size if path.exists() else 0

        # ── Skip ──────────────────────────────────────────────────────────
        if existing_size > 0 and existing_size == total:
            if on_progress:
                on_progress(total, total)
            return DownloadResult(
                path=path,
                bytes_written=0,
                total_bytes=total,
                skipped=True,
            )

        # ── Resume or fresh download ───────────────────────────────────────
        resumed = existing_size > 0 and existing_size < total
        headers = {}
        if resumed:
            headers["Range"] = f"bytes={existing_size}-"

        written = 0
        # "ab" = append binary. For a fresh download the file doesn't
        # exist yet so append is equivalent to write. For a resume it
        # correctly continues from the end of the existing content.
        with open(path, "ab") as f:
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

    # ── Resource management ────────────────────────────────────────────────

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Downloader:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

