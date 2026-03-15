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
    version: Optional[str] = None          # e.g. "Deluxe Edition", "Remastered"
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
    parental_warning: bool = False
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

    @property
    def display_title(self) -> str:
        """Album title with version suffix appended if present."""
        t = self.title.rstrip()
        if self.version:
            t += f" ({self.version})"
        return t

    @classmethod
    def from_dict(cls, data: dict) -> TrackAlbum:
        return cls(
            id=str(data["id"]),      # force str — API sometimes returns int
            title=data["title"],
            version=data.get("version"),
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
            parental_warning=data.get("parental_warning", False),
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
    version: Optional[str] = None          # e.g. "Live", "Acoustic", "2011 Remaster"
    isrc: Optional[str] = None
    copyright: Optional[str] = None
    performers: Optional[str] = None       # structured credit string, see credits.py
    performer: Optional[Performer] = None  # primary credited artist
    composer: Optional[Composer] = None
    work: Optional[str] = None             # classical work name, e.g. "Symphony No. 40"
    audio_info: Optional[AudioInfo] = None
    album: Optional[TrackAlbum] = None
    parental_warning: bool = False
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

    @property
    def display_title(self) -> str:
        """
        Full display title as it should appear in tags and filenames.

        Follows the convention from the reference codebase:
          - Classical tracks: "{work} - {title}"
          - Version suffix appended for all: "{title} ({version})"

        Examples:
            work="Symphony No. 40", title="I. Molto allegro"
              → "Symphony No. 40 - I. Molto allegro"
            title="Billie Jean", version="2012 Remaster"
              → "Billie Jean (2012 Remaster)"
            work="Piano Sonata No. 14", title="I. Adagio", version="Live"
              → "Piano Sonata No. 14 - I. Adagio (Live)"
        """
        t = ""
        if self.work:
            t += f"{self.work} - "
        t += self.title.rstrip()
        if self.version:
            t += f" ({self.version})"
        return t

    @classmethod
    def from_dict(cls, data: dict) -> Track:
        return cls(
            id=data["id"],
            title=data["title"],
            duration=data["duration"],
            track_number=data["track_number"],
            media_number=data["media_number"],
            version=data.get("version"),
            isrc=data.get("isrc"),
            copyright=data.get("copyright"),
            performers=data.get("performers"),
            performer=_parse(Performer, data.get("performer")),
            composer=_parse(Composer, data.get("composer")),
            work=data.get("work"),
            audio_info=_parse(AudioInfo, data.get("audio_info")),
            album=_parse(TrackAlbum, data.get("album")),
            parental_warning=data.get("parental_warning", False),
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
