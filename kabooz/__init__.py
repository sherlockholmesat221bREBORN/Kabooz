# kabooz/__init__.py
from .client import QobuzClient
from .quality import Quality
from .exceptions import (
    QobuzError,
    AuthError,
    InvalidCredentialsError,
    TokenExpiredError,
    NoAuthError,
    TokenPoolExhaustedError,
    CredentialError,
    TokenPoolLoadError,
    APIError,
    NotFoundError,
    NotStreamableError,
    RateLimitError,
)
