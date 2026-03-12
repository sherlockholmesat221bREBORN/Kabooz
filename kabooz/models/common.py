# models/common.py

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional, Type, TypeVar

T = TypeVar("T")


def _parse(cls: Type[T], data: Any) -> Optional[T]:
    if not isinstance(data, dict):
        return None
    return cls.from_dict(data)


def _parse_list(cls: Type[T], data: Any) -> list[T]:
    if not isinstance(data, list):
        return []
    return [cls.from_dict(item) for item in data if isinstance(item, dict)]


@dataclass
class Performer:
    id: int
    name: str

    @classmethod
    def from_dict(cls, data: dict) -> Performer:
        return cls(id=data["id"], name=data["name"])


@dataclass
class Composer:
    id: int
    name: str
    slug: Optional[str] = None
    albums_count: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> Composer:
        return cls(
            id=data["id"],
            name=data["name"],
            slug=data.get("slug"),
            albums_count=data.get("albums_count"),
        )


@dataclass
class Label:
    id: int
    name: str
    slug: str
    supplier_id: int

    @classmethod
    def from_dict(cls, data: dict) -> Label:
        return cls(
            id=data["id"],
            name=data["name"],
            slug=data["slug"],
            supplier_id=data["supplier_id"],
        )


@dataclass
class Genre:
    id: int
    name: str
    slug: str
    path: list[Any] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> Genre:
        return cls(
            id=data["id"],
            name=data["name"],
            slug=data["slug"],
            path=data.get("path", []),
        )


@dataclass
class Image:
    large: Optional[str] = None
    small: Optional[str] = None
    thumbnail: Optional[str] = None
    back: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> Image:
        return cls(
            large=data.get("large"),
            small=data.get("small"),
            thumbnail=data.get("thumbnail"),
            back=data.get("back"),
        )


@dataclass
class Article:
    id: int
    price: float
    currency: str
    type: str
    label: str
    description: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> Article:
        return cls(
            id=data["id"],
            price=data["price"],
            currency=data["currency"],
            type=data["type"],
            label=data["label"],
            description=data.get("description"),
        )


@dataclass
class AudioInfo:
    replaygain_track_gain: float = 0.0
    replaygain_track_peak: float = 0.0

    @classmethod
    def from_dict(cls, data: dict) -> AudioInfo:
        return cls(
            replaygain_track_gain=data.get("replaygain_track_gain", 0.0),
            replaygain_track_peak=data.get("replaygain_track_peak", 0.0),
        )


@dataclass
class AlbumArtist:
    id: int
    name: str
    slug: Optional[str] = None
    albums_count: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> AlbumArtist:
        return cls(
            id=data["id"],
            name=data["name"],
            slug=data.get("slug"),
            albums_count=data.get("albums_count"),
        )


@dataclass
class AlbumArtistWithRoles:
    id: int
    name: str
    roles: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> AlbumArtistWithRoles:
        return cls(
            id=data["id"],
            name=data["name"],
            roles=data.get("roles", []),
        )
