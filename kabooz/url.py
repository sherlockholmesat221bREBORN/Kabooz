# kabooz/url.py
from __future__ import annotations

from urllib.parse import urlparse

# All known Qobuz entity types surfaced in URLs.
_VALID_TYPES = {"track", "album", "artist", "playlist"}

# Qobuz serves the same content under country-prefixed paths:
#   https://www.qobuz.com/gb-en/album/discovery/abc123
#   https://www.qobuz.com/album/discovery/abc123
# The locale segment is two lowercase letters, a hyphen, and two more —
# e.g. "gb-en", "us-en", "fr-fr". We strip it before inspecting the path.
_LOCALE_SEGMENT_LEN = 5  # "xx-xx"


def parse_url(url: str) -> tuple[str, str]:
    """
    Parse a Qobuz web URL and return (entity_type, entity_id).

    Supported URL shapes:
        https://www.qobuz.com/album/discovery/abc123
        https://www.qobuz.com/gb-en/album/discovery/abc123
        https://open.qobuz.com/album/abc123
        https://play.qobuz.com/album/abc123

    Returns:
        A (type, id) tuple where type is one of:
        "track", "album", "artist", "playlist"

    Raises:
        ValueError — if the URL is not a recognised Qobuz URL, if the
        entity type is unknown, or if no entity ID can be extracted.

    Examples:
        >>> parse_url("https://www.qobuz.com/album/discovery/abc123")
        ('album', 'abc123')
        >>> parse_url("https://www.qobuz.com/gb-en/track/one-more-time/12345678")
        ('track', '12345678')
        >>> parse_url("https://open.qobuz.com/playlist/42")
        ('playlist', '42')
    """
    parsed = urlparse(url)

    host = parsed.netloc.lower()
    if "qobuz.com" not in host:
        raise ValueError(
            f"Not a Qobuz URL: {url!r}. "
            "Expected a URL on qobuz.com, open.qobuz.com, or play.qobuz.com."
        )

    # Split path into non-empty segments.
    segments = [s for s in parsed.path.split("/") if s]

    if not segments:
        raise ValueError(f"No path segments found in URL: {url!r}")

    # Strip optional locale prefix (e.g. "gb-en", "us-en").
    if (
        len(segments[0]) == _LOCALE_SEGMENT_LEN
        and segments[0][2] == "-"
        and segments[0][:2].isalpha()
        and segments[0][3:].isalpha()
    ):
        segments = segments[1:]

    if not segments:
        raise ValueError(f"URL has no entity path after locale prefix: {url!r}")

    entity_type = segments[0].lower()

    if entity_type not in _VALID_TYPES:
        raise ValueError(
            f"Unknown Qobuz entity type {entity_type!r} in URL: {url!r}. "
            f"Expected one of: {', '.join(sorted(_VALID_TYPES))}."
        )

    # The ID is always the last path segment. On www.qobuz.com the path
    # is /<type>/<slug>/<id>; on open/play it is /<type>/<id> — taking
    # the last segment handles both without branching.
    if len(segments) < 2:
        raise ValueError(
            f"Could not extract an entity ID from URL: {url!r}. "
            "Expected at least two path segments after the locale."
        )

    entity_id = segments[-1]

    if not entity_id:
        raise ValueError(f"Empty entity ID in URL: {url!r}")

    return entity_type, entity_id

