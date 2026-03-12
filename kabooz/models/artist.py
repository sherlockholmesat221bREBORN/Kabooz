# kabooz/models/artist.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .common import _parse
from .album import Album


@dataclass
class Biography:
    summary: Optional[str] = None
    content: Optional[str] = None
    source: Optional[str] = None
    language: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> Biography:
        return cls(
            summary=data.get("summary"),
            content=data.get("content"),
            source=data.get("source"),
            language=data.get("language"),
        )


@dataclass
class ArtistAlbumList:
    """Paginated album list returned when extra=albums is requested."""
    offset: int
    limit: int
    total: int
    items: list[Album] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> ArtistAlbumList:
        return cls(
            offset=data.get("offset", 0),
            limit=data.get("limit", 25),
            total=data.get("total", 0),
            items=[Album.from_dict(a) for a in data.get("items", [])],
        )


@dataclass
class Artist:
    id: int
    name: str
    slug: Optional[str] = None
    # Both image and picture are documented as null in the API spec.
    # Typed as Optional[str] for when the live API returns a URL.
    image: Optional[str] = None
    picture: Optional[str] = None
    biography: Optional[Biography] = None
    albums_as_primary_artist_count: int = 0
    albums_as_primary_composer_count: int = 0
    albums_count: int = 0
    similar_artist_ids: list[int] = field(default_factory=list)
    information: Optional[str] = None
    # Only populated when extra=albums is included in the request.
    albums: Optional[ArtistAlbumList] = None

    @classmethod
    def from_dict(cls, data: dict) -> Artist:
        return cls(
            id=data["id"],
            name=data["name"],
            slug=data.get("slug"),
            image=data.get("image"),
            picture=data.get("picture"),
            biography=_parse(Biography, data.get("biography")),
            albums_as_primary_artist_count=data.get("albums_as_primary_artist_count", 0),
            albums_as_primary_composer_count=data.get("albums_as_primary_composer_count", 0),
            albums_count=data.get("albums_count", 0),
            similar_artist_ids=data.get("similar_artist_ids", []),
            information=data.get("information"),
            albums=_parse(ArtistAlbumList, data.get("albums")),
        )

