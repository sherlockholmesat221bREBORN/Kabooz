# Python Library

Every operation available in the CLI is equally accessible from Python via
`QobuzSession`. The CLI is a thin presentation layer — the business logic
lives entirely in `session.py`.

---

## Quick start

```python
from kabooz import QobuzSession, Quality

# Use your saved config and session (same as the CLI)
sess = QobuzSession.from_config()

# Download an album
result = sess.download_album(
    "0093046758769",
    quality=Quality.FLAC_16,
    dest_dir="/storage/Music",
)
print(f"{result.succeeded} downloaded, {result.skipped} skipped")

# Fetch metadata
album = sess.get_album("0093046758769")
print(album.display_title, "—", album.artist.name)
for t in album.tracks.items:
    print(f"  {t.track_number:02d}. {t.display_title}  [{t.duration}s]")

# Search
results = sess.search("beethoven symphony 9", search_type="albums", limit=5)
for a in results.albums.items:
    print(a.id, a.display_title)
```

---

## Construction

### `QobuzSession.from_config()`

Load credentials and session from the saved config file. This is the standard
way to get a session — equivalent to what the CLI does on every command.

```python
from kabooz import QobuzSession

sess = QobuzSession.from_config()
# Or specify a custom config path:
sess = QobuzSession.from_config(config_path="~/my-config.toml")
```

### `QobuzSession.from_client()`

Construct a session from a `QobuzClient` you built manually. Useful when you
want full control over authentication.

```python
from kabooz import QobuzClient, QobuzSession

client = QobuzClient.from_credentials(app_id="...", app_secret="...")
client.login(username="me@example.com", password="secret")

sess = QobuzSession.from_client(client)
```

### Token pool

```python
client = QobuzClient.from_token_pool("~/.config/kabooz/pool.txt")
sess = QobuzSession.from_client(client)
```

---

## Metadata

### `sess.get_album(album_id, *, limit=1200)`

Returns a full `Album` object including its track listing.

```python
album = sess.get_album("0093046758769")

print(album.display_title)       # "Dixit Dominus (Deluxe Edition)"
print(album.title)               # "Dixit Dominus"
print(album.artist.name)         # "La Nuova Musica"
print(album.label.name)          # "Harmonia Mundi"
print(album.genre.name)          # "Classical"
print(album.release_date_original)  # "2019-03-08"
print(album.maximum_bit_depth)   # 24
print(album.maximum_sampling_rate)  # 96.0
print(album.tracks_count)        # 23
print(album.duration)            # 3540 (seconds)
print(album.hires)               # True
print(album.upc)                 # "3149020938249"

for goodie in album.goodies:
    print(goodie.name, goodie.url)

for track in album.tracks.items:
    print(track.track_number, track.display_title, track.isrc)
```

### `sess.get_track(track_id)`

```python
track = sess.get_track("12345678")

print(track.display_title)       # "Symphony No. 40 - I. Molto allegro"
print(track.title)               # "I. Molto allegro"
print(track.work)                # "Symphony No. 40"
print(track.version)             # "Live" or None
print(track.performer.name)      # "Vienna Philharmonic"
print(track.composer.name)       # "Wolfgang Amadeus Mozart"
print(track.duration)            # 432 (seconds)
print(track.track_number)        # 1
print(track.media_number)        # 1
print(track.isrc)                # "ATDD23456789"
```

### `sess.get_artist(artist_id, *, extras="albums", limit=50)`

```python
artist = sess.get_artist(298)

print(artist.name)               # "Daft Punk"
print(artist.albums_count)       # 12

for album in artist.albums.items:
    print(album.id, album.display_title, album.release_date_original[:4])
```

### `sess.get_playlist(playlist_id, *, limit=500)`

```python
pl = sess.get_playlist("12345")

print(pl.name)
print(pl.tracks_count)
for t in pl.tracks.items:
    print(t.id, t.title, t.playlist_track_id)
```

