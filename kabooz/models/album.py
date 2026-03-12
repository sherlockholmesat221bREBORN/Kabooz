# models/album.py

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

from .common import (
    _parse, _parse_list,
    AlbumArtist, AlbumArtistWithRoles, Composer,
    Label, Genre, Image, Article,
)


@dataclass
class TrackSummary:
    """Lightweight track as it appears inside album.tracks.items."""
    id: int
    title: str
    track_number: int = 0
    media_number: int = 1
    duration: int = 0
    isrc: Optional[str] = None
    maximum_bit_depth: Optional[int] = None
    maximum_sampling_rate: Optional[float] = None
    maximum_channel_count: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> TrackSummary:
        return cls(
            id=data["id"],
            title=data["title"],
            track_number=data.get("track_number", 0),
            media_number=data.get("media_number", 1),
            duration=data.get("duration", 0),
            isrc=data.get("isrc"),
            maximum_bit_depth=data.get("maximum_bit_depth"),
            maximum_sampling_rate=data.get("maximum_sampling_rate"),
            maximum_channel_count=data.get("maximum_channel_count"),
        )


@dataclass
class TracksPage:
    """Paginated track listing inside an album response."""
    offset: int = 0
    limit: int = 0
    total: int = 0
    items: list[TrackSummary] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> TracksPage:
        return cls(
            offset=data.get("offset", 0),
            limit=data.get("limit", 0),
            total=data.get("total", 0),
            items=_parse_list(TrackSummary, data.get("items")),
        )


@dataclass
class Album:
    id: str          # STRING — not int
    title: str
    qobuz_id: Optional[int] = None
    subtitle: Optional[str] = None
    slug: Optional[str] = None
    artist: Optional[AlbumArtist] = None
    artists: list[AlbumArtistWithRoles] = field(default_factory=list)
    composer: Optional[Composer] = None
    label: Optional[Label] = None
    genre: Optional[Genre] = None
    genres_list: list[str] = field(default_factory=list)
    store_related: Optional[dict[str, Any]] = None
    image: Optional[Image] = None
    upc: Optional[str] = None
    copyright: Optional[str] = None
    release_type: Optional[str] = None
    product_type: Optional[str] = None
    created_at: Optional[int] = None
    released_at: Optional[int] = None
    release_date_original: Optional[str] = None
    release_date_download: Optional[str] = None
    release_date_stream: Optional[str] = None
    duration: int = 0
    tracks_count: int = 0
    media_count: int = 0
    maximum_bit_depth: Optional[int] = None
    maximum_sampling_rate: Optional[float] = None
    maximum_channel_count: Optional[int] = None
    maximum_technical_specifications: Optional[str] = None
    article_ids: Optional[dict[str, Any]] = None
    articles: list[Article] = field(default_factory=list)
    purchasable: bool = False
    streamable: bool = False
    previewable: bool = False
    downloadable: bool = False
    hires: bool = False
    hires_streamable: bool = False
    tracks: Optional[TracksPage] = None

    @classmethod
    def from_dict(cls, data: dict) -> Album:
        return cls(
            id=str(data["id"]),      # force str
            title=data["title"],
            qobuz_id=data.get("qobuz_id"),
            subtitle=data.get("subtitle"),
            slug=data.get("slug"),
            artist=_parse(AlbumArtist, data.get("artist")),
            artists=_parse_list(AlbumArtistWithRoles, data.get("artists")),
            composer=_parse(Composer, data.get("composer")),
            label=_parse(Label, data.get("label")),
            genre=_parse(Genre, data.get("genre")),
            genres_list=data.get("genres_list", []),
            store_related=data.get("store_related"),
            image=_parse(Image, data.get("image")),
            upc=data.get("upc"),
            copyright=data.get("copyright"),
            release_type=data.get("release_type"),
            product_type=data.get("product_type"),
            created_at=data.get("created_at"),
            released_at=data.get("released_at"),
            release_date_original=data.get("release_date_original"),
            release_date_download=data.get("release_date_download"),
            release_date_stream=data.get("release_date_stream"),
            duration=data.get("duration", 0),
            tracks_count=data.get("tracks_count", 0),
            media_count=data.get("media_count", 0),
            maximum_bit_depth=data.get("maximum_bit_depth"),
            maximum_sampling_rate=data.get("maximum_sampling_rate"),
            maximum_channel_count=data.get("maximum_channel_count"),
            maximum_technical_specifications=data.get("maximum_technical_specifications"),
            article_ids=data.get("article_ids"),
            articles=_parse_list(Article, data.get("articles")),
            purchasable=data.get("purchasable", False),
            streamable=data.get("streamable", False),
            previewable=data.get("previewable", False),
            downloadable=data.get("downloadable", False),
            hires=data.get("hires", False),
            hires_streamable=data.get("hires_streamable", False),
            tracks=_parse(TracksPage, data.get("tracks")),
        )
