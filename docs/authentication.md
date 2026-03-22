# Authentication

Kabooz supports three authentication modes. All of them require a Qobuz `app_id`
and `app_secret`, which identify your application to the Qobuz API.

## Getting credentials

The `app_id` and `app_secret` are not publicly documented by Qobuz. They are
obtained by reverse-engineering the official Qobuz clients. Search online for
current values — the community maintains lists of working credentials.

## Mode 1 — Username and password

The standard mode. Kabooz exchanges your credentials for a session token and
saves it to `~/.config/kabooz/session.json`.

```bash
kabooz login \
  --app-id YOUR_APP_ID \
  --app-secret YOUR_APP_SECRET \
  -u your@email.com \
  -p yourpassword
```

Omit any flag and Kabooz will prompt for it interactively:

```bash
kabooz login
# App ID:     (prompted)
# App Secret: (prompted, hidden)
# Username:   (prompted)
# Password:   (prompted, hidden)
```

Credentials and the app ID/secret are saved to `~/.config/kabooz/config.toml`.
The session token is saved separately to `~/.config/kabooz/session.json`.

### Session expiry

Qobuz session tokens expire after a period of inactivity. When a token expires
you will see an `Authentication error` message. Re-run `kabooz login` to get a
fresh token.

## Mode 2 — Pre-existing token

If you already have a valid Qobuz auth token and user ID (from another client,
for example):

```bash
kabooz login --token YOUR_TOKEN --user-id YOUR_USER_ID
```

This skips the credential exchange and saves the token directly.

## Mode 3 — Token pool

Token pool mode allows Kabooz to rotate through a list of pre-authenticated
tokens, falling back to the next one automatically when the current token
expires or returns an empty catalog.

```bash
kabooz login --pool ~/.config/kabooz/pool.txt
```

The pool file is a plain text file with app credentials followed by one token per line:

```
# Lines starting with hash are ignored
# First line is App ID, say 123456
1233456
# Second line is App Secret, say appsecret123
appsecret123
# Third line onwards are token
TOKEN1
TOKEN2
...
```

**Pool mode is read-only.** Write operations — adding or removing favorites,
creating or modifying playlists on your Qobuz account — are disabled in pool
mode. This protects shared accounts from unintended modifications. The local
library (favorites, playlists stored in the SQLite database) still works fully.

### Token pool validation

When loading a pool, Kabooz tests each token with a cheap catalog request to
find the first one that returns real results. Tokens from accounts without an
active subscription pass authentication but return empty catalogs — these are
automatically skipped.

## Environment variables

Credentials can be provided via environment variables, which override the
config file values:

```bash
export QOBUZ_APP_ID=your_app_id
export QOBUZ_APP_SECRET=your_app_secret
```

## Where credentials are stored

| File | Contents |
|------|----------|
| `~/.config/kabooz/config.toml` | App ID, app secret, pool path, all other settings |
| `~/.config/kabooz/session.json` | Session token and user ID |

Both files are created automatically on first login. The session file is safe
to delete — `kabooz login` recreates it. The config file holds your app
credentials, so keep a copy if you want to avoid re-entering them.


