# kabooz/models/release.py
"""
Models for the /artist/getReleasesList endpoint.

Release is a richer representation of an album used specifically in the
release list endpoint. It differs from Album in several ways:
  - artist name comes as a nested display string rather than an object
  - rights (streamable/downloadable/hires) are in a sub-object
  - dates are a structured object with original/download/stream
  - pagination uses has_more (bool) rather than total/offset

The Release model is intentionally kept separate from Album so that the
Album model — which is heavily used throughout the rest of the codebase —
isn't contaminated with fields that only exist on release list responses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .common import _parse, _parse_list, Genre, Image, Label, AudioInfo


@dataclass
class ReleaseArtistName:
    display: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> ReleaseArtistName:
        return cls(display=data.get("display"))


@dataclass
class ReleaseArtist:
    name: Optional[ReleaseArtistName] = None

    @property
    def display_name(self) -> str:
        """Convenience: the artist's display name as a plain string."""
        return (self.name.display or "") if self.name else ""

    @classmethod
    def from_dict(cls, data: dict) -> ReleaseArtist:
        name_data = data.get("name")
        name = None
        if isinstance(name_data, dict):
            name = ReleaseArtistName.from_dict(name_data)
        elif isinstance(name_data, str):
            # Some responses return name as a plain string rather than an object.
            name = ReleaseArtistName(display=name_data)
        return cls(name=name)


@dataclass
class ReleaseDates:
    original: Optional[str] = None    # ISO date string
    download: Optional[str] = None
    stream: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> ReleaseDates:
        return cls(
            original=data.get("original"),
            download=data.get("download"),
            stream=data.get("stream"),
        )


@dataclass
class ReleaseRights:
    purchasable:      bool = False
    streamable:       bool = False
    downloadable:     bool = False
    hires_streamable: bool = False
    hires_purchasable: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> ReleaseRights:
        return cls(
            purchasable=data.get("purchasable", False),
            streamable=data.get("streamable", False),
            downloadable=data.get("downloadable", False),
            hires_streamable=data.get("hires_streamable", False),
            hires_purchasable=data.get("hires_purchasable", False),
        )


@dataclass
class ReleasePhysicalSupport:
    media_number: Optional[int] = None
    track_number: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> ReleasePhysicalSupport:
        return cls(
            media_number=data.get("media_number"),
            track_number=data.get("track_number"),
        )


@dataclass
class ReleaseTrack:
    """A lightweight track as returned inside a Release."""
    id: Optional[str] = None
    title: Optional[str] = None
    version: Optional[str] = None
    duration: Optional[int] = None
    isrc: Optional[str] = None
    work: Optional[str] = None
    parental_warning: bool = False
    rights: Optional[ReleaseRights] = None
    physical_support: Optional[ReleasePhysicalSupport] = None
    audio_info: Optional[AudioInfo] = None

    @classmethod
    def from_dict(cls, data: dict) -> ReleaseTrack:
        return cls(
            id=data.get("id"),
            title=data.get("title"),
            version=data.get("version"),
            duration=data.get("duration"),
            isrc=data.get("isrc"),
            work=data.get("work"),
            parental_warning=data.get("parental_warning", False),
            rights=_parse(ReleaseRights, data.get("rights")),
            physical_support=_parse(ReleasePhysicalSupport, data.get("physical_support")),
            audio_info=_parse(AudioInfo, data.get("audio_info")),
        )


@dataclass
class ReleaseTrackList:
    has_more: bool = False
    items: list[ReleaseTrack] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> ReleaseTrackList:
        return cls(
            has_more=data.get("has_more", False),
            items=_parse_list(ReleaseTrack, data.get("items")),
        )


@dataclass
class Release:
    """
    A release as returned by /artist/getReleasesList.

    Structurally similar to Album but with key differences:
      - artist is a ReleaseArtist (name.display string) not AlbumArtist
      - rights is a sub-object, not scattered boolean fields
      - dates is a structured ReleaseDates object
      - tracks is a ReleaseTrackList (with has_more) not a TracksPage
    """
    id: Optional[str] = None
    title: Optional[str] = None
    version: Optional[str] = None
    release_type: Optional[str] = None
    parental_warning: bool = False
    artist: Optional[ReleaseArtist] = None
    dates: Optional[ReleaseDates] = None
    duration: Optional[int] = None
    tracks_count: Optional[int] = None
    genre: Optional[Genre] = None
    image: Optional[Image] = None
    label: Optional[Label] = None
    release_tags: list[str] = field(default_factory=list)
    rights: Optional[ReleaseRights] = None
    audio_info: Optional[AudioInfo] = None
    tracks: Optional[ReleaseTrackList] = None

    @property
    def display_title(self) -> str:
        t = (self.title or "").rstrip()
        if self.version:
            t += f" ({self.version})"
        return t

    @property
    def release_date(self) -> Optional[str]:
        """Convenience: original release date as a string, or None."""
        return self.dates.original if self.dates else None

    @classmethod
    def from_dict(cls, data: dict) -> Release:
        return cls(
            id=data.get("id"),
            title=data.get("title"),
            version=data.get("version"),
            release_type=data.get("release_type"),
            parental_warning=data.get("parental_warning", False),
            artist=_parse(ReleaseArtist, data.get("artist")),
            dates=_parse(ReleaseDates, data.get("dates")),
            duration=data.get("duration"),
            tracks_count=data.get("tracks_count"),
            genre=_parse(Genre, data.get("genre")),
            image=_parse(Image, data.get("image")),
            label=_parse(Label, data.get("label")),
            release_tags=data.get("release_tags", []),
            rights=_parse(ReleaseRights, data.get("rights")),
            audio_info=_parse(AudioInfo, data.get("audio_info")),
            tracks=_parse(ReleaseTrackList, data.get("tracks")),
        )


@dataclass
class ReleasesList:
    """
    Response from /artist/getReleasesList.

    Uses has_more (bool) for pagination rather than total/offset —
    this is the one endpoint where you advance by bumping offset until
    has_more is False rather than comparing offset to total.
    """
    has_more: bool = False
    items: list[Release] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> ReleasesList:
        return cls(
            has_more=data.get("has_more", False),
            items=_parse_list(Release, data.get("items")),
        )

