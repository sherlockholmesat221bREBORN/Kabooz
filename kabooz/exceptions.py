# exceptions.py

class QobuzError(Exception):
    """Base class for every error this library raises.
    Catch this if you want a single net that catches anything
    from kabooz, regardless of the specific cause."""
    pass


# ── Authentication errors ──────────────────────────────────────────────────

class AuthError(QobuzError):
    """Something went wrong with authentication. Parent class for all
    auth-related errors — catch this if you don't care about the
    specific reason, just that auth failed."""
    pass

class InvalidCredentialsError(AuthError):
    """The username or password was rejected by the API."""
    pass

class TokenExpiredError(AuthError):
    """The token was valid at some point, but the API is now rejecting it.
    The caller should try rotating to the next token or re-logging in."""
    pass

class NoAuthError(AuthError):
    """An API method was called before login() was ever called.
    This is a programmer mistake, not a runtime condition — it means
    the calling code forgot to authenticate first."""
    pass

class TokenPoolExhaustedError(AuthError):
    """Every token in the pool has been tried and every one has failed.
    At this point there is nothing left to rotate to."""
    pass


# ── Credential / config errors ─────────────────────────────────────────────

class CredentialError(QobuzError):
    """Something went wrong loading or parsing app-level credentials
    (the App ID, App Secret, or a pool file)."""
    pass

class TokenPoolLoadError(CredentialError):
    """The pool file could not be read or was malformed. Could mean
    the file path doesn't exist, the URL returned a non-200, or the
    content didn't match the expected format."""
    pass


# ── API / server errors ────────────────────────────────────────────────────

class APIError(QobuzError):
    """The server responded, but with an error. Unlike the auth errors
    above which indicate client-side problems, this means the request
    reached Qobuz and Qobuz said no for some reason.

    Carries status_code alongside the message so callers can distinguish
    between, say, a 404 (missing resource) and a 429 (rate limited)
    without having to parse the message string."""

    def __init__(self, message: str, status_code: int | None = None):
        # Pass the message up to Exception so str(error) and repr(error)
        # work normally — callers who just print the error get the message.
        super().__init__(message)
        self.status_code = status_code

class NotFoundError(APIError):
    """The track, album, or artist ID doesn't exist in the Qobuz catalog."""
    pass

class NotStreamableError(APIError):
    """The item exists but isn't available for download — either because
    the user's subscription tier doesn't cover it, or it's geo-blocked."""
    pass

class RateLimitError(APIError):
    """Too many requests in a short window. The caller should back off
    and retry after a delay."""
    pass

class ConfigError(QobuzError):
    """A config value is missing, the wrong type, or out of range.
    Raised at load/update time so you get a clear message immediately
    rather than a cryptic crash later."""
    pass