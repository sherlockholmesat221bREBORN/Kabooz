# tests/test_models.py
from kabooz.models import Track, Album, Artist, Playlist

TRACK_PAYLOAD = {
    "id": 12345,
    "title": "One More Time",
    "duration": 320,
    "track_number": 1,
    "media_number": 1,
    "isrc": "GBDCE0000001",
    "performers": "Daft Punk, performer",
    "performer": {"id": 999, "name": "Daft Punk"},
    "composer": {"id": 888, "name": "Thomas Bangalter"},
    "work": None,
    "maximum_bit_depth": 24,
    "maximum_sampling_rate": 96.0,
    "purchasable": True,
    "streamable": True,
    "previewable": True,
    "downloadable": True,
    "hires": True,
    "album": {
        "id": "abc123",
        "title": "Discovery",
        "artist": {"id": 999, "name": "Daft Punk", "slug": "daft-punk"},
        "image": {
            "large": "https://example.com/large.jpg",
            "small": "https://example.com/small.jpg",
            "thumbnail": "https://example.com/thumb.jpg",
        },
        "hires": True,
        "streamable": True,
        "purchasable": True,
        "previewable": True,
        "downloadable": True,
    }
}

ALBUM_PAYLOAD = {
    "id": "abc123",
    "qobuz_id": 9999,
    "title": "Discovery",
    "subtitle": "Remastered",
    "artist": {"id": 999, "name": "Daft Punk", "slug": "daft-punk"},
    "artists": [{"id": 999, "name": "Daft Punk", "roles": ["MainArtist"]}],
    "label": {"id": 1, "name": "Virgin", "slug": "virgin", "supplier_id": 5},
    "genre": {"id": 10, "name": "Electronic", "slug": "electronic", "path": []},
    "genres_list": ["Electronic"],
    "image": {
        "large": "https://example.com/large.jpg",
        "small": "https://example.com/small.jpg",
        "thumbnail": "https://example.com/thumb.jpg",
        "back": None,
    },
    "upc": "724384960224",
    "duration": 3540,
    "tracks_count": 14,
    "media_count": 1,
    "maximum_bit_depth": 24,
    "maximum_sampling_rate": 96.0,
    "maximum_channel_count": 2,
    "release_date_original": "2001-03-07",
    "hires": True,
    "hires_streamable": True,
    "purchasable": True,
    "streamable": True,
    "previewable": True,
    "downloadable": True,
    "tracks": {
        "offset": 0,
        "limit": 50,
        "total": 14,
        "items": [
            {"id": 11111, "title": "One More Time",
             "track_number": 1, "media_number": 1, "duration": 320}
        ]
    }
}

ARTIST_PAYLOAD = {
    "id": 999,
    "name": "Daft Punk",
    "slug": "daft-punk",
    "image": None,
    "picture": None,
    "biography": {
        "summary": "French electronic duo.",
        "content": "Daft Punk were a French electronic music duo...",
        "source": "wikipedia",
        "language": "en",
    },
    "albums_as_primary_artist_count": 5,
    "albums_as_primary_composer_count": 1,
    "albums_count": 8,
    "similar_artist_ids": [111, 222, 333],
    "information": None,
}

ARTIST_WITH_ALBUMS_PAYLOAD = {
    **ARTIST_PAYLOAD,
    "albums": {
        "offset": 0,
        "limit": 25,
        "total": 2,
        "items": [
            {
                "id": "abc123",
                "title": "Discovery",
                "duration": 3540,
                "release_date_original": "2001-03-07",
                "maximum_bit_depth": 16,
                "maximum_sampling_rate": 44.1,
                "tracks_count": 14,
                "media_count": 1,
                "artist": {"id": 999, "name": "Daft Punk", "slug": "daft-punk"},
                "label": {"id": 1, "name": "Virgin", "slug": "virgin", "supplier_id": 5},
                "genre": {"id": 10, "name": "Electronic", "slug": "electronic", "path": []},
                "image": {"large": "https://example.com/large.jpg", "small": "https://example.com/small.jpg", "thumbnail": "https://example.com/thumb.jpg", "back": None},
            },
        ],
    }
}

