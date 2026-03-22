# Naming Templates

Kabooz uses template strings to determine where downloaded files are placed and
what they are named. Templates are configured in `config.toml` under `[naming]`
and can be overridden per-command with `--template`.

---

## Syntax

Templates use Python's `str.format_map` syntax: placeholders are wrapped in
`{curly braces}` and support format specifications.

```
{track:02d}    → zero-padded track number: 01, 02, ... 14
{year}         → plain year: 2001
{quality}      → quality string: FLAC 24bit 96kHz
```

Slashes in the template create subdirectories:

```
{album} [{quality}] [{year}]/{track:02d}. {title}
```

For an album called *Discovery* by Daft Punk released in 2001 at 24/96:

```
Discovery [FLAC 24bit 96kHz] [2001]/01. One More Time.flac
```

---

## Placeholders

### Track

| Placeholder | Value | Example |
|-------------|-------|---------|
| `{title}` | Display title — includes work prefix and version suffix | `Symphony No. 40 - I. Molto allegro (Live)` |
| `{raw_title}` | Track title as-is from API | `I. Molto allegro` |
| `{work}` | Classical work name | `Symphony No. 40` |
| `{version}` | Version/subtitle | `Live` |
| `{artist}` | Primary performing artist | `Daft Punk` |
| `{track}` | Track number (integer) | `1` |
| `{disc}` | Disc number (integer) | `2` |
| `{isrc}` | ISRC code | `GBDCE0000001` |

### Album

| Placeholder | Value | Example |
|-------------|-------|---------|
| `{album}` | Album display title — includes version suffix | `Discovery (Remastered)` |
| `{raw_album}` | Album title without version suffix | `Discovery` |
| `{albumartist}` | Album-level artist | `Daft Punk` |
| `{year}` | Release year | `2001` |
| `{label}` | Record label | `Virgin` |
| `{genre}` | Primary genre | `Electronic` |
| `{upc}` | Album UPC barcode | `724384960224` |

### Quality

| Placeholder | Value | Example |
|-------------|-------|---------|
| `{quality}` | Human-readable quality string | `FLAC 24bit 96kHz` |
| `{bit_depth}` | Bit depth integer | `24` |
| `{sampling_rate}` | Sampling rate float | `96.0` |

### Playlist

These are only populated for playlist downloads.

| Placeholder | Value | Example |
|-------------|-------|---------|
| `{playlist}` | Playlist name | `My Favourites` |
| `{index}` | Position in playlist (integer) | `3` |

---

## Default templates

```toml
[naming]
album       = "{album} [{quality}] [{year}]/{track:02d}. {title}"
single      = "{artist} - {title}"
ep          = "{albumartist}/{album} [{quality}] [{year}]/{track:02d}. {title}"
compilation = "{album} [{quality}] [{year}]/{track:02d}. {title}"
playlist    = "{playlist} [{quality}]/{index:02d}. {title}"
artist      = "{album} [{quality}] [{year}]/{track:02d}. {title}"
```

For `artist` downloads, the artist folder is injected by the session layer
before the template runs — so the result is `ArtistName/<template result>`.
Do not put `{albumartist}` at the start of the artist template or the artist
name will appear twice.

---

## Examples

### ISRC-based flat layout

```
{isrc}
```

All tracks land in the output directory named by their ISRC:

```
GBDCE0000001.flac
GBDCE0000002.flac
```

### Classical — work/movement structure

```
{albumartist}/{album} [{year}]/{work}/{track:02d}. {raw_title}
```

```
Herbert von Karajan/Beethoven Symphonies [1962]/Symphony No. 9/01. I. Allegro ma non troppo.flac
```

### Flat by artist

```
{artist}/{track:02d}. {title}
```

### Custom playlist layout

```
{playlist}/{index:03d} - {artist} - {title}
```

---

## File extension

The file extension (`.flac` or `.mp3`) is always appended automatically based
on the actual format returned by the Qobuz CDN. You do not include it in the
template.

---

## Unsafe characters

Characters that are illegal in filenames — `/`, `\`, `:`, `*`, `?`, `"`,
`<`, `>`, `|` — are automatically replaced with visually similar Unicode
lookalikes (e.g. `∕`, `∶`, `∗`) before the path is written to disk.
Control characters are stripped entirely.

---

## Missing placeholders

If a placeholder has no value for a particular track (e.g. `{work}` on a
non-classical track, or `{isrc}` when the API doesn't provide one), the
placeholder is replaced with an empty string. This may produce double spaces
or awkward punctuation — check your template handles missing values gracefully
if you use optional fields.

