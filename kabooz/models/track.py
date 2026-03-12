# models/track.py

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

from .common import (
    _parse, _parse_list,
    Performer, Composer, Label, Genre,
    Image, Article, AudioInfo, AlbumArtist,
)


@dataclass
class TrackAlbum:
    """The album object nested inside a track response.
    A subset of the full Album — no track listing, no artists list."""
    id: str          # STRING — not int
    title: str
    slug: Optional[str] = None
    product_url: Optional[str] = None
    artist: Optional[AlbumArtist] = None
    composer: Optional[Composer] = None
    label: Optional[Label] = None
    genre: Optional[Genre] = None
    genres_list: list[str] = field(default_factory=list)
    image: Optional[Image] = None
    upc: Optional[str] = None
    copyright: Optional[str] = None
    duration: Optional[int] = None
    tracks_count: Optional[int] = None
    media_count: Optional[int] = None
    maximum_bit_depth: Optional[int] = None
    maximum_sampling_rate: Optional[float] = None
    maximum_channel_count: Optional[int] = None
    created_at: Optional[int] = None
    released_at: Optional[int] = None
    release_date_original: Optional[str] = None
    release_date_download: Optional[str] = None
    release_date_stream: Optional[str] = None
    articles: list[Article] = field(default_factory=list)
    purchasable: bool = False
    streamable: bool = False
    previewable: bool = False
    downloadable: bool = False
    hires: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> TrackAlbum:
        return cls(
            id=str(data["id"]),      # force str — API sometimes returns int
            title=data["title"],
            slug=data.get("slug"),
            product_url=data.get("product_url"),
            artist=_parse(AlbumArtist, data.get("artist")),
            composer=_parse(Composer, data.get("composer")),
            label=_parse(Label, data.get("label")),
            genre=_parse(Genre, data.get("genre")),
            genres_list=data.get("genres_list", []),
            image=_parse(Image, data.get("image")),
            upc=data.get("upc"),
            copyright=data.get("copyright"),
            duration=data.get("duration"),
            tracks_count=data.get("tracks_count"),
            media_count=data.get("media_count"),
            maximum_bit_depth=data.get("maximum_bit_depth"),
            maximum_sampling_rate=data.get("maximum_sampling_rate"),
            maximum_channel_count=data.get("maximum_channel_count"),
            created_at=data.get("created_at"),
            released_at=data.get("released_at"),
            release_date_original=data.get("release_date_original"),
            release_date_download=data.get("release_date_download"),
            release_date_stream=data.get("release_date_stream"),
            articles=_parse_list(Article, data.get("articles")),
            purchasable=data.get("purchasable", False),
            streamable=data.get("streamable", False),
            previewable=data.get("previewable", False),
            downloadable=data.get("downloadable", False),
            hires=data.get("hires", False),
        )


@dataclass
class Track:
    id: int
    title: str
    duration: int
    track_number: int
    media_number: int
    isrc: Optional[str] = None
    copyright: Optional[str] = None
    performers: Optional[str] = None   # comma-separated string of all contributors
    performer: Optional[Performer] = None
    composer: Optional[Composer] = None
    work: Optional[str] = None
    audio_info: Optional[AudioInfo] = None
    album: Optional[TrackAlbum] = None
    maximum_bit_depth: Optional[int] = None
    maximum_sampling_rate: Optional[float] = None
    maximum_channel_count: Optional[int] = None
    article_ids: Optional[dict[str, Any]] = None
    articles: list[Article] = field(default_factory=list)
    purchasable: bool = False
    streamable: bool = False
    previewable: bool = False
    downloadable: bool = False
    hires: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> Track:
        return cls(
            id=data["id"],
            title=data["title"],
            duration=data["duration"],
            track_number=data["track_number"],
            media_number=data["media_number"],
            isrc=data.get("isrc"),
            copyright=data.get("copyright"),
            performers=data.get("performers"),
            performer=_parse(Performer, data.get("performer")),
            composer=_parse(Composer, data.get("composer")),
            work=data.get("work"),
            audio_info=_parse(AudioInfo, data.get("audio_info")),
            album=_parse(TrackAlbum, data.get("album")),
            maximum_bit_depth=data.get("maximum_bit_depth"),
            maximum_sampling_rate=data.get("maximum_sampling_rate"),
            maximum_channel_count=data.get("maximum_channel_count"),
            article_ids=data.get("article_ids"),
            articles=_parse_list(Article, data.get("articles")),
            purchasable=data.get("purchasable", False),
            streamable=data.get("streamable", False),
            previewable=data.get("previewable", False),
            downloadable=data.get("downloadable", False),
            hires=data.get("hires", False),
        )
