# auth/session.py
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AuthSession:
    """
    Holds the live authentication state for a logged-in user.
    This is what you get back from a successful login, and what the
    client attaches to every subsequent API request.

    It is a pure data object — no network calls, no file I/O.
    Keeping it that way means you can construct, serialize, and
    inspect sessions anywhere without side effects.
    """

    # The bearer token sent with every authenticated API request.
    user_auth_token: str

    # Qobuz's numeric user ID. Some endpoints require this explicitly
    # rather than deriving it from the token server-side.
    user_id: str

    # When the session was created, as a Unix timestamp. We use this
    # to detect sessions that are likely stale without hitting the API.
    # field(default_factory=time.time) means "call time.time() at the
    # moment this object is constructed" — not once at class definition
    # time, which would make every session share the same timestamp.
    issued_at: float = field(default_factory=time.time)

    # These are optional because they're only populated when you log in
    # with username + password. If you construct a session directly from
    # a token (e.g. from a pool file), you won't have them.
    user_email: Optional[str] = None
    subscription: Optional[str] = None  # e.g. "Studio Premier"

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def age_seconds(self) -> float:
        """How many seconds old this session is."""
        return time.time() - self.issued_at

    @property
    def is_likely_expired(self) -> bool:
        """
        Qobuz tokens don't have a documented TTL, but they do expire
        eventually. We flag anything older than 30 days as potentially
        stale so callers can proactively refresh before hitting a 401.
        This isn't a guarantee — a token could expire sooner, or last
        longer. It's a hint, not a contract.
        """
        thirty_days = 60 * 60 * 24 * 30
        return self.age_seconds > thirty_days

    # ── Serialization ───────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """
        Serialize to a plain dict. Useful for saving a session to a
        JSON file so you can restore it on the next run without
        re-authenticating from scratch.
        """
        return {
            "user_auth_token": self.user_auth_token,
            "user_id":         self.user_id,
            "issued_at":       self.issued_at,
            "user_email":      self.user_email,
            "subscription":    self.subscription,
        }

    @classmethod
    def from_dict(cls, data: dict) -> AuthSession:
        """Reconstruct a session from a previously serialized dict."""
        return cls(
            user_auth_token=data["user_auth_token"],
            user_id=data["user_id"],
            # Fall back to time.time() if issued_at is missing, which
            # would happen if loading a session saved by an older version.
            issued_at=data.get("issued_at", time.time()),
            user_email=data.get("user_email"),
            subscription=data.get("subscription"),
        )

    @classmethod
    def from_token(cls, token: str, user_id: str) -> AuthSession:
        """
        Construct a session directly from a known token and user ID,
        skipping the login flow entirely. This is the path used when
        loading from a token pool file — you already have a token,
        you just need to wrap it in a session object so the client
        can use it uniformly.
        """
        return cls(user_auth_token=token, user_id=user_id)

    # ── Safety ──────────────────────────────────────────────────────────

    def __repr__(self) -> str:
        # Mask most of the token so it doesn't appear in full in logs,
        # stack traces, or debug output — while still being identifiable.
        masked = self.user_auth_token[:6] + "..." if self.user_auth_token else "None"
        return (
            f"AuthSession("
            f"user_id={self.user_id!r}, "
            f"token={masked}, "
            f"email={self.user_email!r})"
        )
