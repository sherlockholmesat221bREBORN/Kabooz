# auth/credentials.py
from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from ..exceptions import TokenPoolExhaustedError, TokenPoolLoadError


# ── App-level credentials ──────────────────────────────────────────────────

@dataclass(frozen=True)
class AppCredentials:
    """
    The App ID and App Secret that identify your application to Qobuz.
    
    frozen=True makes this immutable after construction — you can't
    accidentally overwrite self.app_secret mid-run. It also makes
    AppCredentials hashable, so it can be used as a dict key or in a
    set if you ever need that.
    """

    app_id: str
    app_secret: str

    def __post_init__(self) -> None:
        # __post_init__ runs automatically after the dataclass __init__.
        # It's the right place for validation when using dataclasses.
        if not self.app_id or not self.app_secret:
            raise ValueError("app_id and app_secret must both be non-empty strings.")

    def __repr__(self) -> str:
        # Mask the secret in repr so it doesn't leak into logs or tracebacks.
        masked = self.app_secret[:4] + "..." if self.app_secret else "None"
        return f"AppCredentials(app_id={self.app_id!r}, app_secret={masked})"


# ── Token pool ─────────────────────────────────────────────────────────────

@dataclass
class TokenPool:
    """
    A bundle of one AppCredentials set plus one or more user tokens.

    The pool supports rotation: when a token fails at runtime, you call
    next_token() to advance to the next one. This lets automated scripts
    recover from a single expired token without stopping entirely.

    You should never construct this class directly with TokenPool(...).
    Instead, always use one of the three class methods:

        TokenPool.from_file("~/.config/qobuz/pool.txt")
        TokenPool.from_url("https://example.com/pool.txt")
        TokenPool.from_local_or_url(some_string_that_could_be_either)

    TOKEN POOL FILE FORMAT
    ──────────────────────
    Lines beginning with # are comments and are ignored.
    Blank lines are also ignored. Everything else is read in order:

        # Example pool file
        123456789            ← Line 1 (first non-comment): App ID
        abcdef1234567890     ← Line 2: App Secret
        USER_TOKEN_ONE       ← Line 3+: one or more user tokens
        USER_TOKEN_TWO
    """

    credentials: AppCredentials
    tokens: list[str] = field(default_factory=list)

    # The cursor tracks which token is currently in use.
    # It's excluded from repr and comparison because it's internal state,
    # not part of the pool's identity.
    _cursor: int = field(default=0, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.tokens:
            raise ValueError("TokenPool must contain at least one user token.")

        # Deduplicate tokens while preserving their original order.
        # This prevents the same token from being "rotated to" twice
        # if someone accidentally listed it twice in the pool file.
        seen: set[str] = set()
        deduped: list[str] = []
        for token in self.tokens:
            if token not in seen:
                seen.add(token)
                deduped.append(token)
        self.tokens = deduped

    # ── Token access and rotation ──────────────────────────────────────────

    @property
    def current_token(self) -> str:
        """The token currently in use. Does not move the cursor."""
        return self.tokens[self._cursor]

    def next_token(self) -> str:
        """
        Advance to the next token in the pool and return it.

        Raises TokenPoolExhaustedError if every token has already been
        tried. The typical pattern at the call site looks like this:

            try:
                do_something(pool.current_token)
            except TokenExpiredError:
                token = pool.next_token()  # raises if exhausted
                do_something(token)
        """
        next_cursor = self._cursor + 1
        if next_cursor >= len(self.tokens):
            raise TokenPoolExhaustedError(
                f"All {len(self.tokens)} token(s) in the pool have been exhausted."
            )
        self._cursor = next_cursor
        return self.tokens[self._cursor]

    def reset(self) -> None:
        """
        Reset the cursor back to the first token.
        Useful if you want to retry the whole pool after some time has
        passed — e.g. after sleeping and hoping a rate limit has lifted.
        """
        self._cursor = 0

    def __iter__(self) -> Iterator[str]:
        """Iterate over all tokens from the beginning, ignoring the cursor.
        Useful for inspecting the pool without affecting rotation state."""
        return iter(self.tokens)

    def __len__(self) -> int:
        return len(self.tokens)

    # ── Parsing ────────────────────────────────────────────────────────────

    @classmethod
    def from_string(cls, text: str) -> TokenPool:
        """
        Parse a pool from a raw string. This is the core parsing logic
        that both from_file and from_url delegate to — there's only one
        place where the file format is understood.

        Separating this from file/URL loading also makes testing easy:
        you can call from_string(...) in a test with a literal string
        without touching the filesystem or network at all.
        """
        # Strip comments and blank lines first, so the index-based
        # access below (lines[0] = app_id, lines[1] = app_secret) is
        # robust regardless of how many comments appear at the top.
        lines = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]

        if len(lines) < 3:
            raise TokenPoolLoadError(
                "Token pool must have at least 3 non-comment lines: "
                "app_id, app_secret, and at least one user token. "
                f"Got {len(lines)} line(s)."
            )

        try:
            credentials = AppCredentials(app_id=lines[0], app_secret=lines[1])
        except ValueError as e:
            raise TokenPoolLoadError(f"Invalid app credentials in pool: {e}") from e

        return cls(credentials=credentials, tokens=lines[2:])

    @classmethod
    def from_file(cls, path: str | Path) -> TokenPool:
        """Load a pool from a local file path. Accepts both strings and
        pathlib.Path objects, and expands ~ to the home directory."""
        path = Path(path).expanduser()

        if not path.exists():
            raise TokenPoolLoadError(f"Token pool file not found: {path}")
        if not path.is_file():
            raise TokenPoolLoadError(f"Token pool path is not a file: {path}")

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise TokenPoolLoadError(f"Could not read token pool file: {e}") from e

        return cls.from_string(text)

    @classmethod
    def from_url(cls, url: str, timeout: int = 10) -> TokenPool:
        """
        Load a pool from a remote URL pointing to a raw text file.
        Works with any URL that returns plain text — a private GitHub
        Gist raw URL, an S3 presigned URL, anything like that.

        Uses urllib from the standard library deliberately — we don't
        need httpx's connection pooling for a single startup request,
        and keeping this module free of heavy dependencies makes it
        easier to use in isolation.
        """
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if response.status != 200:
                    raise TokenPoolLoadError(
                        f"Remote pool URL returned HTTP {response.status}: {url}"
                    )
                text = response.read().decode("utf-8")
        except TokenPoolLoadError:
            # Re-raise our own errors without wrapping them a second time.
            raise
        except Exception as e:
            raise TokenPoolLoadError(
                f"Failed to fetch pool from {url!r}: {e}"
            ) from e

        return cls.from_string(text)

    @classmethod
    def from_local_or_url(cls, source: str | Path, timeout: int = 10) -> TokenPool:
        """
        The convenience loader. Detects automatically whether the source
        is a local path or a remote URL based on whether it starts with
        http:// or https://, then delegates to the appropriate method.

        This is what most callers should use — it means you can accept
        either format from a config file or CLI argument without the
        caller needing to decide upfront which loader to call.
        """
        source_str = str(source)
        if source_str.startswith("http://") or source_str.startswith("https://"):
            return cls.from_url(source_str, timeout=timeout)
        return cls.from_file(source_str)
