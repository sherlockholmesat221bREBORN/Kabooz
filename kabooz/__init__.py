# kabooz/__init__.py
from .client import QobuzClient
from .session import QobuzSession
from .quality import Quality
from .exceptions import (
    QobuzError,
    AuthError,
    InvalidCredentialsError,
    TokenExpiredError,
    NoAuthError,
    TokenPoolExhaustedError,
    PoolModeError,
    CredentialError,
    TokenPoolLoadError,
    APIError,
    NotFoundError,
    NotStreamableError,
    RateLimitError,
    ConfigError,
)
from .models.release import Release, ReleasesList
from .models.favorites import UserFavorites, UserFavoriteIds, LabelDetail
from .models.user import UserProfile, UserCredential

# Local data-layer types — exposed here so library consumers don't need to
# import from the internal subpackage paths.
from .local import (
    LocalStore,
    LocalPlaylist,
    LocalPlaylistTrack,
    ImportResult,
)
