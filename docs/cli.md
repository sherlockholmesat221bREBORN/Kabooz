# CLI Reference

## Global options

These options work on the root `kabooz` command, before any subcommand.

```
kabooz [OPTIONS] COMMAND [ARGS]

Options:
  --version, -V   Show version and exit
  --credits       Show credits and acknowledgements
  --dev           Developer mode (see below)
  --help          Show help
```

### Developer mode

`--dev` (or `KABOOZ_DEV=1`) enables two things:

- **API response caching** — responses are saved to disk and replayed on
  subsequent runs. Useful when building scripts without hammering the API.
- **Dev audio** — downloads write a short real audio clip instead of the
  actual track. The full tagging pipeline still runs, so you can test
  metadata without downloading full files.

Cache location: shown in the dev mode banner. Clear with `kabooz dev-cache --clear`.

---

## Authentication

### `kabooz login`

Authenticate with Qobuz and save a session.

```
kabooz login [OPTIONS]

Options:
  -u, --username TEXT      Qobuz email address
  -p, --password TEXT      Password (prompted if omitted, input hidden)
  --token TEXT             Use a pre-existing auth token
  --user-id TEXT           User ID (required with --token)
  --pool PATH              Path to a token pool file
  --app-id TEXT            Qobuz app ID
  --app-secret TEXT        Qobuz app secret (input hidden)
  --help                   Show help
```

See [Authentication](authentication.md) for full details.

---

## Configuration

### `kabooz config`

View or edit the configuration file.

```
kabooz config [OPTIONS]

Options:
  --show          Print current config as JSON
  --set KEY=VAL   Set a config value (section.key=value format)
  --help          Show help
```

**Examples:**

```bash
kabooz config --show
kabooz config --set download.quality=cd
kabooz config --set download.max_workers=4
kabooz config --set tagging.fetch_lyrics=true
kabooz config --set tagging.save_cover_file=true
kabooz config --set download.output_dir=/storage/Music
```

See [Configuration](configuration.md) for all available keys.

---

## Downloads

All download commands accept a Qobuz URL or a bare numeric ID:

```bash
kabooz album https://open.qobuz.com/album/0093046758769
kabooz album 0093046758769          # same thing
```

### `kabooz track`

Download a single track.

```
kabooz track URL_OR_ID [OPTIONS]

Arguments:
  URL_OR_ID       Qobuz track URL or bare track ID

Options:
  -o PATH                        Output directory (default: config value)
  -q QUALITY                     Quality level (default: hires)
  --lyrics / --no-lyrics         Fetch and embed synced lyrics from LRCLIB
  --cover / --no-cover           Embed cover art in the audio file
  --save-cover / --no-save-cover Save cover.jpg alongside the audio file
  -j INT                         Number of worker threads
  --template TEXT                Custom naming template (see Naming)
  --help                         Show help
```

**Examples:**

```bash
kabooz track https://open.qobuz.com/track/12345678
kabooz track 12345678 -q cd -o ~/Music
kabooz track 12345678 --lyrics --save-cover
```

### `kabooz album`

Download a full album. Accepts all options that `track` accepts, plus:

```
Options:
  --goodies / --no-goodies   Download bonus files (PDFs, videos) — default: yes
```

**Examples:**

```bash
kabooz album https://open.qobuz.com/album/0093046758769
kabooz album 0093046758769 -q flac -j 3 --lyrics
kabooz album 0093046758769 --no-goodies
```

### `kabooz artist`

Download an artist's full discography. All albums are placed under
`<output>/<ArtistName>/`.

```
kabooz artist URL_OR_ID [OPTIONS]

Options:
  -o PATH          Output directory
  -q QUALITY       Quality level
  -t, --type TEXT  Release type filter. Values: album, live, compilation,
                   epSingle, other, download. Combine with commas.
  -j INT           Worker threads
  --template TEXT  Naming template
  --help           Show help
```

**Examples:**

```bash
kabooz artist https://open.qobuz.com/artist/298
kabooz artist 298 --type album -q cd
kabooz artist 298 --type album,live
```

### `kabooz playlist`

Download a Qobuz playlist.

```
kabooz playlist URL_OR_ID [OPTIONS]

Options:
  -o, -q, --lyrics, --cover, --save-cover, -j, --template   (same as track)
  --m3u    Write an M3U8 playlist file alongside the audio
  --help   Show help
```

### `kabooz favorites`

Download all favorited tracks or albums.

```
kabooz favorites [OPTIONS]

Options:
  -t, --type TEXT   tracks | albums  (default: tracks)
  -o, -q, -j        (same as track)
  --help            Show help
```

### `kabooz purchases`

Download all purchased albums or tracks.

```
kabooz purchases [OPTIONS]

Options:
  -t, --type TEXT   albums | tracks  (default: albums)
  -o, -q, -j        (same as track)
  --help            Show help
```

---

## Metadata

### `kabooz info`

Display metadata for an album, track, or artist without downloading anything.

```
kabooz info URL_OR_ID [OPTIONS]

Arguments:
  URL_OR_ID       Qobuz URL or bare ID

Options:
  -t, --type TEXT   Entity type when using a bare ID: album | track | artist
  --json            Output raw JSON (dataclass serialization)
  --help            Show help
```

For albums: shows a header panel with artist, label, genre, year, format,
duration, goodies inventory, and availability flags, followed by a full
track listing with durations and ISRC codes.

For tracks: shows title, performer, composer, work, album, format, ISRC, and
availability flags.

For artists: shows name, album count, and a full release table with IDs,
titles, years, and release types.

**Examples:**

```bash
kabooz info https://open.qobuz.com/album/0093046758769
kabooz info 0093046758769 --type album
kabooz info https://open.qobuz.com/track/12345678
kabooz info 298 --type artist
kabooz info 298 --type artist --json
```

