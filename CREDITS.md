# Credits

Kabooz exists because of the people and services below. This file is an attempt
to give proper credit rather than just a bullet list.

---

## Qobuz

*https://www.qobuz.com*

Kabooz would be a meaningless piece of software without Qobuz. It is worth saying
that plainly, even in a tool that talks to Qobuz's API without official sanction.

Qobuz is one of the few streaming services that has genuinely cared about music as
an art form rather than as content. In an industry that spent years training people
to think music was worth nothing — a monthly fee for infinite disposable background
noise — Qobuz held a different position. It insisted that the recording quality
mattered, that the people who made the music deserved to be paid properly, and that
the listeners who wanted more than a compressed MP3 deserved to get it.

The catalog is serious. Not just the obvious canonical recordings, but deep cuts,
obscure chamber music, early music on period instruments, jazz from labels most
people have never heard of, folk traditions from places most streaming algorithms
would never surface. The kind of catalog that a person who actually listens to music
built, not one assembled by a recommendation engine optimizing for passive listening
time.

The hi-res audio is real. Not upsampled. The bit depths and sampling rates on offer
correspond to actual source material, which matters in ways that are hard to explain
to someone who hasn't heard the difference but immediately obvious to someone who has.
A 24-bit 96 kHz transfer of an analogue master sounds different from a 16-bit 44.1 kHz
version of the same recording. Sometimes profoundly so. Qobuz provides that access.

The business model is honest. You pay a subscription. Qobuz pays the labels and
through them, artists. There are no advertisements. The service does not harvest and
sell your listening data as its primary product. The money flows from the person who
wants to listen to music toward the people who made it, which is how it should work.

The Qobuz editorial team is worth mentioning separately. The reviews and listening
guides on the platform are written by people who know music — not auto-generated
summaries, not marketing copy, but actual critical writing about recordings and
composers and performers and why a particular release is worth your time. That is
increasingly rare and it deserves to be noticed.

None of this means Kabooz is endorsed by or affiliated with Qobuz. It is not. Kabooz
is an unofficial tool built by someone who uses Qobuz and wanted a way to work with
it from the command line. In the meantime, if you use Kabooz and you do not already
have a Qobuz subscription, please consider getting one. The service deserves the revenue.

---

## MusicBrainz

*https://musicbrainz.org*

MusicBrainz is the open music encyclopedia. It is a community-maintained database of
recordings, releases, artists, labels, works, and the relationships between all of them
— maintained not by a corporation but by volunteers who care about music metadata being
correct and complete.

Kabooz uses MusicBrainz to enrich audio tags beyond what the Qobuz API provides. When
you have an ISRC code for a track, MusicBrainz can tell you the official MusicBrainz
recording ID, the release group it belongs to, whether it is a live recording or a
studio take, which label released it in which country, and a great deal more. That
information ends up embedded in your audio files, where it can be used by any player
or library manager that knows how to read it.

The MusicBrainz project is not glamorous work. Correcting metadata, merging duplicate
entries, adding missing releases, standardizing artist names across scripts and
transliterations — it is painstaking and unglamorous and the people who do it are
rarely thanked. They should be.

---

## LRCLIB

*https://lrclib.net*

LRCLIB is a free, open, no-authentication-required database of synced lyrics in LRC
format. LRC is the format where each line of lyrics is tagged with a timestamp — the
format that makes lyrics scroll in sync with the music in players that support it.

Kabooz uses LRCLIB to fetch synced lyrics and embed them in downloaded audio files,
both as synced LRC data for players that can display it in time and as plain text for
players that cannot. The result is that your local files have lyrics without requiring
any additional software or service.

LRCLIB asks nothing in return. No account, no API key, no rate limit that requires
you to prove you are a legitimate user. It simply has a database of lyrics and an
HTTP endpoint that returns them. That straightforward, non-extractive approach to
providing a useful service is worth appreciating explicitly.

---

## tmxkwpn

*@tmxkwpn on Telegram*

Technical help and guidance during development. Kabooz would have taken longer and
worked worse without it.

---

## Python dependencies

These libraries do the actual work underneath Kabooz's surface.

**httpx** *(github.com/encode/httpx)* — the HTTP client that handles all communication
with the Qobuz API and the CDN. Reliable, well-documented, and a genuine improvement
over the alternatives.

**mutagen** *(github.com/quodlibet/mutagen)* — reads and writes audio metadata for
FLAC and MP3 files. The Quod Libet project has maintained this library for a long time
and it handles edge cases that simpler libraries miss.

**Typer** *(github.com/tiangolo/typer)* — the CLI framework that turns Python function
signatures into command-line interfaces. It makes the CLI code read like what it does
rather than like argument parsing boilerplate.

**Rich** *(github.com/Textualize/rich)* — terminal formatting. The progress bars,
colored output, tables, and panels that make Kabooz's output readable rather than a
wall of plain text.

**tomli-w** *(github.com/hukkin/tomli-w)* — writes TOML configuration files. Small,
does one thing, does it correctly.

---

## A note on the name

Kabooz is phonetically close to Qobuz. That is intentional. The connection should be
legible to anyone who sees it. The distance from the real name is also intentional —
it is not Qobuz, it is not affiliated with Qobuz, and the name reflects both of those
things at once.

---

## License

Kabooz is released under the AGPL-3.0-or-later license. The full text is in the
LICENSE file. The short version: you can use it, study it, modify it, and distribute
it, but if you distribute a modified version you must make the source available under
the same terms.

This is not a legal barrier to tinkering. Modify the code. Add features. Fix bugs.
Make it work better for your use case. The AGPL is designed to keep improvements in
the commons rather than allowing them to disappear into proprietary forks, which
seems like the right disposition for a tool like this.

