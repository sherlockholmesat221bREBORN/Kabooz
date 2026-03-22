# Architecture

## Directory structure

```
kabooz/
├── __init__.py         Public API — re-exports the main classes
├── client.py           Raw Qobuz API wrapper
├── session.py          Business logic — the only place it lives
├── cli.py              CLI presentation layer
├── tui.py              TUI (Textual-based)
├── tui_curses.py       TUI (curses fallback)
├── config.py           TOML config read/write
├── quality.py          Quality IntEnum
├── exceptions.py       Exception hierarchy
├── url.py              Qobuz URL parser
├── dev.py              Developer mode utilities
│
├── auth/
│   ├── credentials.py  AppCredentials, TokenPool
│   └── session.py      AuthSession (token + user ID)
│
├── download/
│   ├── downloader.py   HTTP downloader with .part convention
│   ├── tagger.py       FLAC/MP3 metadata writer
│   ├── naming.py       Filename templates, path resolution
│   ├── lyrics.py       LRCLIB lyrics fetcher
│   ├── musicbrainz.py  MusicBrainz ISRC enrichment
│   └── credits.py      Performer credit string parser
│
├── models/
│   ├── common.py       Shared sub-types (Performer, Label, Image, ...)
│   ├── track.py        Track, TrackAlbum
│   ├── album.py        Album, TrackSummary, TracksPage, Goodie
│   ├── artist.py       Artist, ArtistAlbumList, Biography
│   ├── playlist.py     Playlist, PlaylistTrack
│   ├── release.py      Release, ReleasesList (from /artist/getReleasesList)
│   ├── favorites.py    UserFavorites, UserFavoriteIds, LabelDetail
│   ├── user.py         UserProfile, UserCredential
│   └── search.py       SearchResults and per-type result containers
│
└── local/
    ├── store.py        SQLite library database
    ├── playlist.py     LocalPlaylist TOML format
    └── export.py       Backup/restore, TOML import/export
```

---

## Layering rules

The single most important architectural rule is:

> **Business logic lives in `session.py` only.**

`cli.py` and `tui.py` are presentation layers. They call `QobuzSession`
methods and render results. They never:

- Import from `client.py` directly (except inside `login()`, where constructing
  a `QobuzClient` is inherently a CLI concern)
- Call `sess.client.anything` to bypass the session layer
- Duplicate quality-parsing, URL-resolution, or download logic

This means every operation triggered from the CLI is equally callable from a
Python script with identical behaviour.

---

## Data flow: downloading a track

```
kabooz track URL
    │
    ▼
cli.py: track()
    │  resolves quality, calls _session()
    ▼
session.py: download_track()
    │  resolves ID via parse_url()
    │  fetches Track via client.get_track()
    │  resolves CDN URL via client.get_track_url()
    ▼
download/downloader.py: Downloader.download_track()
    │  resolves dest path via naming.resolve_track_path()
    │  checks final path — skips if exists
    │  checks .part path — resumes if partial
    │  streams bytes from CDN → writes to <dest>.part
    │  returns DownloadResult with path=<dest>.part
    ▼
session.py: _post_download()
    │  optionally fetches lyrics via lyrics.LRCLibProvider
    │  tags <dest>.part via tagger.Tagger
    │  optionally enriches via musicbrainz.apply_mb_tags
    │  renames <dest>.part → <dest>  (_finalise)
    │  optionally saves cover.jpg
    │  logs to local store if track_history=true
    ▼
cli.py: prints result
```

---

## The .part convention

Downloads are written to `<final_path>.part` and only renamed to the final
path after the full post-download pipeline (tagging, MusicBrainz, cover art)
completes successfully.

This means:

- `final_path.exists()` is an unambiguous "fully complete" signal
- If a download or tagging step crashes, the `.part` file remains on disk
- On the next run, Kabooz detects the complete `.part` file, skips the
  download, and retries the tagging pipeline automatically
- Goodies (bonus files) bypass this convention — they are not tagged, so
  direct-write with size-based skip/resume is unambiguous

---

## Authentication modes

```
QobuzClient.from_credentials()   Personal session
    │  login(username, password) → AuthSession saved to session.json
    │  All operations available

QobuzClient.from_token_pool()    Pool mode
    │  TokenPool rotates through a list of tokens
    │  Validates each token with a catalog call
    │  pool.next_token() advances on expiry
    │  Write operations raise PoolModeError
```

The `_guard_write()` method on `QobuzClient` enforces the pool-mode read-only
constraint. It is called at the top of every method that modifies account
state (favorites, playlists, profile).

---

## Model design

All API responses are parsed into typed Python dataclasses. Every model has:

- A `from_dict(data: dict)` class method that constructs it from a raw API
  response dict
- Optional fields use `Optional[T]` with `.get()` in `from_dict()` — no
  `KeyError` on partial responses
- `display_title` properties on `Track` and `Album` that compose the human-
  readable title (including work prefix and version suffix)

Search results return typed containers (`SearchResults`, `TrackSearchResults`,
etc.) rather than raw dicts. Lookups return the specific model type
(`get_album()` returns `Album`, not `dict`).

---

## Local store

The SQLite database (`library.db`) stores four tables:

| Table | Contents |
|-------|----------|
| `favorites` | Locally saved tracks, albums, artists with metadata |
| `playlists` | Local playlist headers (name, description, timestamps) |
| `playlist_tracks` | Track entries within a playlist, ordered by position |
| `history` | Per-track download/playback log with timestamps |

All primary keys are `TEXT` (Qobuz IDs can be integers or strings depending
on the entity type and API endpoint — using TEXT avoids type mismatch bugs).

The store is entirely local and works in all modes including token pool mode.
Syncing it with the Qobuz account (`library sync`) requires a personal session.

---

## Circular import avoidance

`session.py` imports `QobuzClient` from `client.py` at the top level — this
is intentional and correct. The cycle that was previously present was:

```
client.py → models/search.py → models/__init__.py → (back to client.py)
```

This is resolved by importing the search model types lazily inside the search
methods rather than at module level:

```python
# client.py — search methods
def search(self, ...) -> "SearchResults":
    from .models.search import SearchResults   # lazy import
    ...
```

`cli.py` imports `QobuzClient` lazily inside `login()` only, since that is
the only CLI function that constructs a client directly.

