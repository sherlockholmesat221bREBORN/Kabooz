# kabooz/client.py
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

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
    RateLimitError,
    TokenExpiredError,
    TokenPoolExhaustedError,
)
from .quality import Quality
from .models.track import Track
from .models.album import Album
from .models.artist import Artist
from .models.playlist import Playlist

_BASE_URL = "https://www.qobuz.com/api.json/0.2"


class QobuzClient:
    """
    The main entry point for the library. Holds authentication state
    and exposes methods for every supported Qobuz API operation.

    Always construct this via a factory method, never directly:
        QobuzClient.from_credentials(app_id=..., app_secret=...)
        QobuzClient.from_token_pool("~/.config/qobuz/pool.txt")
    """

    def __init__(
        self,
        credentials: AppCredentials,
        token_pool: Optional[TokenPool] = None,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._credentials = credentials
        self._token_pool = token_pool
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
    ) -> QobuzClient:
        """
        Create a client from a bare App ID and App Secret.
        You still need to call login() after this to authenticate.
        """
        return cls(
            credentials=AppCredentials(app_id=app_id, app_secret=app_secret),
            http_client=http_client,
        )

    @classmethod
    def from_token_pool(
        cls,
        source: str | Path,
        http_client: Optional[httpx.Client] = None,
        timeout: int = 10,
        validate: bool = True,
    ) -> QobuzClient:
        """
        Create a client from a token pool file or URL.

        When validate=True (default), each token is tested with a cheap
        catalog call to find the first one that returns real results.
        Tokens from accounts without an active subscription pass auth
        but return empty catalogs — this filters them out automatically.
        """
        pool = TokenPool.from_local_or_url(source, timeout=timeout)
        instance = cls(
            credentials=pool.credentials,
            token_pool=pool,
            http_client=http_client,
        )

        if not validate:
            instance.session = AuthSession(
                user_auth_token=pool.current_token,
                user_id="unknown",
            )
            return instance

        # Try each token. Albums reliably populate for any active
        # subscription — tracks can be empty depending on region.
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
                    # Advance pool cursor to match the working token.
                    while pool.current_token != token:
                        try:
                            pool.next_token()
                        except TokenPoolExhaustedError:
                            break
                    return instance
            except (TokenExpiredError, InvalidCredentialsError):
                continue
            except Exception:
                continue

        # No token passed validation — fall back to first and let real
        # calls surface the error naturally.
        pool.reset()
        instance.session = AuthSession(
            user_auth_token=pool.current_token,
            user_id="unknown",
        )
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

    def __repr__(self) -> str:
        auth = repr(self.session) if self.session else "not authenticated"
        return f"QobuzClient({auth})"

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
            # Send token both ways — as a query param (required by stream/
            # signing endpoints) and as a header (required by catalog
            # endpoints to return populated results).
            all_params["user_auth_token"] = self.session.user_auth_token
            headers["X-User-Auth-Token"]  = self.session.user_auth_token

        response = self._http.request(
            method, endpoint, params=all_params, headers=headers,
        )
        return self._handle_response(response)

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

        Qobuz assembles a canonical string from the request parameters
        and the App Secret, then MD5-hashes it. The timestamp is included
        to prevent replay attacks — the same request sent 10 minutes later
        will have a different signature and be rejected.
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

    # ── Catalog endpoints ──────────────────────────────────────────────────

    def get_track(self, track_id: str | int) -> Track:
        data = self._request(
            "GET", "/track/get",
            params={"track_id": str(track_id)},
        )
        return Track.from_dict(data)

    def get_album(self, album_id: str) -> Album:
        data = self._request(
            "GET", "/album/get",
            params={"album_id": album_id},
        )
        return Album.from_dict(data)

    def get_artist(
        self,
        artist_id: str | int,
        extras: str = "albums",
        limit: int = 25,
        offset: int = 0,
    ) -> Artist:
        """
        Fetch artist info and optionally their discography.
        extras controls what extra data is included — common values are
        "albums", "tracks", "playlists", "focusAll".
        Pass extras="" to skip the album list entirely.
        """
        data = self._request(
            "GET", "/artist/get",
            params={
                "artist_id": str(artist_id),
                "extra":     extras,
                "limit":     limit,
                "offset":    offset,
            },
        )
        return Artist.from_dict(data)

    def get_playlist(
        self,
        playlist_id: str | int,
        limit: int = 50,
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

    def search(
        self,
        query: str,
        type: str = "tracks",
        limit: int = 25,
        offset: int = 0,
    ) -> dict:
        """
        Search the Qobuz catalog.
        type is one of: "tracks", "albums", "artists", "playlists".
        """
        return self._request(
            "GET", "/catalog/search",
            params={
                "query":  query,
                "type":   type,
                "limit":  limit,
                "offset": offset,
            },
        )

    # ── User library endpoints ─────────────────────────────────────────────

    def get_user_favorites(
        self,
        type: str = "tracks",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Fetch the authenticated user's favorites.
        type is one of: "tracks", "albums", "artists"."""
        return self._request(
            "GET", "/favorite/getUserFavorites",
            params={"type": type, "limit": limit, "offset": offset},
        )

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
        """Fetch albums or tracks the user has purchased outright."""
        return self._request(
            "GET", "/purchase/getUserPurchases",
            params={"type": type, "limit": limit, "offset": offset},
        )

    # ── Stream endpoint ────────────────────────────────────────────────────

    def get_track_url(
        self,
        track_id: str | int,
        quality: Quality = Quality.HI_RES,
    ) -> dict:
        """
        Resolve a track to a signed CDN download URL.

        The returned dict contains "url" plus format metadata like
        "bit_depth", "sampling_rate", and "mime_type".

        The URL expires in roughly 30 minutes — don't cache it.

        Raises NotStreamableError if the track isn't available at the
        requested quality or the user's subscription doesn't cover it.
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
