# kabooz/models/user.py
"""
Typed models for the Qobuz user account API.

UserProfile    — full response from GET /user/get
UserCredential — subscription tier embedded in the user object
UserSubscription — standalone subscription info from the credential block
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class UserCredential:
    """
    The 'credential' block inside a user response.
    Describes the user's current subscription tier and its capabilities.
    """
    id: int
    label: str                          # e.g. "Studio Premier Annual"
    description: str                    # e.g. "Hi-Res Downloads included"
    parameters: dict[str, Any] = field(default_factory=dict)

    # Convenience helpers derived from parameters ---
    @property
    def lossy_streaming(self) -> bool:
        return bool(self.parameters.get("lossy_streaming"))

    @property
    def lossless_streaming(self) -> bool:
        return bool(self.parameters.get("lossless_streaming"))

    @property
    def hires_streaming(self) -> bool:
        return bool(self.parameters.get("hires_streaming"))

    @property
    def hires_purchases_streaming(self) -> bool:
        return bool(self.parameters.get("hires_purchases_streaming"))

    @property
    def mobile_streaming(self) -> bool:
        return bool(self.parameters.get("mobile_streaming"))

    @property
    def offline_listening(self) -> bool:
        return bool(self.parameters.get("offline_listening"))

    @property
    def max_audio_quality(self) -> str:
        """Human-readable max quality string, e.g. 'hi_res' or 'lossless'."""
        if self.hires_streaming or self.hires_purchases_streaming:
            return "hi_res"
        if self.lossless_streaming:
            return "lossless"
        if self.lossy_streaming:
            return "lossy"
        return "none"

    @classmethod
    def from_dict(cls, data: dict) -> UserCredential:
        return cls(
            id=data.get("id", 0),
            label=data.get("label", ""),
            description=data.get("description", ""),
            parameters=data.get("parameters", {}),
        )


@dataclass
class UserProfile:
    """
    Full profile as returned by GET /user/get.

    All fields are optional because the API sometimes returns partial
    objects (e.g. when the session token is near expiry or the user
    account is restricted).
    """
    id: int
    login: str                           # username
    email: Optional[str] = None
    firstname: Optional[str] = None
    lastname: Optional[str] = None
    display_name: Optional[str] = None
    country_code: Optional[str] = None
    language_code: Optional[str] = None
    zone: Optional[str] = None
    store: Optional[str] = None
    avatar: Optional[str] = None         # URL of profile picture
    creation_date: Optional[str] = None
    last_update: Optional[str] = None
    newsletter: bool = False
    credential: Optional[UserCredential] = None
    store_features: dict[str, Any] = field(default_factory=dict)
    # Raw dict preserved so callers can access any undocumented field
    _raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def full_name(self) -> str:
        """Convenience: 'Firstname Lastname', falling back to display_name or login."""
        parts = [p for p in [self.firstname, self.lastname] if p]
        if parts:
            return " ".join(parts)
        return self.display_name or self.login

    @property
    def subscription_label(self) -> str:
        """Human-readable subscription tier, e.g. 'Studio Premier Annual'."""
        return self.credential.label if self.credential else "No subscription"

    @classmethod
    def from_dict(cls, data: dict) -> UserProfile:
        # /user/get wraps the user object under a 'user' key
        user = data.get("user", data)

        from .common import _parse
        credential_data = user.get("credential") or user.get("subscription")
        credential = None
        if isinstance(credential_data, dict):
            credential = UserCredential.from_dict(credential_data)

        return cls(
            id=user.get("id", 0),
            login=user.get("login", ""),
            email=user.get("email"),
            firstname=user.get("firstname"),
            lastname=user.get("lastname"),
            display_name=user.get("display_name"),
            country_code=user.get("country_code"),
            language_code=user.get("language_code"),
            zone=user.get("zone"),
            store=user.get("store"),
            avatar=user.get("avatar"),
            creation_date=user.get("creation_date"),
            last_update=user.get("last_update"),
            newsletter=bool(user.get("newsletter", False)),
            credential=credential,
            store_features=user.get("store_features", {}),
            _raw=user,
        )

