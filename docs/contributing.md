# Contributing

Kabooz is designed to be extended. The codebase has a deliberate structure
that makes adding features straightforward — read [Architecture](architecture.md)
first if you haven't already.

---

## Getting started

```bash
git clone https://github.com/youruser/kabooz.git
cd kabooz
pip install -e '.[cli,dev]'
```

The `-e` flag means changes to source files take effect immediately.

Run the test suite:

```bash
pytest
```

All 220+ tests should pass. If any fail before you change anything, open an
issue — that is a bug.

---

## Where to add things

### New CLI command

1. Write the business logic as a method on `QobuzSession` in `session.py`
2. Add the CLI command in `cli.py` that calls it and renders the result
3. The CLI command must never contain business logic — only argument handling
   and output formatting

### New API endpoint

1. Add a method to `QobuzClient` in `client.py`
2. If the response needs a typed model, add it to `models/`
3. Add a thin wrapper in `session.py` if CLI/TUI commands need it

### New model field

The Qobuz API returns many fields not currently parsed. To add one:

1. Add the field to the relevant dataclass in `models/`
2. Add `.get("field_name")` in the `from_dict()` method
3. Add a test in `tests/test_models.py`

### New config option

1. Add the field to the relevant config dataclass in `config.py`
2. Add it to `load_config()` and `save_config()`
3. Add validation in `validate_config()` if it has constraints
4. Document it in [docs/configuration.md](configuration.md)

---

## Code style

- 4-space indentation throughout
- Every source file starts with a `# kabooz/filename.py` header comment
- Public methods have docstrings
- Internal helpers are prefixed with `_`
- No business logic in `cli.py` or `tui.py`
- All features must be callable from library code, not just from the CLI

---

## Tests

Tests live in `tests/`. The test suite uses `pytest` and `respx` for HTTP
mocking. No real network calls are made in tests.

```bash
pytest                    # all tests
pytest tests/test_models.py  # specific file
pytest -k "test_search"   # specific tests by name
pytest -v                 # verbose output
```

When adding a feature, add tests for:

- The happy path
- Empty/missing fields in API responses (partial objects are common)
- Error conditions (404, 401, rate limit)

---

## Things that would make Kabooz better

If you are looking for a place to start, these are known gaps:

- **Async support** — `QobuzClient` uses synchronous `httpx`. An async
  variant would make parallel downloads significantly faster without the
  thread overhead of the current `max_workers` approach
- **Better TUI** — `tui.py` (Textual-based) is incomplete; `tui_curses.py`
  works but is limited
- **More search result pages** — search currently returns one page;
  pagination helpers would make `kabooz search` more useful
- **Stream report accuracy** — `_StreamReporter` sends start/end events but
  does not track actual playback duration accurately
- **More MusicBrainz data** — currently only applies recording and release
  group IDs; there is much more available per-ISRC
- **Windows path handling** — filename sanitization uses Unicode lookalikes
  which work on Linux/macOS/Android; Windows has additional constraints

---

## Reporting issues

Include:

- The exact command you ran
- The full traceback if there was an error
- Your Python version (`python --version`)
- Your Kabooz version (`kabooz --version`)

Do not include your app ID, app secret, or auth token in bug reports.

