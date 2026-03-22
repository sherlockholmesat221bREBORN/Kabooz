# kabooz/models/search.py
"""
Typed result containers for the Qobuz search endpoints.

Every search method on QobuzClient returns one of these instead of a raw
dict, so callers can navigate results the same way they navigate any other
model — via named attributes, not string-keyed dicts.

    results = client.search("beethoven symphony 9", type="albums")
    for album in results.albums.items:
        print(album.title, album.artist.name)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .track import Track
    from .album import Album
    from .artist import Artist
    from .playlist import Playlist


def _parse_list(cls, data: Any) -> list:
    if not isinstance(data, list):
        return []
    return [cls.from_dict(item) for item in data if isinstance(item, dict)]


@dataclass
class SearchPage:
    """
    A paginated collection of a single entity type within search results.

    Attributes:
        items:  The actual result objects (Track, Album, Artist, or Playlist).
        total:  Total matches in the catalog for this query (not just this page).
        limit:  Page size used for this request.
        offset: Starting offset of this page.
    """
    items: list = field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0

    @property
    def has_more(self) -> bool:
        """True when more results are available beyond this page."""
        return (self.offset + len(self.items)) < self.total


@dataclass
class SearchResults:
    """
    Combined results from ``client.search()``.

    Each attribute is a :class:`SearchPage` whose ``.items`` list contains
    the appropriate model objects.  A page is ``None`` when that type was
    not requested or not returned by the API.

    Example::

        results = client.search("daft punk", type="tracks", limit=10)
        for track in results.tracks.items:
            print(track.display_title, "—", track.performer.name)
    """
    tracks:    Optional[SearchPage] = None
    albums:    Optional[SearchPage] = None
    artists:   Optional[SearchPage] = None
    playlists: Optional[SearchPage] = None
    query:     str = ""

    @classmethod
    def from_dict(cls, data: dict) -> SearchResults:
        from .track import Track
        from .album import Album
        from .artist import Artist
        from .playlist import Playlist

        def _page(key: str, model_cls) -> Optional[SearchPage]:
            raw = data.get(key)
            if not isinstance(raw, dict):
                return None
            return SearchPage(
                items=_parse_list(model_cls, raw.get("items", [])),
                total=raw.get("total", 0),
                limit=raw.get("limit", 0),
                offset=raw.get("offset", 0),
            )

        return cls(
            tracks=_page("tracks", Track),
            albums=_page("albums", Album),
            artists=_page("artists", Artist),
            playlists=_page("playlists", Playlist),
            query=data.get("query", ""),
        )


@dataclass
class TrackSearchResults:
    """Results from ``client.search_tracks()``."""
    items:  list = field(default_factory=list)
    total:  int = 0
    limit:  int = 0
    offset: int = 0
    query:  str = ""

    @property
    def has_more(self) -> bool:
        return (self.offset + len(self.items)) < self.total

    @classmethod
    def from_dict(cls, data: dict) -> TrackSearchResults:
        from .track import Track
        raw = data.get("tracks", data)
        return cls(
            items=_parse_list(Track, raw.get("items", [])),
            total=raw.get("total", 0),
            limit=raw.get("limit", 0),
            offset=raw.get("offset", 0),
            query=data.get("query", ""),
        )


@dataclass
class AlbumSearchResults:
    """Results from ``client.search_albums()``."""
    items:  list = field(default_factory=list)
    total:  int = 0
    limit:  int = 0
    offset: int = 0
    query:  str = ""

    @property
    def has_more(self) -> bool:
        return (self.offset + len(self.items)) < self.total

    @classmethod
    def from_dict(cls, data: dict) -> AlbumSearchResults:
        from .album import Album
        raw = data.get("albums", data)
        return cls(
            items=_parse_list(Album, raw.get("items", [])),
            total=raw.get("total", 0),
            limit=raw.get("limit", 0),
            offset=raw.get("offset", 0),
            query=data.get("query", ""),
        )


@dataclass
class ArtistSearchResults:
    """Results from ``client.search_artists()``."""
    items:  list = field(default_factory=list)
    total:  int = 0
    limit:  int = 0
    offset: int = 0
    query:  str = ""

    @property
    def has_more(self) -> bool:
        return (self.offset + len(self.items)) < self.total

    @classmethod
    def from_dict(cls, data: dict) -> ArtistSearchResults:
        from .artist import Artist
        raw = data.get("artists", data)
        return cls(
            items=_parse_list(Artist, raw.get("items", [])),
            total=raw.get("total", 0),
            limit=raw.get("limit", 0),
            offset=raw.get("offset", 0),
            query=data.get("query", ""),
        )


@dataclass
class PlaylistSearchResults:
    """Results from ``client.search_playlists()``."""
    items:  list = field(default_factory=list)
    total:  int = 0
    limit:  int = 0
    offset: int = 0
    query:  str = ""

    @property
    def has_more(self) -> bool:
        return (self.offset + len(self.items)) < self.total

    @classmethod
    def from_dict(cls, data: dict) -> PlaylistSearchResults:
        from .playlist import Playlist
        raw = data.get("playlists", data)
        return cls(
            items=_parse_list(Playlist, raw.get("items", [])),
            total=raw.get("total", 0),
            limit=raw.get("limit", 0),
            offset=raw.get("offset", 0),
            query=data.get("query", ""),
        )