### `kabooz goodies`

Download only the bonus files (booklets, videos) for an album. Audio tracks
are not downloaded.

```
kabooz goodies URL_OR_ID [OPTIONS]

Options:
  -o PATH   Output directory
  --help    Show help
```

Files are placed in `<output>/<Album [Format] [Year]>/` — the same folder
that `kabooz album` would create, so bonus files land next to any
already-downloaded audio.

When two goodies have the same name (common for duplicate booklets in
different qualities), they are automatically deduplicated: the second gets
` (2)` appended before the extension.

---

## Search

### `kabooz search`

Search the Qobuz catalog.

```
kabooz search QUERY [OPTIONS]

Arguments:
  QUERY           Search string

Options:
  -t, --type TEXT   tracks | albums | artists | playlists  (default: tracks)
  -n INT            Maximum results  (default: 10)
  --json            Output raw JSON
  --help            Show help
```

**Examples:**

```bash
kabooz search "handel dixit dominus"
kabooz search "handel" --type albums -n 25
kabooz search "daft punk" --type artists
```

---

## Discovery

### `kabooz new-releases`

Browse new or featured album releases.

```
Options:
  -t, --type TEXT    Release feed type. Common values:
                       new-releases, new-releases-full, press-awards,
                       editor-picks, most-streamed, best-sellers
                     (default: new-releases)
  --genre INT        Filter by genre ID
  -n INT             Maximum results (default: 25)
```

### `kabooz featured`

Browse editorially curated playlists.

```
Options:
  -t, --type TEXT   Curation type: editor-picks, last-created, best-of
                    (default: editor-picks)
  --genre INT       Filter by genre ID
  -n INT            Maximum results (default: 25)
```

### `kabooz genres`

List Qobuz genres.

```
Options:
  --parent INT   List sub-genres of this genre ID
```

---

## Local library

The local library is a SQLite database at `~/.local/share/kabooz/library.db`.
It stores favorites and playlists locally, independent of your Qobuz account.
All library commands work in token pool mode.

### `kabooz library show`

```
Options:
  -t, --type TEXT   all | track | album | artist  (default: all)
  -n INT            Maximum results per type (default: 50)
```

### `kabooz library add`

```
kabooz library add URL_OR_ID [OPTIONS]

Options:
  -t, --type TEXT            track | album | artist  (default: track)
  --remote / --local-only    Also add to your Qobuz account (default: local only)
```

### `kabooz library remove`

```
kabooz library remove URL_OR_ID [OPTIONS]

Options:
  -t, --type TEXT            track | album | artist  (default: track)
  --remote / --local-only    Also remove from your Qobuz account
```

### `kabooz library sync`

Pull favorites from your Qobuz account into the local store.

```
Options:
  -t, --type TEXT   all | track | album | artist  (default: all)
  --clear           Replace local favorites instead of merging
```

Requires a personal session (not pool mode).

### `kabooz library history`

```
Options:
  -n INT      Maximum entries (default: 20)
  --clear     Delete all history
```

---

## Local playlists (`lpl`)

Local playlists are stored in the SQLite database. They can be shared as TOML
files and imported by other Kabooz users.

```bash
kabooz lpl list
kabooz lpl create "Playlist Name" --desc "Optional description"
kabooz lpl show "Playlist Name"
kabooz lpl add "Playlist Name" TRACK_URL_OR_ID
kabooz lpl remove "Playlist Name" TRACK_ID
kabooz lpl delete "Playlist Name" --yes
kabooz lpl clone QOBUZ_PLAYLIST_URL [--name "Custom Name"]
kabooz lpl download "Playlist Name" [-o DIR] [-q QUALITY] [-j WORKERS]
kabooz lpl share "Playlist Name" [-o output.toml] [--author "Name"]
kabooz lpl import playlist.toml [--overwrite]
```

---

## Qobuz account playlists (`remote`)

Manage playlists directly on your Qobuz account. Requires a personal session.

```bash
kabooz remote list
kabooz remote show PLAYLIST_ID [-n 50]
kabooz remote create "Name" [--desc "..."] [--public] [--collaborative]
kabooz remote update PLAYLIST_ID [--name "..."] [--desc "..."] [--public/--private]
kabooz remote delete PLAYLIST_ID --yes
kabooz remote add PLAYLIST_ID TRACK_ID [TRACK_ID ...]
kabooz remote remove PLAYLIST_ID PLAYLIST_TRACK_ID [...]  # PT-ID from 'remote show'
kabooz remote follow PLAYLIST_ID
kabooz remote unfollow PLAYLIST_ID --yes
```

Note: `remote remove` takes the *playlist track ID* (the PT-ID column in
`remote show`), not the track ID. This is the join-table ID that Qobuz uses
to identify a specific track's position in a specific playlist.

---

## Account

```bash
kabooz account show              # Profile and subscription info
kabooz account show --json       # Raw JSON
kabooz account subscription      # Subscription tier and capabilities
kabooz account update [--email ...] [--firstname ...] [--lastname ...]
                      [--display-name ...] [--country XX] [--language XX]
                      [--newsletter / --no-newsletter]
kabooz account password          # Change password (prompted)
```

---

## Export and backup

```bash
kabooz export backup                        # Full .tar.gz backup
kabooz export backup -o ~/my-backup.tar.gz
kabooz export favorites                     # Export favorites as TOML
kabooz export favorites --type track
kabooz export import-favorites file.toml    # Import favorites from TOML
kabooz export import-favorites file.toml --replace  # Replace instead of merge
kabooz export restore backup.tar.gz
kabooz export restore backup.tar.gz --no-favorites --db  # Restore DB only
```

