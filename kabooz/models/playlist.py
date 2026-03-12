# kabooz/models/playlist.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .common import _parse, _parse_list, Performer
from .track import TrackAlbum


@dataclass
class PlaylistOwner:
    id: int
    name: str

    @classmethod
    def from_dict(cls, data: dict) -> PlaylistOwner:
        return cls(id=data["id"], name=data["name"])


@dataclass
class PlaylistGenre:
    """Genre as returned inside a playlist — adds color and percent fields
    that the standard Genre in common.py does not have."""
    id: int
    name: str
    slug: Optional[str] = None
    color: Optional[str] = None
    path: list = field(default_factory=list)
    percent: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict) -> PlaylistGenre:
        return cls(
            id=data["id"],
            name=data["name"],
            slug=data.get("slug"),
            color=data.get("color"),
            path=data.get("path", []),
            percent=data.get("percent"),
        )


@dataclass
class PlaylistTrack:
    """A track as it appears inside a playlist response.

    Extends the core track fields with playlist-specific metadata:
    position in the playlist, playlist_track_id (the join table ID),
    sampleable and displayable flags, and purchasable_at / streamable_at
    timestamps.

    Uses TrackAlbum for the nested album — same shape as in Track.
    """
    id: int
    title: str
    duration: int
    track_number: int
    media_number: int
    position: Optional[int] = None
    playlist_track_id: Optional[int] = None
    version: Optional[str] = None
    performers: Optional[str] = None
    copyright: Optional[str] = None
    performer: Optional[Performer] = None
    album: Optional[TrackAlbum] = None
    maximum_bit_depth: Optional[int] = None
    maximum_sampling_rate: Optional[float] = None
    purchasable: bool = False
    streamable: bool = False
    downloadable: bool = False
    previewable: bool = False
    sampleable: bool = False
    displayable: bool = False
    hires: bool = False
    purchasable_at: Optional[int] = None
    streamable_at: Optional[int] = None
    created_at: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> PlaylistTrack:
        return cls(
            id=data["id"],
            title=data["title"],
            duration=data["duration"],
            track_number=data["track_number"],
            media_number=data["media_number"],
            position=data.get("position"),
            playlist_track_id=data.get("playlist_track_id"),
            version=data.get("version"),
            performers=data.get("performers"),
            copyright=data.get("copyright"),
            performer=_parse(Performer, data.get("performer")),
            album=_parse(TrackAlbum, data.get("album")),
            maximum_bit_depth=data.get("maximum_bit_depth"),
            maximum_sampling_rate=data.get("maximum_sampling_rate"),
            purchasable=data.get("purchasable", False),
            streamable=data.get("streamable", False),
            downloadable=data.get("downloadable", False),
            previewable=data.get("previewable", False),
            sampleable=data.get("sampleable", False),
            displayable=data.get("displayable", False),
            hires=data.get("hires", False),
            purchasable_at=data.get("purchasable_at"),
            streamable_at=data.get("streamable_at"),
            created_at=data.get("created_at"),
        )


@dataclass
class PlaylistTrackList:
    offset: int
    limit: int
    total: int
    items: list[PlaylistTrack] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> PlaylistTrackList:
        return cls(
            offset=data.get("offset", 0),
            limit=data.get("limit", 0),
            total=data.get("total", 0),
            items=[PlaylistTrack.from_dict(t) for t in data.get("items", [])],
        )


@dataclass
class Playlist:
    id: int
    name: str
    tracks_count: int
    duration: int
    is_public: bool
    is_collaborative: bool
    users_count: int
    created_at: int
    updated_at: int
    owner: PlaylistOwner
    description: Optional[str] = None
    genres: list[PlaylistGenre] = field(default_factory=list)
    # List of image URLs at different sizes (50px, 150px, 300px).
    images: list[str] = field(default_factory=list)
    # Only populated when tracks are included in the response.
    tracks: Optional[PlaylistTrackList] = None

    @classmethod
    def from_dict(cls, data: dict) -> Playlist:
        return cls(
            id=data["id"],
            name=data["name"],
            tracks_count=data["tracks_count"],
            duration=data["duration"],
            is_public=data.get("is_public", True),
            is_collaborative=data.get("is_collaborative", False),
            users_count=data.get("users_count", 0),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            owner=PlaylistOwner.from_dict(data["owner"]),
            description=data.get("description"),
            genres=_parse_list(PlaylistGenre, data.get("genres")),
            images=data.get("images", []),
            tracks=_parse(PlaylistTrackList, data.get("tracks")),
        )

