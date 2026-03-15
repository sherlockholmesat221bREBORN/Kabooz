# kabooz/client.py
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Generator, Optional

import httpx

from .auth.credentials import AppCredentials, TokenPool
from .auth.session import AuthSession
from .exceptions import (
    APIError,
    AuthError,
    InvalidCredentialsError,
    NoAuthError,
    NotFoundError,
    NotStreamableError,
    PoolModeError,
    RateLimitError,
    TokenExpiredError,
    TokenPoolExhaustedError,
)
from .quality import Quality
from .models.track import Track
from .models.album import Album
from .models.artist import Artist
from .models.playlist import Playlist
from .models.release import Release, ReleasesList
from .models.favorites import UserFavorites, UserFavoriteIds, LabelDetail

_BASE_URL = "https://www.qobuz.com/api.json/0.2"


class QobuzClient:
    """
    The main entry point for the library. Holds authentication state
    and exposes methods for every supported Qobuz API operation.

    Always construct this via a factory method, never directly:
        QobuzClient.from_credentials(app_id=..., app_secret=...)
        QobuzClient.from_token_pool("~/.config/qobuz/pool.txt")

    Pool-mode clients are read-only. Write operations (add/remove
    favourites, library management) raise PoolModeError when attempted
    from a pool session, since pool tokens belong to shared accounts.
    """

    def __init__(
        self,
        credentials: AppCredentials,
        token_pool: Optional[TokenPool] = None,
        http_client: Optional[httpx.Client] = None,
        dev: bool = False,
    ) -> None:
        self._credentials = credentials
        self._token_pool = token_pool
        self._dev = dev
        self.session: Optional[AuthSession] = None
        self._http = http_client or httpx.Client(
            base_url=_BASE_URL,
            headers={"X-App-Id": credentials.app_id},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    # ── Factory methods ────────────────────────────────────────────────────

    @classmethod
    def from_credentials(
        cls,
        app_id: str,
        app_secret: str,
        http_client: Optional[httpx.Client] = None,
        dev: bool = False,
    ) -> QobuzClient:
        """
        Create a client from a bare App ID and App Secret.
        You still need to call login() after this to authenticate.
        """
        return cls(
            credentials=AppCredentials(app_id=app_id, app_secret=app_secret),
            http_client=http_client,
            dev=dev,
        )

    @classmethod
    def from_token_pool(
        cls,
        source: str | Path,
        http_client: Optional[httpx.Client] = None,
        timeout: int = 10,
        validate: bool = True,
        dev: bool = False,
    ) -> QobuzClient:
        """
        Create a client from a token pool file or URL.

        When validate=True (default), each token is tested with a cheap
        catalog call to find the first one that returns real results.
        Tokens from accounts without an active subscription pass auth
        but return empty catalogs — this filters them out automatically.

        Validation always runs with dev=False regardless of the dev flag,
        so that probe requests are never cached. dev is enabled on the
        returned instance only after a working token is confirmed.

        Pool-mode clients are read-only. Write operations raise PoolModeError.
        """
        pool = TokenPool.from_local_or_url(source, timeout=timeout)

        instance = cls(
            credentials=pool.credentials,
            token_pool=pool,
            http_client=http_client,
            dev=False,
        )

        if not validate:
            instance.session = AuthSession(
                user_auth_token=pool.current_token,
                user_id="unknown",
            )
            instance._dev = dev
            return instance

        for token in pool:
            instance.session = AuthSession(
                user_auth_token=token,
                user_id="unknown",
            )
            try:
                result = instance._request(
                    "GET", "/catalog/search",
                    params={"query": "beethoven", "limit": 1},
                )
                if result.get("albums", {}).get("total", 0) > 0:
                    while pool.current_token != token:
                        try:
                            pool.next_token()
                        except TokenPoolExhaustedError:
                            break
                    instance._dev = dev
                    return instance
            except (TokenExpiredError, InvalidCredentialsError):
                continue
            except Exception:
                continue

        pool.reset()
        instance.session = AuthSession(
            user_auth_token=pool.current_token,
            user_id="unknown",
        )
        instance._dev = dev
        return instance

    # ── Context manager support ────────────────────────────────────────────

    def close(self) -> None:
        """Release the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> QobuzClient:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    @property
    def is_authenticated(self) -> bool:
        return self.session is not None

    @property
    def is_pool_mode(self) -> bool:
        """True when this client was created from a token pool."""
        return self._token_pool is not None

    def __repr__(self) -> str:
        auth = repr(self.session) if self.session else "not authenticated"
        mode = " [pool]" if self.is_pool_mode else ""
        return f"QobuzClient({auth}{mode})"

    # ── Pool-mode write guard ──────────────────────────────────────────────

    def _guard_write(self, operation: str) -> None:
        """
        Raise PoolModeError if the client is in pool mode.

        Call this at the top of every method that modifies account state
        (favourites, library). Pool tokens belong to shared accounts —
        writes against them would corrupt state for all pool users.
        """
        if self._token_pool is not None:
            raise PoolModeError(
                f"{operation}() is a write operation and is disabled in token "
                "pool mode. Pool tokens belong to shared accounts — writing "
                "against them would corrupt state for other pool users. "
                "Authenticate with a personal session to use write operations:\n"
                "    client = QobuzClient.from_credentials(app_id=..., app_secret=...)\n"
                "    client.login(username=..., password=...)"
            )

    # ── Authentication ─────────────────────────────────────────────────────

    def login(
        self,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> AuthSession:
        """
        Authenticate the client. Two modes:

        Username + password:
            client.login(username="me@example.com", password="secret")

        Pre-existing token:
            client.login(token="MY_TOKEN", user_id="12345")
        """
        if token is not None:
            return self._login_with_token(token, user_id)
        if username is not None and password is not None:
            return self._login_with_password(username, password)
        raise ValueError(
            "login() requires either (username + password) or (token + user_id)."
        )

    def _login_with_token(
        self,
        token: str,
        user_id: Optional[str],
    ) -> AuthSession:
        if not user_id:
            raise ValueError(
                "login(token=...) also requires user_id. "
                "If you don't know it, use username + password instead."
            )
        self.session = AuthSession.from_token(token=token, user_id=user_id)
        return self.session

    def _login_with_password(self, username: str, password: str) -> AuthSession:
        resp = self._request(
            "POST",
            "/user/login",
            params={
                "username": username,
                "password": hashlib.md5(password.encode("utf-8")).hexdigest(),
            },
            require_auth=False,
        )
        user = resp.get("user", {})
        cred = user.get("credential", {})
        self.session = AuthSession(
            user_auth_token=resp["user_auth_token"],
            user_id=str(user.get("id", "unknown")),
            user_email=user.get("email"),
            subscription=cred.get("description"),
        )
        return self.session

    def logout(self) -> None:
        """Clear the session. You must call login() again after this."""
        self.session = None

    def rotate_token(self) -> AuthSession:
        """
        Advance to the next token in the pool. Call this when you catch
        a TokenExpiredError and want to try the next token without
        re-authenticating from scratch.
        """
        if self._token_pool is None:
            raise AuthError(
                "rotate_token() is only available on clients created via "
                "from_token_pool()."
            )
        new_token = self._token_pool.next_token()
        self.session = AuthSession(
            user_auth_token=new_token,
            user_id=self.session.user_id if self.session else "unknown",
        )
        return self.session

    # ── Session persistence ────────────────────────────────────────────────

    def save_session(self, path: str | Path) -> None:
        """
        Persist the current session to a JSON file.
        Parent directories are created automatically.

        Raises NoAuthError if there is no active session to save.
        """
        if self.session is None:
            raise NoAuthError("No active session to save. Call login() first.")
        dest = Path(path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(self.session.to_dict(), indent=2))

    def load_session(self, path: str | Path) -> AuthSession:
        """
        Restore a previously saved session from a JSON file and set it
        as the active session on this client.

        Raises FileNotFoundError if the file does not exist.
        """
        src = Path(path).expanduser()
        data = json.loads(src.read_text())
        self.session = AuthSession.from_dict(data)
        return self.session

    # ── HTTP layer ─────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[dict] = None,
        require_auth: bool = True,
    ) -> dict[str, Any]:
        if require_auth and self.session is None:
            raise NoAuthError(
                "This call requires authentication. Call login() first."
            )
        all_params = dict(params or {})
        headers = {}

        if require_auth and self.session:
            all_params["user_auth_token"] = self.session.user_auth_token
            headers["X-User-Auth-Token"]  = self.session.user_auth_token

        # ── Dev mode: cache check ──────────────────────────────────────────
        if self._dev:
            from .dev import load_cached, save_cached, dev_log

            cached = load_cached(method, endpoint, all_params)
            if cached is not None:
                dev_log(f"[green]CACHE HIT[/green] {method} {endpoint}")
                return cached

            visible = [k for k in all_params if k not in ("user_auth_token",)]
            dev_log(f"{method} {endpoint} params={visible} → fetching…")

        response = self._http.request(
            method, endpoint, params=all_params, headers=headers,
        )
        body = self._handle_response(response)

        # ── Dev mode: cache the fresh response ────────────────────────────
        if self._dev:
            from .dev import save_cached, dev_log
            save_cached(method, endpoint, all_params, body)
            dev_log(f"{method} {endpoint} → HTTP {response.status_code} (cached)")

        return body

    def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except Exception:
            body = {}

        status = response.status_code

        if status == 401:
            message = body.get("message", "Unauthorized")
            if "token" in message.lower():
                raise TokenExpiredError(message)
            raise InvalidCredentialsError(message)

        if status == 404:
            raise NotFoundError(body.get("message", "Not found."), status_code=status)

        if status == 429:
            raise RateLimitError("Rate limit hit. Back off and retry.", status_code=status)

        if not response.is_success:
            raise APIError(
                body.get("message", f"API error: HTTP {status}"),
                status_code=status,
            )
        return body

    # ── Request signing ────────────────────────────────────────────────────

    def _sign_track_url_request(
        self,
        track_id: str,
        format_id: int,
    ) -> tuple[str, str]:
        """
        Compute the timestamp + MD5 signature required by getFileUrl.
        """
        ts = str(int(time.time()))
        canonical = (
            f"trackgetFileUrl"
            f"format_id{format_id}"
            f"intentstream"
            f"track_id{track_id}"
            f"{ts}"
            f"{self._credentials.app_secret}"
        )
        sig = hashlib.md5(canonical.encode("utf-8")).hexdigest()
        return ts, sig

    # ── Catalog endpoints — single-item fetches ────────────────────────────

    def get_track(self, track_id: str | int) -> Track:
        """Fetch a single track by ID."""
        data = self._request(
            "GET", "/track/get",
            params={"track_id": str(track_id)},
        )
        return Track.from_dict(data)

    def get_album(
        self,
        album_id: str,
        extra: Optional[str] = None,
        limit: int = 1200,
        offset: int = 0,
    ) -> Album:
        """
        Fetch a single album by ID.

        Parameters:
            extra:  Additional data to include. Accepted values:
                    'albumsFromSameArtist', 'focus', 'focusAll', 'track_ids'.
                    Combine with commas for multiple: 'focus,albumsFromSameArtist'.
            limit:  Maximum number of tracks to include (default 1200, API max).
            offset: Offset into the track list.
        """
        params: dict[str, Any] = {"album_id": album_id, "limit": limit, "offset": offset}
        if extra:
            params["extra"] = extra
        data = self._request("GET", "/album/get", params=params)
        return Album.from_dict(data)

    def get_artist(
        self,
        artist_id: str | int,
        extras: str = "albums",
        sort: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Artist:
        """
        Fetch artist info and optionally their discography.

        Parameters:
            extras: Comma-separated extras. Values: 'albums', 'playlists',
                    'albums_with_last_release', 'focusAll'.
                    Pass '' to skip extras entirely.
            sort:   Sort extra results. Values: 'release_desc', 'official'.
            limit:  Max number of extra items (default 50, max 500).
            offset: Offset into extra items.
        """
        params: dict[str, Any] = {
            "artist_id": str(artist_id),
            "extra":     extras,
            "limit":     limit,
            "offset":    offset,
        }
        if sort:
            params["sort"] = sort
        data = self._request("GET", "/artist/get", params=params)
        return Artist.from_dict(data)

    def get_playlist(
        self,
        playlist_id: str | int,
        limit: int = 500,
        offset: int = 0,
    ) -> Playlist:
        """Fetch a single playlist and its tracks."""
        data = self._request(
            "GET", "/playlist/get",
            params={
                "playlist_id": str(playlist_id),
                "extra":       "tracks",
                "limit":       limit,
                "offset":      offset,
            },
        )
        return Playlist.from_dict(data)

    def get_label(
        self,
        label_id: str | int,
        extra: str = "albums",
        limit: int = 25,
        offset: int = 0,
    ) -> LabelDetail:
        """
        Fetch a record label and optionally its album catalogue.

        Parameters:
            extra:  Comma-separated extras. Values: 'albums', 'focus', 'focusAll'.
            limit:  Max albums to include (default 25, max 500).
            offset: Offset into album list.
        """
        params: dict[str, Any] = {
            "label_id": str(label_id),
            "limit":    limit,
            "offset":   offset,
        }
        if extra:
            params["extra"] = extra
        data = self._request("GET", "/label/get", params=params)
        return LabelDetail.from_dict(data)

    def get_release_list(
        self,
        artist_id: str | int,
        release_type: Optional[str] = None,
        sort: Optional[str] = None,
        order: str = "desc",
        track_size: int = 1,
        limit: int = 50,
        offset: int = 0,
    ) -> ReleasesList:
        """
        Fetch a page of releases for an artist from /artist/getReleasesList.

        Parameters:
            release_type: Filter by type. Values: 'all', 'album', 'live',
                          'compilation', 'epSingle', 'other', 'download'.
                          Combine with commas.
            sort:         Sort by: 'release_date', 'relevant',
                          'release_date_by_priority'.
            order:        'desc' (default) or 'asc'.
            track_size:   Max tracks to include per release (1–30, default 1).
                          Use 1 unless you need track listings — it's faster.
            limit:        Max releases per page (default 50, max 100).
            offset:       Offset into results.
        """
        params: dict[str, Any] = {
            "artist_id":  str(artist_id),
            "order":      order,
            "track_size": track_size,
            "limit":      limit,
            "offset":     offset,
        }
        if release_type:
            params["release_type"] = release_type
        if sort:
            params["sort"] = sort
        data = self._request("GET", "/artist/getReleasesList", params=params)
        return ReleasesList.from_dict(data)

    def get_similar_artists(
        self,
        artist_id: str | int,
        limit: int = 10,
    ) -> list[Artist]:
        """
        Return a list of artists similar to the given artist.

        Qobuz surfaces similar artists as a list of IDs on the Artist
        object (similar_artist_ids). This method fetches them and returns
        the full Artist objects, up to `limit` results.

        Note: each similar artist requires a separate API call. Use a
        small limit if you only need a few results.
        """
        artist = self.get_artist(artist_id, extras="")
        similar_ids = (artist.similar_artist_ids or [])[:limit]
        result = []
        for sid in similar_ids:
            try:
                result.append(self.get_artist(sid, extras=""))
            except (NotFoundError, APIError):
                continue
        return result

    # ── Catalog endpoints — search ─────────────────────────────────────────

    def search(
        self,
        query: str,
        type: str = "tracks",
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        """
        Search the Qobuz catalog.
        type is one of: "tracks", "albums", "artists", "playlists",
        "articles", "focus", "stories".
        """
        return self._request(
            "GET", "/catalog/search",
            params={"query": query, "type": type, "limit": limit, "offset": offset},
        )

    def search_tracks(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Search tracks via the dedicated /track/search endpoint."""
        return self._request(
            "GET", "/track/search",
            params={"query": query, "limit": limit, "offset": offset},
        )

    def search_albums(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Search albums via the dedicated /album/search endpoint."""
        return self._request(
            "GET", "/album/search",
            params={"query": query, "limit": limit, "offset": offset},
        )

    def search_artists(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Search artists via the dedicated /artist/search endpoint."""
        return self._request(
            "GET", "/artist/search",
            params={"query": query, "limit": limit, "offset": offset},
        )

    def search_playlists(
        self,
        query: str,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Search playlists via the dedicated /playlist/search endpoint."""
        return self._request(
            "GET", "/playlist/search",
            params={"query": query, "limit": limit, "offset": offset},
        )

    # ── User library — single-page reads ──────────────────────────────────

    def get_user_favorites(
        self,
        type: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> UserFavorites:
        """
        Fetch the authenticated user's favourites.

        Parameters:
            type:    Filter by type: 'tracks', 'albums', 'artists', 'articles'.
                     Pass None (default) to return all types at once.
            user_id: Fetch another user's public favourites by their ID.
                     Omit to use the authenticated user.
            limit:   Max items per type (default 50, max 500).
            offset:  Offset into results.
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if type:
            params["type"] = type
        if user_id:
            params["user_id"] = user_id
        data = self._request("GET", "/favorite/getUserFavorites", params=params)
        return UserFavorites.from_dict(data)

    def get_favorite_ids(
        self,
        user_id: Optional[str] = None,
        limit: int = 5000,
        offset: int = 0,
    ) -> UserFavoriteIds:
        """
        Fetch just the IDs of the user's favourites.
        Useful for quickly checking membership without fetching full objects.

        Parameters:
            user_id: Fetch another user's IDs by their user ID.
            limit:   Max IDs to return (default 5000, max 999999).
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user_id:
            params["user_id"] = user_id
        data = self._request("GET", "/favorite/getUserFavoriteIds", params=params)
        return UserFavoriteIds.from_dict(data)

    def get_user_playlists(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Fetch playlists owned by the authenticated user."""
        return self._request(
            "GET", "/playlist/getUserPlaylists",
            params={"limit": limit, "offset": offset},
        )

    def get_user_purchases(
        self,
        type: str = "albums",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        Fetch albums or tracks the user has purchased outright.
        type is 'albums' or 'tracks'.
        """
        return self._request(
            "GET", "/purchase/getUserPurchases",
            params={"type": type, "limit": limit, "offset": offset},
        )

    # ── Favourites — write operations (personal session only) ─────────────

    def add_favorite(
        self,
        track_ids: Optional[list[str | int]] = None,
        album_ids: Optional[list[str]] = None,
        artist_ids: Optional[list[str | int]] = None,
    ) -> dict:
        """
        Add tracks, albums, and/or artists to the user's favourites.
        At least one list must be non-empty.

        Raises PoolModeError if called from a token-pool client.

        Example:
            client.add_favorite(track_ids=[12345, 67890])
            client.add_favorite(album_ids=["abc123"], artist_ids=[999])
        """
        self._guard_write("add_favorite")
        if not any([track_ids, album_ids, artist_ids]):
            raise ValueError("At least one of track_ids, album_ids, or artist_ids must be provided.")
        params: dict[str, Any] = {
            "track_ids":  ",".join(str(i) for i in (track_ids  or [])),
            "album_ids":  ",".join(str(i) for i in (album_ids  or [])),
            "artist_ids": ",".join(str(i) for i in (artist_ids or [])),
        }
        return self._request("GET", "/favorite/create", params=params)

    def remove_favorite(
        self,
        track_ids: Optional[list[str | int]] = None,
        album_ids: Optional[list[str]] = None,
        artist_ids: Optional[list[str | int]] = None,
    ) -> dict:
        """
        Remove tracks, albums, and/or artists from the user's favourites.
        At least one list must be non-empty.

        Raises PoolModeError if called from a token-pool client.
        """
        self._guard_write("remove_favorite")
        if not any([track_ids, album_ids, artist_ids]):
            raise ValueError("At least one of track_ids, album_ids, or artist_ids must be provided.")
        params: dict[str, Any] = {
            "track_ids":  ",".join(str(i) for i in (track_ids  or [])),
            "album_ids":  ",".join(str(i) for i in (album_ids  or [])),
            "artist_ids": ",".join(str(i) for i in (artist_ids or [])),
        }
        return self._request("GET", "/favorite/delete", params=params)

    # ── Stream endpoint ────────────────────────────────────────────────────

    def get_track_url(
        self,
        track_id: str | int,
        quality: Quality = Quality.HI_RES,
    ) -> dict:
        """
        Resolve a track to a signed CDN download URL.

        The returned dict contains "url" plus format metadata:
        "bit_depth", "sampling_rate", "mime_type", "format_id".

        The URL expires in roughly 30 minutes — don't cache it.

        Note: Qobuz silently downgrades quality when a track is not
        available at the requested tier. Check format_id in the response
        to see what quality was actually granted.

        Raises NotStreamableError if the track cannot be streamed at all
        (geo-block, label restriction, subscription tier).
        """
        if not self.session:
            raise NoAuthError("get_track_url() requires authentication.")

        ts, sig = self._sign_track_url_request(str(track_id), int(quality))

        result = self._request(
            "GET", "/track/getFileUrl",
            params={
                "track_id":    str(track_id),
                "format_id":   int(quality),
                "intent":      "stream",
                "request_ts":  ts,
                "request_sig": sig,
            },
        )

        if "url" not in result:
            raise NotStreamableError(
                result.get("message", "Track URL not available.")
            )

        return result

    # ── Pagination — lazy generators ───────────────────────────────────────
    #
    # All iter_* methods yield individual items one at a time.
    # Pages are fetched on demand — iteration stops as soon as you break.
    #
    # Usage:
    #     for album in client.iter_artist_albums(artist_id):
    #         print(album.display_title)
    #
    #     # Collect everything:
    #     all_albums = list(client.iter_artist_albums(artist_id))

    def iter_artist_albums(
        self,
        artist_id: str | int,
        sort: Optional[str] = None,
        page_size: int = 50,
    ) -> Generator[Album, None, None]:
        """
        Lazily iterate over all albums for an artist.

        Parameters:
            sort:      'release_desc' or 'official'.
            page_size: Items per API call (default 50, max 500).
        """
        offset = 0
        while True:
            artist = self.get_artist(
                artist_id,
                extras="albums",
                sort=sort,
                limit=page_size,
                offset=offset,
            )
            if not artist.albums or not artist.albums.items:
                break
            for album in artist.albums.items:
                yield album
            offset += page_size
            total = getattr(artist.albums, "total", 0) or 0
            if offset >= total:
                break

    def iter_releases(
        self,
        artist_id: str | int,
        release_type: Optional[str] = None,
        sort: Optional[str] = None,
        order: str = "desc",
        page_size: int = 50,
    ) -> Generator[Release, None, None]:
        """
        Lazily iterate over all releases for an artist via
        /artist/getReleasesList.

        Yields Release objects (richer than Album — includes rights,
        structured dates, and per-track format info).

        Parameters:
            release_type: 'album', 'live', 'compilation', 'epSingle',
                          'other', 'download', or combinations.
            sort:         'release_date', 'relevant', 'release_date_by_priority'.
            order:        'desc' (default) or 'asc'.
            page_size:    Items per API call (default 50, max 100).
        """
        offset = 0
        while True:
            page = self.get_release_list(
                artist_id,
                release_type=release_type,
                sort=sort,
                order=order,
                track_size=1,
                limit=page_size,
                offset=offset,
            )
            for release in (page.items or []):
                yield release
            if not page.has_more:
                break
            offset += page_size

    def iter_label_albums(
        self,
        label_id: str | int,
        page_size: int = 50,
    ) -> Generator[Album, None, None]:
        """
        Lazily iterate over all albums on a record label.

        Parameters:
            page_size: Items per API call (default 50, max 500).
        """
        offset = 0
        while True:
            label = self.get_label(
                label_id,
                extra="albums",
                limit=page_size,
                offset=offset,
            )
            if not label.albums or not label.albums.items:
                break
            for album in label.albums.items:
                yield album
            offset += page_size
            total = label.albums_count or (label.albums.total if label.albums else 0) or 0
            if offset >= total:
                break

    def iter_favorites(
        self,
        type: Optional[str] = None,
        user_id: Optional[str] = None,
        page_size: int = 50,
    ) -> Generator[Any, None, None]:
        """
        Lazily iterate over all user favourites.

        Parameters:
            type:      'tracks', 'albums', or 'artists'. Pass None to
                       iterate all types in a single pass (tracks first,
                       then albums, then artists).
            user_id:   Fetch another user's public favourites.
            page_size: Items per API call (default 50, max 500).

        Yields Track, Album, or Artist objects depending on type.
        """
        types = [type] if type else ["tracks", "albums", "artists"]
        for t in types:
            offset = 0
            while True:
                fav = self.get_user_favorites(
                    type=t,
                    user_id=user_id,
                    limit=page_size,
                    offset=offset,
                )
                page = getattr(fav, t, None)   # fav.tracks / fav.albums / fav.artists
                if not page or not page.items:
                    break
                for item in page.items:
                    yield item
                offset += page_size
                if offset >= (page.total or 0):
                    break

    def iter_purchases(
        self,
        type: str = "albums",
        page_size: int = 50,
    ) -> Generator[Any, None, None]:
        """
        Lazily iterate over all purchased albums or tracks.

        Parameters:
            type:      'albums' (default) or 'tracks'.
            page_size: Items per API call (default 50).
        """
        offset = 0
        while True:
            data = self.get_user_purchases(
                type=type,
                limit=page_size,
                offset=offset,
            )
            collection = data.get(type, {})
            items = collection.get("items", [])
            if not items:
                break
            item_cls = Album if type == "albums" else Track
            for item_data in items:
                try:
                    yield item_cls.from_dict(item_data)
                except Exception:
                    yield item_data   # fall back to raw dict on parse error
            offset += page_size
            total = collection.get("total", 0)
            if offset >= total:
                break

    def iter_user_playlists(
        self,
        page_size: int = 50,
    ) -> Generator[Playlist, None, None]:
        """Lazily iterate over all playlists owned by the authenticated user."""
        offset = 0
        while True:
            data = self.get_user_playlists(limit=page_size, offset=offset)
            collection = data.get("playlists", {})
            items = collection.get("items", [])
            if not items:
                break
            for item_data in items:
                try:
                    yield Playlist.from_dict(item_data)
                except Exception:
                    pass
            offset += page_size
            total = collection.get("total", 0)
            if offset >= total:
                break

    def iter_album_tracks(
        self,
        album_id: str,
        page_size: int = 100,
    ) -> Generator[Track, None, None]:
        """
        Lazily iterate over all tracks in an album.

        Useful for very long albums or box sets where the default
        get_album() call might miss tracks if they exceed the page.
        For most albums get_album() with the default limit=1200 is enough.
        """
        offset = 0
        while True:
            album = self.get_album(album_id, limit=page_size, offset=offset)
            if not album.tracks or not album.tracks.items:
                break
            for track_summary in album.tracks.items:
                # TrackSummary → full Track
                try:
                    yield self.get_track(track_summary.id)
                except Exception:
                    pass
            offset += page_size
            total = album.tracks.total or 0
            if offset >= total:
                break

    def iter_playlist_track_summaries(
        self,
        playlist_id: str | int,
        page_size: int = 500,
    ) -> Generator[Any, None, None]:
        """
        Lazily yield all PlaylistTrack items from a playlist, across all
        pages. Pagination-proof regardless of playlist size.

        Returns lightweight PlaylistTrack objects — one API call per
        page rather than one per track. Use this for clone, download,
        and any operation where you only need track IDs and basic metadata.

        Parameters:
            page_size: Items per API call (default 500, Qobuz max).
        """
        offset = 0
        while True:
            pl = self.get_playlist(playlist_id, limit=page_size, offset=offset)
            if not pl.tracks or not pl.tracks.items:
                break
            for t in pl.tracks.items:
                yield t
            if len(pl.tracks.items) < page_size:
                break
            offset += page_size

    def iter_playlist_tracks(
        self,
        playlist_id: str | int,
        page_size: int = 500,
    ) -> Generator[Track, None, None]:
        """
        Lazily yield full Track objects for every track in a playlist,
        across all pages. Pagination-proof.

        Makes one extra API call per track to get the full Track object
        (AudioInfo, performers string, composer, work, version, etc.).

        Use iter_playlist_track_summaries() when you only need IDs and
        basic display metadata — it's much faster for large playlists.
        """
        for summary in self.iter_playlist_track_summaries(playlist_id, page_size):
            try:
                yield self.get_track(str(summary.id))
            except (NotFoundError, APIError):
                continue

    # ── User account endpoints ─────────────────────────────────────────────

    def get_user_info(self) -> dict:
        """
        Fetch the authenticated user's profile information.

        Returns the raw API dict containing fields like:
        id, login, email, firstname, lastname, avatar, credential,
        subscription, store_features, and so on.
        """
        return self._request("GET", "/user/get")

    def reset_password(self, username_or_email: str) -> dict:
        """
        Request a password reset email for a Qobuz account.

        Works for both username and email address. Qobuz sends a reset
        link to the account's registered email address.

        Does NOT require authentication — can be called before login.

        Returns a status dict with a 'status' key ('success' or error).
        """
        return self._request(
            "GET", "/user/resetPassword",
            params={"username": username_or_email},
            require_auth=False,
        )