PLAYLIST_PAYLOAD = {
    "id": 42,
    "name": "My Favourites",
    "description": "A curated mix",
    "tracks_count": 2,
    "duration": 640,
    "is_public": True,
    "is_collaborative": False,
    "users_count": 1,
    "created_at": 1700000000,
    "updated_at": 1700001000,
    "owner": {"id": 7, "name": "maxxx"},
    "genres": [
        {"id": 10, "name": "Electronic", "slug": "electronic", "color": "#abc123", "path": [], "percent": 100.0}
    ],
    "images": [
        "https://example.com/50.jpg",
        "https://example.com/150.jpg",
        "https://example.com/300.jpg",
    ],
    "tracks": {
        "offset": 0,
        "limit": 50,
        "total": 2,
        "items": [
            {
                "id": 12345,
                "title": "One More Time",
                "duration": 320,
                "track_number": 1,
                "media_number": 1,
                "position": 0,
                "playlist_track_id": 9001,
                "version": None,
                "performers": "Daft Punk, performer",
                "copyright": "2001 Virgin",
                "sampleable": True,
                "displayable": True,
                "purchasable": True,
                "streamable": True,
                "downloadable": True,
                "previewable": True,
                "purchasable_at": 1700000000,
                "streamable_at": 1700000000,
                "maximum_sampling_rate": 44.1,
                "maximum_bit_depth": 16,
                "hires": False,
                "created_at": 1700000000,
                "performer": {"id": 999, "name": "Daft Punk"},
                "album": {
                    "id": "abc123",
                    "title": "Discovery",
                    "duration": 3540,
                    "tracks_count": 14,
                    "released_at": 984700800,
                    "media_count": 1,
                    "artist": {"id": 999, "name": "Daft Punk", "slug": "daft-punk"},
                    "label": {"id": 1, "name": "Virgin", "slug": "virgin", "supplier_id": 5},
                    "genre": {"id": 10, "name": "Electronic", "slug": "electronic", "path": []},
                    "image": {"thumbnail": "https://example.com/thumb.jpg", "small": "https://example.com/small.jpg", "large": "https://example.com/large.jpg", "back": None},
                    "purchasable": True,
                    "streamable": True,
                    "downloadable": True,
                    "previewable": True,
                    "hires": False,
                }
            }
        ]
    }
}


# ── Track tests ────────────────────────────────────────────────────────────

def test_track_parses_correctly():
    track = Track.from_dict(TRACK_PAYLOAD)
    assert track.id == 12345
    assert track.title == "One More Time"
    assert track.duration == 320


def test_track_nested_performer():
    track = Track.from_dict(TRACK_PAYLOAD)
    assert track.performer is not None
    assert track.performer.name == "Daft Punk"


def test_track_album_id_is_string():
    track = Track.from_dict(TRACK_PAYLOAD)
    assert isinstance(track.album.id, str)
    assert track.album.id == "abc123"


def test_track_work_is_nullable():
    track = Track.from_dict(TRACK_PAYLOAD)
    assert track.work is None


def test_track_minimal_payload():
    track = Track.from_dict({
        "id": 1, "title": "Test",
        "duration": 100, "track_number": 1, "media_number": 1,
    })
    assert track.isrc is None
    assert track.performer is None
    assert track.hires is False


def test_track_flags_are_bool():
    track = Track.from_dict(TRACK_PAYLOAD)
    assert track.streamable is True
    assert track.hires is True
    assert isinstance(track.purchasable, bool)


# ── Album tests ────────────────────────────────────────────────────────────

def test_album_parses_correctly():
    album = Album.from_dict(ALBUM_PAYLOAD)
    assert album.id == "abc123"
    assert album.title == "Discovery"
    assert album.tracks_count == 14


def test_album_id_is_string():
    album = Album.from_dict(ALBUM_PAYLOAD)
    assert isinstance(album.id, str)


def test_album_artists_with_roles():
    album = Album.from_dict(ALBUM_PAYLOAD)
    assert album.artists[0].name == "Daft Punk"
    assert "MainArtist" in album.artists[0].roles


def test_album_track_listing():
    album = Album.from_dict(ALBUM_PAYLOAD)
    assert album.tracks.total == 14
    assert album.tracks.items[0].title == "One More Time"


