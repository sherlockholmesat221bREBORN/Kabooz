# tests/test_models.py

from kabooz.models import Track, Album

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
