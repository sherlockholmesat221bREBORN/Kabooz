# Configuration

The configuration file lives at `~/.config/kabooz/config.toml`. It is created
automatically the first time you run `kabooz login`.

View the current configuration:

```bash
kabooz config --show
```

Set a value:

```bash
kabooz config --set section.key=value
```

Or edit the file directly in any text editor.

---

## Full reference

```toml
[credentials]
# Qobuz app ID. Also read from QOBUZ_APP_ID environment variable.
app_id = ""

# Qobuz app secret. Also read from QOBUZ_APP_SECRET environment variable.
app_secret = ""

# Path to a token pool file for pool mode. Leave empty for personal session.
pool = ""


[download]
# Default output directory for all downloads.
output_dir = "."

# Default quality level.
# Values: mp3_320 | flac_16 | flac_24_96 | hi_res
quality = "hi_res"

# Number of parallel track downloads within an album or playlist.
# 1 = sequential (default, safest). Higher values speed up multi-track
# downloads but increase API and CDN load.
max_workers = 1

# Shell command template for an external downloader (e.g. aria2c).
# Leave empty to use the built-in httpx downloader.
# Placeholders: {url} {output} {dir} {filename}
# {output} and {filename} refer to the .part path.
# Example: "aria2c -x 16 -s 16 -d {dir} -o {filename} {url}"
external_downloader = ""

# Timeout in seconds for reading data from the CDN.
# Large hi-res files on slow connections may need this increased.
read_timeout = 300.0

# Timeout in seconds for establishing a connection.
connect_timeout = 10.0


[tagging]
# Master switch. Set to false to skip all tagging.
enabled = true

# Download and embed full-resolution cover art in each audio file.
embed_cover = true

# Save cover art as a separate cover.jpg file in the album folder.
# For single-track downloads, the file is named after the audio file.
save_cover_file = false

# Fetch synced lyrics from LRCLIB and embed them in audio files.
# Writes LRC timestamps to SYNCEDLYRICS (FLAC) / SYLT (MP3) frames,
# and plain text to LYRICS (FLAC) / USLT (MP3) frames.
fetch_lyrics = false


[musicbrainz]
# Enrich tags with MusicBrainz data (recording IDs, release group IDs,
# additional relationships) using the track's ISRC code.
# Makes one extra HTTP request per track when enabled.
enabled = false


[naming]
# Naming templates control where downloaded files are placed and what
# they are named. See docs/naming.md for all available placeholders.

# Used by: kabooz album
album = "{album} [{quality}] [{year}]/{track:02d}. {title}"

# Used by: kabooz track (standalone), kabooz favorites --type tracks
single = "{artist} - {title}"

# Used by: kabooz album when release_type is "ep"
ep = "{albumartist}/{album} [{quality}] [{year}]/{track:02d}. {title}"

# Used by: kabooz album when release_type is "compilation"
compilation = "{album} [{quality}] [{year}]/{track:02d}. {title}"

# Used by: kabooz playlist
playlist = "{playlist} [{quality}]/{index:02d}. {title}"

# Used by: kabooz artist (the artist folder itself is injected by the
# session layer, so this template must NOT start with {albumartist})
artist = "{album} [{quality}] [{year}]/{track:02d}. {title}"


[local_data]
# Root directory for the local SQLite database, playlist TOML files,
# and export archives.
data_dir = "~/.local/share/kabooz"

# Log every download to the history table in the local database.
track_history = true

# On login (personal session only), pull all Qobuz favorites into
# the local store automatically.
auto_sync_favorites = false

# Subdirectory under data_dir for exported playlist TOML files.
# Relative paths are resolved against data_dir.
playlists_subdir = "playlists"
```

---

## Environment variables

| Variable | Equivalent config key |
|----------|----------------------|
| `QOBUZ_APP_ID` | `credentials.app_id` |
| `QOBUZ_APP_SECRET` | `credentials.app_secret` |
| `KABOOZ_DEV` | `--dev` flag |

Environment variables take precedence over config file values for credentials.

---

## Per-command overrides

Most config values can be overridden per-command without editing the file:

```bash
# Override quality for this run only
kabooz album 0093046758769 -q cd

# Override output directory
kabooz album 0093046758769 -o /external/drive/Music

# Override naming template
kabooz album 0093046758769 --template "{albumartist}/{album}/{track:02d}. {title}"

# Override worker count
kabooz album 0093046758769 -j 4
```

