# client.py
from __future__ import annotations

import hashlib
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
        # Accepting an optional http_client here is the key to testability.
        # In production, we create a real httpx.Client. In tests, we pass
        # in a mock client that never touches the network. This pattern is
        # called "dependency injection" — instead of creating your dependencies
        # inside the class, you accept them from outside.
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
    ) -> QobuzClient:
        """
        Create a client from a token pool file or URL. The client is
        immediately ready to make API calls — no need to call login().
        The first token in the pool is used automatically.
        """
        pool = TokenPool.from_local_or_url(source, timeout=timeout)
        instance = cls(
            credentials=pool.credentials,
            token_pool=pool,
            http_client=http_client,
        )
        # Bootstrap the session from the first token in the pool.
        # We set user_id to "unknown" because we haven't hit the API yet —
        # it will be populated if the caller later calls get_user_info().
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
        
    # ── Authentication ─────────────────────────────────────────────────────────

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
        
    # ── HTTP layer ─────────────────────────────────────────────────────────────

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
        if require_auth and self.session:
            all_params["user_auth_token"] = self.session.user_auth_token

        response = self._http.request(method, endpoint, params=all_params)
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