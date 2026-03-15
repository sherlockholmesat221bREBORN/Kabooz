# kabooz/models/favorites.py
"""
Models for user favorites and label detail endpoints.

UserFavorites     — response from /favorite/getUserFavorites
UserFavoriteIds   — response from /favorite/getUserFavoriteIds
LabelDetail       — response from /label/get (richer than the Label in common.py)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .common import _parse, _parse_list, Label, Image
from .album import Album, TracksPage
from .artist import Artist
from .track import Track


# ── Paginated collection ───────────────────────────────────────────────────

@dataclass
class ItemPage:
    """Generic paginated collection (offset / limit / total / items)."""
    offset: int = 0
    limit: int = 0
    total: int = 0
    items: list = field(default_factory=list)

    @classmethod
    def of(cls, item_cls, data: dict) -> ItemPage:
        items = [item_cls.from_dict(i) for i in data.get("items", []) if isinstance(i, dict)]
        return cls(
            offset=data.get("offset", 0),
            limit=data.get("limit", 0),
            total=data.get("total", 0),
            items=items,
        )


# ── UserFavorites ──────────────────────────────────────────────────────────

@dataclass
class UserFavorites:
    """
    Response from GET /favorite/getUserFavorites.

    Each collection (tracks, albums, artists) is a paginated page.
    Which collections are populated depends on the `type` param passed
    to the request — if no type is given, all are returned.
    """
    tracks:  Optional[ItemPage] = None
    albums:  Optional[ItemPage] = None
    artists: Optional[ItemPage] = None

    @classmethod
    def from_dict(cls, data: dict) -> UserFavorites:
        tracks  = ItemPage.of(Track,  data["tracks"])  if "tracks"  in data and isinstance(data.get("tracks"),  dict) else None
        albums  = ItemPage.of(Album,  data["albums"])  if "albums"  in data and isinstance(data.get("albums"),  dict) else None
        artists = ItemPage.of(Artist, data["artists"]) if "artists" in data and isinstance(data.get("artists"), dict) else None
        return cls(tracks=tracks, albums=albums, artists=artists)


# ── UserFavoriteIds ────────────────────────────────────────────────────────

@dataclass
class UserFavoriteIds:
    """
    Response from GET /favorite/getUserFavoriteIds.
    Returns plain lists of IDs, not full objects.
    Useful for quickly checking whether a specific item is in the library.
    """
    tracks:   list[int]  = field(default_factory=list)
    albums:   list[str]  = field(default_factory=list)   # album IDs are strings
    artists:  list[int]  = field(default_factory=list)
    articles: list[int]  = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> UserFavoriteIds:
        return cls(
            tracks=data.get("tracks", []),
            albums=data.get("albums", []),
            artists=data.get("artists", []),
            articles=data.get("articles", []),
        )


# ── LabelDetail ────────────────────────────────────────────────────────────

@dataclass
class LabelDetail:
    """
    Full label response from GET /label/get.

    Richer than the Label in common.py (which is just id/name/slug used
    inline in track and album metadata). This version includes the
    album catalogue, description, and focus items.
    """
    id: Optional[int] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    supplier_id: Optional[int] = None
    description: Optional[str] = None
    description_language: Optional[str] = None
    albums_count: Optional[int] = None
    # Paginated album list — populated when extra="albums" is requested.
    albums: Optional[ItemPage] = None

    @classmethod
    def from_dict(cls, data: dict) -> LabelDetail:
        albums = None
        if "albums" in data and isinstance(data.get("albums"), dict):
            albums = ItemPage.of(Album, data["albums"])
        return cls(
            id=data.get("id"),
            name=data.get("name"),
            slug=data.get("slug"),
            supplier_id=data.get("supplier_id"),
            description=data.get("description"),
            description_language=data.get("description_language"),
            albums_count=data.get("albums_count"),
            albums=albums,
        )
