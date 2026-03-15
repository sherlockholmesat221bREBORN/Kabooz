# kabooz/download/credits.py
"""
Qobuz performers string parser.

The `performers` field on a Track is a structured plain-text string:

    "Quincy Jones, Producer - Michael Jackson, MainArtist - Jerry Hey, Conductor, Arranger"

Each credit is separated by " - ". Within a credit the name comes first,
followed by one or more comma-separated role tokens.

This module parses that string into two useful forms:

  1. A role → [names] dict for writing proper PERFORMER:Role Vorbis tags
     and TIPL/TMCL ID3 frames.

  2. A deduplicated list of featured artist names (roles MainArtist,
     FeaturedArtist, Artist) beyond the primary performer, for writing
     the ARTISTS tag.

Reference: the Orpheus Network Qobuz module's credit parsing logic,
adapted and extended for standalone use.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Role tokens that identify a credited person as an artist rather than
# a technical contributor.  We use these to build the featured-artist list.
_ARTIST_ROLES = {"MainArtist", "FeaturedArtist", "Artist"}


@dataclass
class ParsedCredits:
    """
    Result of parsing a performers string.

    Attributes:
        roles:            Dict mapping role name → list of contributor names.
                          e.g. {"Producer": ["Quincy Jones"], "Conductor": ["Jerry Hey"]}
                          Artist roles (MainArtist, FeaturedArtist, Artist) are
                          excluded from this dict — they live in featured_artists.

        featured_artists: Ordered list of artist names extracted from artist
                          roles, de-duplicated, primary performer excluded.
                          Ready to append to the primary performer for an
                          ARTISTS / featured-artist tag.

        cleaned_performers: The performers string with artist-role tokens
                            stripped out — only technical credits remain.
                            Useful for writing a PERFORMERS tag that doesn't
                            redundantly restate what ARTIST already says.
    """
    roles: dict[str, list[str]] = field(default_factory=dict)
    featured_artists: list[str] = field(default_factory=list)
    cleaned_performers: str = ""


def parse_performers(
    performers_str: str,
    primary_artist: str = "",
) -> ParsedCredits:
    """
    Parse a Qobuz performers string into structured credit data.

    Parameters:
        performers_str:  The raw `performers` field from the API response.
        primary_artist:  Name of the primary performer (from track.performer).
                         Used to exclude them from the featured_artists list
                         since they're already written to the ARTIST tag.

    Returns:
        ParsedCredits with roles, featured_artists, and cleaned_performers.

    Example:
        >>> parse_performers(
        ...     "Quincy Jones, Producer - Michael Jackson, MainArtist"
        ...     " - Vincent Price, FeaturedArtist",
        ...     primary_artist="Michael Jackson",
        ... )
        ParsedCredits(
            roles={"Producer": ["Quincy Jones"]},
            featured_artists=["Vincent Price"],
            cleaned_performers="Quincy Jones, Producer",
        )
    """
    if not performers_str or not performers_str.strip():
        return ParsedCredits()

    roles: dict[str, list[str]] = {}
    featured: list[str] = []
    # Tracks which names are already in featured to avoid duplicates
    # while preserving order.
    featured_seen: set[str] = set()
    cleaned_parts: list[str] = []

    for credit in performers_str.split(" - "):
        parts = [p.strip() for p in credit.split(",")]
        if not parts or not parts[0]:
            continue

        name = parts[0]
        credit_roles = parts[1:]

        # Separate artist roles from technical roles.
        artist_role_found = any(r in _ARTIST_ROLES for r in credit_roles)
        tech_roles = [r for r in credit_roles if r not in _ARTIST_ROLES]

        # Collect featured artists — anyone with an artist role who isn't
        # the primary performer.
        if artist_role_found and name != primary_artist:
            if name not in featured_seen:
                featured.append(name)
                featured_seen.add(name)

        # Collect technical credits.
        for role in tech_roles:
            if role not in roles:
                roles[role] = []
            if name not in roles[role]:
                roles[role].append(name)

        # Rebuild a cleaned credit entry with only technical roles.
        if tech_roles:
            cleaned_parts.append(f"{name}, {', '.join(tech_roles)}")

    return ParsedCredits(
        roles=roles,
        featured_artists=featured,
        cleaned_performers=" - ".join(cleaned_parts),
    )


def format_credits_for_vorbis(credits: ParsedCredits) -> dict[str, list[str]]:
    """
    Convert parsed credits into Vorbis Comment PERFORMER:Role multi-value tags.

    The standard format for FLAC/Vorbis is:
        PERFORMER=Name (Role)
    or as separate multi-value entries per role:
        PERFORMER:Producer=Quincy Jones

    We use the latter (role-keyed) format as it's more queryable by
    music players that understand it (e.g. Quod Libet, beets).

    Returns a dict of {tag_name: [values]} ready to pass to mutagen.
    """
    tags: dict[str, list[str]] = {}
    for role, names in credits.roles.items():
        tag_key = f"PERFORMER:{role}"
        tags[tag_key] = names
    return tags


def format_credits_for_id3(credits: ParsedCredits) -> dict[str, list[tuple[str, str]]]:
    """
    Convert parsed credits into ID3v2.4 TIPL / TMCL frame data.

    TIPL (Involved people list) — producers, engineers, etc.
    TMCL (Musician credits list) — performers of specific instruments.

    Both frames use the same format: a flat list of (role, name) pairs.
    We put all credits into TIPL for simplicity — splitting between TIPL
    and TMCL correctly requires knowing which roles are instruments vs.
    production roles, which isn't reliably determinable from Qobuz data.

    Returns {"TIPL": [(role, name), ...]} ready to pass to mutagen's
    TIPL frame constructor.
    """
    pairs: list[tuple[str, str]] = []
    for role, names in credits.roles.items():
        for name in names:
            pairs.append((role, name))
    return {"TIPL": pairs}

