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
from .models.user import UserProfile
from .models.search import (
    SearchResults,
    TrackSearchResults,
    AlbumSearchResults,
    ArtistSearchResults,
    PlaylistSearchResults,
)

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

        Call this at the top of every method that modifies account state.
        Pool tokens belong to shared accounts — writes against them would
        corrupt state for all pool users.
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

    def _login_with_token(self, token: str, user_id: Optional[str]) -> AuthSession:
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
                "password": hashlib.md5(password.encode("utf-8"), usedforsecurity=False).hexdigest(),
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
        """Advance to the next token in the pool."""
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
        """Persist the current session to a JSON file."""
        if self.session is None:
            raise NoAuthError("No active session to save. Call login() first.")
        dest = Path(path).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(self.session.to_dict(), indent=2))

    def load_session(self, path: str | Path) -> AuthSession:
        """Restore a previously saved session from a JSON file."""
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

        if self._dev:
            from .dev import save_cached, dev_log
            save_cached(method, endpoint, all_params, body)
            dev_log(f"{method} {endpoint} → HTTP {response.status_code} (cached)")

        return body

    def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        """
        Parse the HTTP response, raising typed exceptions for all error statuses.

        On a 2xx response with non-JSON body (e.g. an HTML maintenance page),
        APIError is raised immediately — callers must never receive an empty
        dict that would cause a confusing KeyError later.
        """
        status = response.status_code

        body: dict = {}
        if response.content:
            try:
                body = response.json()
            except Exception:
                if response.is_success:
                    raise APIError(
                        f"Expected JSON response but got: {response.text[:200]!r}",
                        status_code=status,
                    )

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
        """Compute the timestamp + MD5 signature required by getFileUrl."""
        ts = str(int(time.time()))
        canonical = (
            f"trackgetFileUrl"
            f"format_id{format_id}"
            f"intentstream"
            f"track_id{track_id}"
            f"{ts}"
            f"{self._credentials.app_secret}"
        )
        sig = hashlib.md5(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()
        return ts, sig

    # ── Catalog — single-item fetches ──────────────────────────────────────

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
            extra:  Additional data: 'albumsFromSameArtist', 'focus',
                    'focusAll', 'track_ids'. Combine with commas.
            limit:  Maximum number of tracks to include (default 1200).
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
            extras: Comma-separated extras: 'albums', 'playlists',
                    'albums_with_last_release', 'focusAll'.
                    Pass '' to skip extras.
            sort:   Sort extras: 'release_desc', 'official'.
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
        """Fetch a record label and optionally its album catalogue."""
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
            release_type: 'all', 'album', 'live', 'compilation', 'epSingle',
                          'other', 'download'. Combine with commas.
            sort:         'release_date', 'relevant', 'release_date_by_priority'.
            order:        'desc' (default) or 'asc'.
            track_size:   Max tracks per release (1–30). Use 1 for speed.
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

        Note: each similar artist requires a separate API call.
        Use a small limit if you only need a few results.
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

    # ── Catalog — search ──────────────────────────────────────────────────

    def search(
        self,
        query: str,
        type: str = "tracks",
        limit: int = 25,
        offset: int = 0,
    ) -> SearchResults:
        """
        Search the Qobuz catalog.

        Parameters:
            query:  Search string.
            type:   Result types to include. One of: 'tracks', 'albums',
                    'artists', 'playlists', 'articles', 'focus', 'stories'.
                    Pass a comma-separated list to request multiple types.
            limit:  Max results per type (default 25).
            offset: Offset into results (for pagination).

        Returns a :class:`SearchResults` object with typed ``tracks``,
        ``albums``, ``artists``, and ``playlists`` pages.
        """
        data = self._request(
            "GET", "/catalog/search",
            params={"query": query, "type": type, "limit": limit, "offset": offset},
        )
        return SearchResults.from_dict(data)

    def search_tracks(self, query: str, limit: int = 50, offset: int = 0) -> TrackSearchResults:
        """
        Search tracks via the dedicated ``/track/search`` endpoint.

        Returns a :class:`TrackSearchResults` with a typed ``.items`` list
        of :class:`~kabooz.models.track.Track` objects.
        """
        data = self._request(
            "GET", "/track/search",
            params={"query": query, "limit": limit, "offset": offset},
        )
        return TrackSearchResults.from_dict(data)

    def search_albums(self, query: str, limit: int = 50, offset: int = 0) -> AlbumSearchResults:
        """
        Search albums via the dedicated ``/album/search`` endpoint.

        Returns a :class:`AlbumSearchResults` with a typed ``.items`` list
        of :class:`~kabooz.models.album.Album` objects.
        """
        data = self._request(
            "GET", "/album/search",
            params={"query": query, "limit": limit, "offset": offset},
        )
        return AlbumSearchResults.from_dict(data)

    def search_artists(self, query: str, limit: int = 50, offset: int = 0) -> ArtistSearchResults:
        """
        Search artists via the dedicated ``/artist/search`` endpoint.

        Returns a :class:`ArtistSearchResults` with a typed ``.items`` list
        of :class:`~kabooz.models.artist.Artist` objects.
        """
        data = self._request(
            "GET", "/artist/search",
            params={"query": query, "limit": limit, "offset": offset},
        )
        return ArtistSearchResults.from_dict(data)

    def search_playlists(self, query: str, limit: int = 50, offset: int = 0) -> PlaylistSearchResults:
        """
        Search playlists via the dedicated ``/playlist/search`` endpoint.

        Returns a :class:`PlaylistSearchResults` with a typed ``.items`` list
        of :class:`~kabooz.models.playlist.Playlist` objects.
        """
        data = self._request(
            "GET", "/playlist/search",
            params={"query": query, "limit": limit, "offset": offset},
        )
        return PlaylistSearchResults.from_dict(data)

    # ── User library — reads ───────────────────────────────────────────────

    @staticmethod
    def _build_favorite_params(
        track_ids: Optional[list] = None,
        album_ids: Optional[list] = None,
        artist_ids: Optional[list] = None,
    ) -> dict:
        """Build params dict for favorite create/delete endpoints."""
        params = {}
        if track_ids:
            params["track_ids"]  = ",".join(str(i) for i in track_ids)
        if album_ids:
            params["album_ids"]  = ",".join(str(i) for i in album_ids)
        if artist_ids:
            params["artist_ids"] = ",".join(str(i) for i in artist_ids)
        return params
    
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
            type:    'tracks', 'albums', 'artists', 'articles'.
                     None returns all types at once.
            user_id: Fetch another user's public favourites.
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
        """
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if user_id:
            params["user_id"] = user_id
        data = self._request("GET", "/favorite/getUserFavoriteIds", params=params)
        return UserFavoriteIds.from_dict(data)

    def get_user_playlists(self, limit: int = 50, offset: int = 0) -> dict:
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
        """Fetch albums or tracks the user has purchased. type: 'albums' or 'tracks'."""
        return self._request(
            "GET", "/purchase/getUserPurchases",
            params={"type": type, "limit": limit, "offset": offset},
        )

    # ── Favourites — write (personal session only) ─────────────────────────

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
        """
        self._guard_write("add_favorite")
        if not any([track_ids, album_ids, artist_ids]):
            raise ValueError("At least one of track_ids, album_ids, or artist_ids must be provided.")
        params = self._build_favorite_params(track_ids, album_ids, artist_ids)
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
        params = self._build_favorite_params(track_ids, album_ids, artist_ids)
        return self._request("GET", "/favorite/delete", params=params)

    # ── Stream endpoint ────────────────────────────────────────────────────

    def get_track_url(
        self,
        track_id: str | int,
        quality: Quality = Quality.HI_RES,
    ) -> dict:
        """
        Resolve a track to a signed CDN download URL.

        The returned dict contains 'url' plus format metadata:
        'bit_depth', 'sampling_rate', 'mime_type', 'format_id'.

        The URL expires in roughly 30 minutes — don't cache it.

        Raises NotStreamableError if the track cannot be streamed at all.
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

    def iter_artist_albums(
        self,
        artist_id: str | int,
        sort: Optional[str] = None,
        page_size: int = 50,
    ) -> Generator[Album, None, None]:
        """Lazily iterate over all albums for an artist."""
        offset = 0
        while True:
            artist = self.get_artist(
                artist_id, extras="albums", sort=sort,
                limit=page_size, offset=offset,
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
        """Lazily iterate over all releases for an artist."""
        offset = 0
        while True:
            page = self.get_release_list(
                artist_id, release_type=release_type,
                sort=sort, order=order, track_size=1,
                limit=page_size, offset=offset,
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
        """Lazily iterate over all albums on a record label."""
        offset = 0
        while True:
            label = self.get_label(label_id, extra="albums", limit=page_size, offset=offset)
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
        Yields Track, Album, or Artist objects depending on type.

        Parameters:
            type:      'tracks', 'albums', or 'artists'.
                       None iterates all types (tracks → albums → artists).
            user_id:   Fetch another user's public favourites.
            page_size: Items per API call (default 50, max 500).
        """
        types = [type] if type else ["tracks", "albums", "artists"]
        for t in types:
            offset = 0
            while True:
                fav = self.get_user_favorites(
                    type=t, user_id=user_id, limit=page_size, offset=offset,
                )
                page = getattr(fav, t, None)
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
        """Lazily iterate over all purchased albums or tracks."""
        offset = 0
        while True:
            data = self.get_user_purchases(type=type, limit=page_size, offset=offset)
            collection = data.get(type, {})
            items = collection.get("items", [])
            if not items:
                break
            item_cls = Album if type == "albums" else Track
            for item_data in items:
                try:
                    yield item_cls.from_dict(item_data)
                except Exception:
                    yield item_data
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
        For most albums get_album() with limit=1200 is sufficient.
        """
        offset = 0
        while True:
            album = self.get_album(album_id, limit=page_size, offset=offset)
            if not album.tracks or not album.tracks.items:
                break
            for track_summary in album.tracks.items:
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
        Lazily yield all PlaylistTrack items across all pages.
        Pagination-proof regardless of playlist size.
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
        Lazily yield full Track objects for every track in a playlist.
        Makes one extra API call per track — use iter_playlist_track_summaries()
        when only IDs and basic metadata are needed.
        """
        for summary in self.iter_playlist_track_summaries(playlist_id, page_size):
            try:
                yield self.get_track(str(summary.id))
            except (NotFoundError, APIError):
                continue

    # ── User account — read ────────────────────────────────────────────────

    def get_user_info(self) -> UserProfile:
        """
        Fetch the authenticated user's full profile.

        Returns a typed UserProfile with subscription tier, credential
        parameters, display names, avatar URL, and so on.
        """
        data = self._request("GET", "/user/get")
        return UserProfile.from_dict(data)

    def reset_password(self, username_or_email: str) -> dict:
        """
        Request a password reset email for a Qobuz account.

        Works for both username and email address. Does NOT require
        authentication — can be called before login.
        """
        return self._request(
            "GET", "/user/resetPassword",
            params={"username": username_or_email},
            require_auth=False,
        )

    # ── User account — update (personal session only) ──────────────────────

    def update_user(
        self,
        email: Optional[str] = None,
        firstname: Optional[str] = None,
        lastname: Optional[str] = None,
        display_name: Optional[str] = None,
        country_code: Optional[str] = None,
        language_code: Optional[str] = None,
        newsletter: Optional[bool] = None,
    ) -> UserProfile:
        """
        Update the authenticated user's profile fields.

        Only the fields you pass are changed — omit a parameter to leave
        the corresponding field unchanged.

        Parameters:
            email:         New email address.
            firstname:     Given name.
            lastname:      Family name.
            display_name:  Public display name (separate from login username).
            country_code:  ISO 3166-1 alpha-2 country code, e.g. 'US', 'GB'.
            language_code: Preferred interface language, e.g. 'en', 'fr'.
            newsletter:    Subscribe / unsubscribe from the Qobuz newsletter.

        Returns the updated UserProfile.
        Raises PoolModeError in pool mode.
        Raises APIError if the server rejects the update (e.g. email taken).
        """
        self._guard_write("update_user")
        params: dict[str, Any] = {}
        if email         is not None: params["email"]         = email
        if firstname     is not None: params["firstname"]     = firstname
        if lastname      is not None: params["lastname"]      = lastname
        if display_name  is not None: params["display_name"]  = display_name
        if country_code  is not None: params["country_code"]  = country_code
        if language_code is not None: params["language_code"] = language_code
        if newsletter    is not None: params["newsletter"]    = int(newsletter)
        if not params:
            raise ValueError("update_user() called with no fields to update.")
        data = self._request("POST", "/user/update", params=params)
        return UserProfile.from_dict(data)

    def update_password(
        self,
        current_password: str,
        new_password: str,
    ) -> dict:
        """
        Change the password for the authenticated account.

        Parameters:
            current_password: The user's existing password (plain text — it
                              is MD5-hashed before transmission).
            new_password:     The desired new password (plain text — hashed
                              before transmission).

        Returns a status dict from the API.
        Raises PoolModeError in pool mode.
        Raises InvalidCredentialsError if current_password is wrong.
        """
        self._guard_write("update_password")
        return self._request(
            "POST", "/user/update",
            params={
                "old_password": hashlib.md5(current_password.encode("utf-8"), usedforsecurity=False).hexdigest(),
                "new_password": hashlib.md5(new_password.encode("utf-8"), usedforsecurity=False).hexdigest(),
            },
        )

    # ── Remote playlist management (personal session only) ─────────────────

    def create_remote_playlist(
        self,
        name: str,
        description: str = "",
        is_public: bool = False,
        is_collaborative: bool = False,
    ) -> Playlist:
        """
        Create a new playlist on the user's Qobuz account.

        Parameters:
            name:             Playlist title (required).
            description:      Optional description shown in the app.
            is_public:        If True, the playlist is publicly visible.
            is_collaborative: If True, other users can add tracks.

        Returns the newly created Playlist object.
        Raises PoolModeError in pool mode.
        """
        self._guard_write("create_remote_playlist")
        data = self._request(
            "POST", "/playlist/create",
            params={
                "name":             name,
                "description":      description,
                "is_public":        int(is_public),
                "is_collaborative": int(is_collaborative),
            },
        )
        return Playlist.from_dict(data)

    def update_remote_playlist(
        self,
        playlist_id: str | int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        is_public: Optional[bool] = None,
        is_collaborative: Optional[bool] = None,
    ) -> Playlist:
        """
        Update the metadata of an existing Qobuz playlist.

        Only the fields you pass are changed. The playlist must be owned
        by the authenticated user.

        Returns the updated Playlist object.
        Raises PoolModeError in pool mode.
        """
        self._guard_write("update_remote_playlist")
        params: dict[str, Any] = {"playlist_id": str(playlist_id)}
        if name             is not None: params["name"]             = name
        if description      is not None: params["description"]      = description
        if is_public        is not None: params["is_public"]        = int(is_public)
        if is_collaborative is not None: params["is_collaborative"] = int(is_collaborative)
        if len(params) == 1:
            raise ValueError("update_remote_playlist() called with no fields to update.")
        data = self._request("POST", "/playlist/update", params=params)
        return Playlist.from_dict(data)

    def delete_remote_playlist(self, playlist_id: str | int) -> dict:
        """
        Permanently delete a Qobuz playlist owned by the authenticated user.

        This is irreversible. Raises PoolModeError in pool mode.
        """
        self._guard_write("delete_remote_playlist")
        return self._request(
            "POST", "/playlist/delete",
            params={"playlist_id": str(playlist_id)},
        )

    def add_tracks_to_remote_playlist(
        self,
        playlist_id: str | int,
        track_ids: list[str | int],
        no_duplicate: bool = True,
    ) -> dict:
        """
        Add tracks to a Qobuz playlist owned by the authenticated user.

        Parameters:
            playlist_id:  Playlist to add tracks to.
            track_ids:    List of Qobuz track IDs to append.
            no_duplicate: If True (default), Qobuz will silently skip
                          tracks already in the playlist.

        Returns a status dict with 'tracks_count' (new total).
        Raises PoolModeError in pool mode.
        """
        self._guard_write("add_tracks_to_remote_playlist")
        if not track_ids:
            raise ValueError("track_ids must not be empty.")
        return self._request(
            "POST", "/playlist/addTracks",
            params={
                "playlist_id":  str(playlist_id),
                "track_ids":    ",".join(str(i) for i in track_ids),
                "no_duplicate": int(no_duplicate),
            },
        )

    def remove_tracks_from_remote_playlist(
        self,
        playlist_id: str | int,
        playlist_track_ids: list[int],
    ) -> dict:
        """
        Remove tracks from a Qobuz playlist by their *playlist_track_id*
        (the join-table ID, not the track ID).

        The playlist_track_id field is present on every PlaylistTrack
        object returned by get_playlist() or iter_playlist_track_summaries().
        You must collect these IDs first, then pass them here.

        Example:
            pl = client.get_playlist("12345")
            ids_to_remove = [
                t.playlist_track_id
                for t in pl.tracks.items
                if t.title == "Some Track"
            ]
            client.remove_tracks_from_remote_playlist("12345", ids_to_remove)

        Returns a status dict.
        Raises PoolModeError in pool mode.
        """
        self._guard_write("remove_tracks_from_remote_playlist")
        if not playlist_track_ids:
            raise ValueError("playlist_track_ids must not be empty.")
        return self._request(
            "POST", "/playlist/deleteTracks",
            params={
                "playlist_id":        str(playlist_id),
                "playlist_track_ids": ",".join(str(i) for i in playlist_track_ids),
            },
        )

    def subscribe_to_playlist(self, playlist_id: str | int) -> dict:
        """
        Follow / subscribe to a public Qobuz playlist.

        The playlist appears in the user's library after this call.
        Raises PoolModeError in pool mode.
        """
        self._guard_write("subscribe_to_playlist")
        return self._request(
            "POST", "/playlist/subscribe",
            params={"playlist_id": str(playlist_id)},
        )

    def unsubscribe_from_playlist(self, playlist_id: str | int) -> dict:
        """
        Unfollow / unsubscribe from a Qobuz playlist.

        Raises PoolModeError in pool mode.
        """
        self._guard_write("unsubscribe_from_playlist")
        return self._request(
            "POST", "/playlist/unsubscribe",
            params={"playlist_id": str(playlist_id)},
        )

    # ── Editorial / discovery ──────────────────────────────────────────────

    def get_featured_playlists(
        self,
        type: str = "editor-picks",
        genre_id: Optional[int] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        """
        Fetch editorially curated playlists.

        Parameters:
            type:     Curation type. Common values:
                        'editor-picks'     — editorial staff picks
                        'last-created'     — newest public playlists
                        'best-of'          — best-of collections
                      Check the Qobuz app for current values as the API
                      may support additional types.
            genre_id: Filter by genre ID (optional).
            limit:    Max playlists to return (default 25, max 100).
            offset:   Offset into results.

        Returns a raw dict with a 'playlists' key containing items and
        pagination info.
        """
        params: dict[str, Any] = {
            "type":   type,
            "limit":  limit,
            "offset": offset,
        }
        if genre_id is not None:
            params["genre_id"] = genre_id
        return self._request("GET", "/playlist/getFeatured", params=params)

    def get_new_releases(
        self,
        type: str = "new-releases",
        genre_id: Optional[int] = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        """
        Fetch new or featured album releases from the editorial catalogue.

        Parameters:
            type:     Release feed type. Common values:
                        'new-releases'          — newest releases
                        'new-releases-full'     — full catalog new releases
                        'press-awards'          — critic award winners
                        'editor-picks'          — editorial album picks
                        'most-streamed'         — trending albums
                        'ideal-discography'     — canonical artist picks
                        'best-sellers'          — current bestsellers
                      Check the Qobuz app for current values.
            genre_id: Filter by genre ID (optional).
            limit:    Max albums to return (default 25, max 100).
            offset:   Offset into results.

        Returns a raw dict with an 'albums' key.
        """
        params: dict[str, Any] = {
            "type":   type,
            "limit":  limit,
            "offset": offset,
        }
        if genre_id is not None:
            params["genre_id"] = genre_id
        return self._request("GET", "/album/getFeatured", params=params)

    def get_genres(self, parent_id: Optional[int] = None) -> dict:
        """
        Fetch the Qobuz genre tree.

        Parameters:
            parent_id: Fetch sub-genres of this genre ID.
                       Omit to fetch top-level genres.

        Returns a raw dict with a 'genres' key containing the list.
        """
        params: dict[str, Any] = {}
        if parent_id is not None:
            params["parent_id"] = parent_id
        return self._request("GET", "/genre/list", params=params)
        