---

## Search

All search methods return typed result objects, not raw dicts.

### `sess.search(query, search_type="tracks", limit=25)`

```python
results = sess.search("daft punk", search_type="tracks", limit=10)

# results.tracks is a SearchPage
for track in results.tracks.items:
    print(track.id, track.display_title, track.performer.name)

print(results.tracks.total)   # total matches in catalog
print(results.tracks.has_more)  # True if more pages exist
```

`search_type` can be: `"tracks"`, `"albums"`, `"artists"`, `"playlists"`.

The session's `search()` method calls `client.search()` which returns a
`SearchResults` object. Accessing `results.tracks`, `results.albums`,
`results.artists`, or `results.playlists` gives a `SearchPage` with `.items`,
`.total`, `.limit`, `.offset`, and `.has_more`.

---

## Downloads

### `sess.download_track(url_or_id, *, quality, dest_dir, ...)`

```python
from kabooz import Quality
from pathlib import Path

result = sess.download_track(
    "12345678",
    quality=Quality.HI_RES,
    dest_dir=Path("/storage/Music"),
    embed_cover=True,
    fetch_lyrics_flag=True,
)

print(result.download.path)       # Path to the downloaded file
print(result.download.skipped)    # True if file was already complete
print(result.tagged)              # True if tagging ran
print(result.lyrics_found)        # True if lyrics were embedded
print(result.mb_enriched)         # True if MusicBrainz data was applied
```

### `sess.download_album(url_or_id, *, quality, dest_dir, ...)`

```python
def on_start(title, index, total):
    print(f"[{index}/{total}] {title}")

def on_done(result):
    if result.download.skipped:
        print("  skipped")

result = sess.download_album(
    "0093046758769",
    quality=Quality.FLAC_16,
    dest_dir=Path("/storage/Music"),
    embed_cover=True,
    fetch_lyrics_flag=False,
    download_goodies=True,
    on_track_start=on_start,
    on_track_done=on_done,
)

print(f"{result.succeeded} downloaded, {result.skipped} skipped")
print(f"{result.failed} failed")
print(f"{result.goodies_ok} goodies, {result.goodies_failed} failed")
```

### `sess.download_album_goodies(url_or_id, *, dest_dir, on_progress_each)`

```python
def on_progress(filename, done, total):
    pct = int(done / total * 100) if total else 0
    print(f"\r{filename}: {pct}%", end="")

results = sess.download_album_goodies(
    "0093046758769",
    dest_dir=Path("/storage/Music"),
    on_progress_each=on_progress,
)

for r in results:
    if r.ok:
        print(r.path.name, "skipped" if r.skipped else "downloaded")
    else:
        print(r.goodie.name, "FAILED:", r.error)
```

### `sess.download_artist_discography(url_or_id, *, release_type, quality, dest_dir, ...)`

```python
results = sess.download_artist_discography(
    "298",
    release_type="album",          # or "album,live" or None for all
    quality=Quality.FLAC_16,
    dest_dir=Path("/storage/Music"),
)

for album_result in results:
    print(album_result.album.display_title,
          album_result.succeeded, "tracks")
```

### `sess.download_playlist(url_or_id, *, quality, dest_dir, ...)`

```python
result = sess.download_playlist(
    "12345",
    quality=Quality.HI_RES,
    dest_dir=Path("/storage/Music"),
    write_m3u=True,
)
```

---

## Streaming (for players)

```python
# Resolve a CDN URL and report stream start to Qobuz
stream = sess.prepare_stream("12345678", quality=Quality.HI_RES)

print(stream.url)            # CDN URL — expires in ~30 minutes
print(stream.format_id)      # Qobuz format ID integer
print(stream.bit_depth)      # 24
print(stream.sampling_rate)  # 96.0
print(stream.mime_type)      # "audio/flac"

# Play stream.url with your player, then:
sess.report_stream_end(stream.track_id)

# Or if the track was skipped before finishing:
sess.report_stream_cancel(stream.track_id)
```