def test_album_image_back_nullable():
    album = Album.from_dict(ALBUM_PAYLOAD)
    assert album.image.back is None


def test_album_without_tracks():
    payload = {k: v for k, v in ALBUM_PAYLOAD.items() if k != "tracks"}
    album = Album.from_dict(payload)
    assert album.tracks is None


# ── Artist tests ───────────────────────────────────────────────────────────

def test_artist_parses_correctly():
    artist = Artist.from_dict(ARTIST_PAYLOAD)
    assert artist.id == 999
    assert artist.name == "Daft Punk"
    assert artist.slug == "daft-punk"


def test_artist_biography():
    artist = Artist.from_dict(ARTIST_PAYLOAD)
    assert artist.biography is not None
    assert artist.biography.summary == "French electronic duo."
    assert artist.biography.language == "en"


def test_artist_counts():
    artist = Artist.from_dict(ARTIST_PAYLOAD)
    assert artist.albums_count == 8
    assert artist.albums_as_primary_artist_count == 5


def test_artist_similar_ids():
    artist = Artist.from_dict(ARTIST_PAYLOAD)
    assert artist.similar_artist_ids == [111, 222, 333]


def test_artist_image_nullable():
    artist = Artist.from_dict(ARTIST_PAYLOAD)
    assert artist.image is None
    assert artist.picture is None


def test_artist_albums_none_without_extra():
    artist = Artist.from_dict(ARTIST_PAYLOAD)
    assert artist.albums is None


def test_artist_albums_with_extra():
    artist = Artist.from_dict(ARTIST_WITH_ALBUMS_PAYLOAD)
    assert artist.albums is not None
    assert artist.albums.total == 2
    assert artist.albums.items[0].title == "Discovery"


def test_artist_album_items_are_album_objects():
    artist = Artist.from_dict(ARTIST_WITH_ALBUMS_PAYLOAD)
    album = artist.albums.items[0]
    assert isinstance(album, Album)
    assert album.id == "abc123"


# ── Playlist tests ─────────────────────────────────────────────────────────

def test_playlist_parses_correctly():
    pl = Playlist.from_dict(PLAYLIST_PAYLOAD)
    assert pl.id == 42
    assert pl.name == "My Favourites"
    assert pl.tracks_count == 2


def test_playlist_owner():
    pl = Playlist.from_dict(PLAYLIST_PAYLOAD)
    assert pl.owner.id == 7
    assert pl.owner.name == "maxxx"


def test_playlist_genres():
    pl = Playlist.from_dict(PLAYLIST_PAYLOAD)
    assert len(pl.genres) == 1
    assert pl.genres[0].name == "Electronic"
    assert pl.genres[0].color == "#abc123"
    assert pl.genres[0].percent == 100.0


def test_playlist_images():
    pl = Playlist.from_dict(PLAYLIST_PAYLOAD)
    assert len(pl.images) == 3
    assert pl.images[0].endswith("50.jpg")


def test_playlist_track_listing():
    pl = Playlist.from_dict(PLAYLIST_PAYLOAD)
    assert pl.tracks is not None
    assert pl.tracks.total == 2
    assert pl.tracks.items[0].title == "One More Time"


def test_playlist_track_extra_fields():
    pl = Playlist.from_dict(PLAYLIST_PAYLOAD)
    track = pl.tracks.items[0]
    assert track.position == 0
    assert track.playlist_track_id == 9001
    assert track.sampleable is True
    assert track.displayable is True


def test_playlist_track_nested_album():
    pl = Playlist.from_dict(PLAYLIST_PAYLOAD)
    track = pl.tracks.items[0]
    assert track.album is not None
    assert track.album.title == "Discovery"
    assert track.album.id == "abc123"


def test_playlist_without_tracks():
    payload = {k: v for k, v in PLAYLIST_PAYLOAD.items() if k != "tracks"}
    pl = Playlist.from_dict(payload)
    assert pl.tracks is None


def test_playlist_flags():
    pl = Playlist.from_dict(PLAYLIST_PAYLOAD)
    assert pl.is_public is True
    assert pl.is_collaborative is False
