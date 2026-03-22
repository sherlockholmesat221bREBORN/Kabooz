# Installation

## Requirements

- Python 3.11 or newer
- An active [Qobuz](https://www.qobuz.com) subscription
- A Qobuz `app_id` and `app_secret` (see [Authentication](authentication.md))

## Install from PyPI

```bash
pip install 'kabooz[cli]'
```

The `[cli]` extra installs Typer and Rich, which are required to use the
`kabooz` command. Without them, the Python library still works but the CLI
will not start.

## Install for development

```bash
git clone https://github.com/youruser/kabooz.git
cd kabooz
pip install -e '.[cli,dev]'
```

The `-e` flag installs in editable mode — changes to source files take
effect immediately without reinstalling.

## Verify

```bash
kabooz --version
```

## Optional dependencies

| Feature | How to enable |
|---------|--------------|
| MusicBrainz tag enrichment | Set `musicbrainz.enabled = true` in config — no extra packages needed |
| Synced lyrics | Set `tagging.fetch_lyrics = true` in config — no extra packages needed |
| External downloader (aria2c, wget) | Install the tool separately; set `download.external_downloader` in config |