---

## Quality enum

```python
from kabooz import Quality

Quality.MP3_320      # format_id=5   MP3 320 kbps
Quality.FLAC_16      # format_id=6   FLAC 16-bit 44.1 kHz (CD)
Quality.FLAC_24_96   # format_id=7   FLAC 24-bit 96 kHz
Quality.HI_RES       # format_id=27  FLAC up to 24-bit 192 kHz
```

`Quality` is an `IntEnum`, so you can compare and cast:

```python
int(Quality.FLAC_16)    # 6
Quality(27)             # Quality.HI_RES
Quality.HI_RES > Quality.FLAC_16  # True
```

---

## Error handling

```python
from kabooz.exceptions import (
    QobuzError,           # base class — catches everything from Kabooz
    APIError,             # server responded with an error
    AuthError,            # base class for all auth errors
    TokenExpiredError,    # session token expired; re-login
    InvalidCredentialsError,
    NoAuthError,          # method called before login
    TokenPoolExhaustedError,  # all pool tokens failed
    PoolModeError,        # write attempted in pool mode
    NotFoundError,        # 404 from API
    NotStreamableError,   # track exists but cannot be streamed
    RateLimitError,       # 429 — back off and retry
    ConfigError,          # invalid config value
)

try:
    result = sess.download_track("12345678")
except TokenExpiredError:
    # Re-authenticate
    pass
except NotStreamableError as exc:
    print(f"Track not available: {exc}")
except APIError as exc:
    print(f"API error {exc.status_code}: {exc}")
except QobuzError as exc:
    print(f"Kabooz error: {exc}")
```

---

## Direct API access

For endpoints not wrapped by `QobuzSession`, the underlying `QobuzClient`
is accessible via `sess.client`:

```python
# Raw API call — returns a dict
data = sess.client._request("GET", "/genre/list", params={"parent_id": 113})
```

All `QobuzClient` methods are documented below:

| Method | Returns |
|--------|---------|
| `get_track(track_id)` | `Track` |
| `get_album(album_id, ...)` | `Album` |
| `get_artist(artist_id, ...)` | `Artist` |
| `get_playlist(playlist_id, ...)` | `Playlist` |
| `get_label(label_id, ...)` | `LabelDetail` |
| `get_track_url(track_id, quality)` | `dict` with `"url"` |
| `search(query, type, limit, offset)` | `SearchResults` |
| `search_tracks(query, ...)` | `TrackSearchResults` |
| `search_albums(query, ...)` | `AlbumSearchResults` |
| `search_artists(query, ...)` | `ArtistSearchResults` |
| `search_playlists(query, ...)` | `PlaylistSearchResults` |
| `get_user_favorites(...)` | `UserFavorites` |
| `get_favorite_ids(...)` | `UserFavoriteIds` |
| `get_user_info()` | `UserProfile` |
| `get_new_releases(...)` | `dict` |
| `get_featured_playlists(...)` | `dict` |
| `get_genres(...)` | `dict` |
| `iter_artist_albums(artist_id, ...)` | `Generator[Album]` |
| `iter_releases(artist_id, ...)` | `Generator[Release]` |
| `iter_favorites(type, ...)` | `Generator[Track \| Album \| Artist]` |
| `iter_playlist_tracks(playlist_id)` | `Generator[Track]` |

---

## Local store

The local SQLite store is accessible via `sess.store`:

```python
store = sess.store

# Favorites
store.add_favorite("12345", "track", title="...", artist="...")
store.remove_favorite("12345", "track")
items = store.get_favorites("track", limit=50)

# Playlists
pl_id = store.create_playlist("My List", "Description")
store.add_track_to_playlist(pl_id, "12345", title="...", artist="...")
tracks = store.get_playlist_tracks(pl_id)

# History
rows = store.get_history(limit=20)
store.clear_history()
```

