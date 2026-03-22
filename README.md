# Kabooz

Unofficial Qobuz CLI and Python library — download hi-res audio, manage your
local library, and browse the catalog from the command line or from a script.

```
kabooz album  https://open.qobuz.com/album/0093046758769
kabooz track  https://open.qobuz.com/track/12345678 -q cd --lyrics
kabooz artist https://open.qobuz.com/artist/298 --type album
kabooz info   https://open.qobuz.com/album/0093046758769
kabooz search "handel dixit dominus" --type albums
kabooz goodies https://open.qobuz.com/album/0093046758769
```

> Requires an active [Qobuz](https://www.qobuz.com) subscription.
> Not affiliated with or endorsed by Qobuz.

---

## Why Kabooz

Qobuz is one of the few streaming services that has genuinely cared about music
as an art form — hi-res lossless audio, a serious catalog with real depth, no
advertisements, and fair artist payouts. It deserves your subscription. Kabooz
exists because Qobuz is that good, and because a command-line interface to it
is a natural thing to want.

Beyond downloading, Kabooz manages a local library — favorites and playlists
stored in a local SQLite database that works even in token pool mode. It embeds
full metadata using [MusicBrainz](https://musicbrainz.org) for enrichment and
[LRCLIB](https://lrclib.net) for synced lyrics. The architecture is designed so
that every operation available in the CLI is equally callable from Python.

---

## Install

```bash
pip install 'kabooz[cli]'
```

See [docs/installation.md](docs/installation.md) for more, including editable
development installs.

---

## Quick start

**1. Authenticate**

```bash
kabooz login --app-id YOUR_APP_ID --app-secret YOUR_APP_SECRET \
             -u your@email.com
```

Kabooz will prompt for anything not supplied. Credentials are saved to
`~/.config/kabooz/config.toml` and the session token to
`~/.config/kabooz/session.json`.

See [docs/authentication.md](docs/authentication.md) for token pool mode and
other options.

**2. Download**

```bash
# Single track at maximum quality
kabooz track https://open.qobuz.com/track/12345678

# Full album at CD quality with synced lyrics
kabooz album https://open.qobuz.com/album/0093046758769 -q cd --lyrics

# Artist's full discography (albums only)
kabooz artist https://open.qobuz.com/artist/298 --type album

# Playlist
kabooz playlist https://open.qobuz.com/playlist/12345

# Bonus files only (booklets, PDFs, videos)
kabooz goodies https://open.qobuz.com/album/0093046758769
```

**3. Browse without downloading**

```bash
# Full album info — tracklist, format, goodies, ISRC codes
kabooz info https://open.qobuz.com/album/0093046758769

# Search
kabooz search "beethoven" --type albums -n 20

# New releases, editorial picks, genre tree
kabooz new-releases
kabooz featured --type editor-picks
kabooz genres
```

---

## Quality levels

| Flag | Format | Notes |
|------|--------|-------|
| `mp3` / `320` | MP3 320 kbps | Lossy |
| `cd` / `flac` / `lossless` | FLAC 16-bit 44.1 kHz | CD quality |
| `24bit` | FLAC 24-bit 96 kHz | High-res |
| `hires` / `best` / `max` | FLAC up to 24-bit 192 kHz | Maximum (default) |

The best quality your subscription supports is used automatically when the
requested quality exceeds your tier.

---

## Features

**Downloads**
- Tracks, albums, playlists, artist discographies, and favorites
- `.part` file convention — downloads are atomic; a file exists only when
  fully complete including tagging. Interrupted downloads resume automatically.
- Parallel downloads with configurable worker count (`-j`)
- External downloader support (aria2c, wget) via shell template

**Metadata**
- Full FLAC (Vorbis Comments) and MP3 (ID3v2.4) tagging
- Embedded full-resolution cover art
- Standalone `cover.jpg` option
- Synced lyrics from [LRCLIB](https://lrclib.net) — written to both LRC
  frames (for players that scroll lyrics) and plain text frames
- [MusicBrainz](https://musicbrainz.org) enrichment via ISRC lookup
- Performer credits, ReplayGain values, ISRC, UPC, composer, work title

**Naming templates**
- Fully configurable file and folder naming via template strings
- Separate templates for albums, singles, EPs, compilations, playlists,
  and artist discographies
- Placeholders for title, artist, album, year, quality, ISRC, label, genre,
  disc number, and more
- See [docs/naming.md](docs/naming.md) for the full reference

**Local library**
- SQLite database for favorites and playlists — works in all modes including
  token pool mode
- Local playlist creation, sharing as TOML files, and importing
- Sync favorites from your Qobuz account
- Download history log

**Account management**
- View profile and subscription tier
- Manage Qobuz account playlists (create, update, delete, follow, add/remove tracks)
- Update account fields, change password

**Python library**
- Every CLI operation is callable from Python via `QobuzSession`
- Typed models for all API responses — no raw dict access required
- Streaming URL resolution with automatic start/end reporting

---

## Configuration

Configuration lives at `~/.config/kabooz/config.toml`. The most commonly
changed settings:

```toml
[download]
output_dir   = "/storage/Music"
quality      = "hi_res"          # mp3_320 | flac_16 | flac_24_96 | hi_res
max_workers  = 1                 # parallel track downloads

[tagging]
embed_cover     = true
fetch_lyrics    = false          # set true to embed synced lyrics
save_cover_file = false          # set true to write cover.jpg

[musicbrainz]
enabled = false                  # set true to enrich tags via ISRC
```

```bash
kabooz config --show                         # view full config
kabooz config --set download.quality=cd      # update a value
kabooz config --set tagging.fetch_lyrics=true
```

Full reference: [docs/configuration.md](docs/configuration.md)

---

## Naming templates

By default albums are saved as:

```
Discovery [FLAC 24bit 96kHz] [2001]/01. One More Time.flac
```

This is controlled by naming templates in the config:

```toml
[naming]
album  = "{album} [{quality}] [{year}]/{track:02d}. {title}"
single = "{artist} - {title}"
```

Override per-command:

```bash
kabooz album 0093046758769 --template "{albumartist}/{album}/{track:02d}. {title}"
```

Full placeholder reference: [docs/naming.md](docs/naming.md)

---

## Python library

Every CLI operation is equally callable from Python:

```python
from kabooz import QobuzSession, Quality
from pathlib import Path

sess = QobuzSession.from_config()

# Metadata
album = sess.get_album("0093046758769")
print(album.display_title, "—", album.artist.name)
for t in album.tracks.items:
    print(f"  {t.track_number:02d}. {t.display_title}  {t.isrc}")

# Download
result = sess.download_album(
    "0093046758769",
    quality=Quality.FLAC_16,
    dest_dir=Path("/storage/Music"),
    fetch_lyrics_flag=True,
)
print(f"{result.succeeded} downloaded, {result.skipped} skipped")

# Search
results = sess.search("beethoven", search_type="albums", limit=10)
for a in results.albums.items:
    print(a.id, a.display_title, a.artist.name)

# Stream URL for a player
stream = sess.prepare_stream("12345678", quality=Quality.HI_RES)
# play stream.url, then:
sess.report_stream_end(stream.track_id)
```

Full library reference: [docs/library.md](docs/library.md)

---

## Architecture

```
kabooz/
├── client.py       Raw Qobuz API — HTTP, auth, all endpoints
├── session.py      Business logic — downloads, tagging, library
├── cli.py          CLI presentation layer
├── config.py       TOML config read/write
├── quality.py      Quality enum
├── exceptions.py   Exception hierarchy
├── auth/           Credentials, token pool, session persistence
├── download/       Downloader, tagger, lyrics, MusicBrainz, naming
├── models/         Typed dataclasses for every API response
└── local/          SQLite store, local playlists, export/import
```

The core rule: `cli.py` is presentation only. All business logic lives in
`session.py`, which means every CLI operation is equally a library call.

Full architecture notes: [docs/architecture.md](docs/architecture.md)

---

## Tinker with it

Kabooz is deliberately designed to be hackable.

The business logic lives in one file — `session.py`. The models are plain
dataclasses with `from_dict()` constructors. The CLI is a thin layer that
calls session methods and prints results. Adding a new feature means writing
a session method, wiring a CLI command, and adding a test.

Some things that would make Kabooz meaningfully better and are not yet done:

- **Async downloads** — the HTTP layer uses synchronous httpx; an async
  version would make parallel downloads faster without thread overhead
- **Better TUI** — the Textual-based TUI is incomplete
- **More MusicBrainz depth** — currently applies recording and release group
  IDs; much more is available per ISRC
- **Windows path handling** — the filename sanitizer works on Linux/macOS/Android;
  Windows has additional constraints not yet handled

If you find something broken, missing, or improvable — fix it. PRs, forks,
and feature branches are all welcome. The AGPL license keeps improvements in
the commons.

Read [docs/contributing.md](docs/contributing.md) to get started.

---

## Documentation

| | |
|--|--|
| [Installation](docs/installation.md) | Requirements, pip, Termux |
| [Authentication](docs/authentication.md) | Login, token pool, session management |
| [CLI reference](docs/cli.md) | Every command, every option |
| [Configuration](docs/configuration.md) | Full config file reference |
| [Naming templates](docs/naming.md) | All placeholders, examples |
| [Python library](docs/library.md) | API reference with code examples |
| [Architecture](docs/architecture.md) | How the code is organized |
| [Contributing](docs/contributing.md) | Where to add things, code style, tests |

---

## Credits

Kabooz would not exist without [Qobuz](https://www.qobuz.com) — the platform
it talks to, and genuinely one of the best things to happen to music in years.
Tag enrichment is powered by [MusicBrainz](https://musicbrainz.org). Synced
lyrics come from [LRCLIB](https://lrclib.net). Technical help from
[@tmxkwpn](https://t.me/tmxkwpn) on Telegram.

Full acknowledgements: [CREDITS.md](CREDITS.md)

---

## License

AGPL-3.0-or-later.

You can use it, study it, modify it, and distribute it. If you distribute a
modified version you must make the source available under the same terms.
