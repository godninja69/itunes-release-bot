# Telegram Release Radar — iTunes

A standalone Telegram bot that watches tracked artists on **iTunes only** and
alerts on new albums, singles and guest appearances released in the last five
days. It shares no code and no database with the Spotify bot in `../Spotify/`, so
the two can be deployed, restarted or deleted independently.

Only the public iTunes Search and Lookup APIs are used, so there are no API keys,
no OAuth and nothing to rotate.

## Files

| File | Purpose |
| --- | --- |
| `itunes_bot.py` | Telegram command handlers and the continuous scan |
| `itunes_database.py` | SQLite storage for subscriptions, seen releases and settings |
| `itunes_music_client.py` | Public iTunes API client, rate limiting, filtering, dedup |
| `itunes_radar.db` | Local database (the server keeps its own copy) |

There is no backfill tool and no import path: the artist list is entered by hand
through the bot.

## Configuration

`.env` is the only file you need, and it holds only the bot token:

```env
BOT_TOKEN=...
```

`BOT_TOKEN` must be a token created for **this** bot with @BotFather. Two bots
polling with the same token both receive `409 Conflict` and every command
silently fails, so do not reuse the Spotify bot's token.

Neither the chat ID nor the scan schedule is configured. Alerts go to whichever
account sends the bot a private `/start` first, and the scan runs continuously.

## Run

```bash
python3 -m venv .venv
.venv/bin/activate
pip install -r requirements.txt
python itunes_bot.py
```

## Scheduling

The scan is registered with `application.job_queue.run_repeating` and runs
continuously, re-walking the whole artist list every `ITUNES_SCAN_INTERVAL_SECONDS`
(default 60). The iTunes API has no daily quota, so continuous coverage costs
nothing and catches a drop within minutes instead of the next morning.

The batch size is irrelevant here — every artist is checked each pass. What keeps
the request rate reasonable is `ITUNES_CHECK_DELAY_SECONDS` (default 3.0) between
artists, because Apple rate-limits by IP.

## Commands

- `/start` — help; in a private chat it also claims the alert destination
- `/add <artist name or link>` — track an artist on iTunes
- `/bulkadd <names or links>` — track several artists at once
- `/list` — list tracked artists
- `/remove <artist name or link>` — stop tracking
- `/bulkremove <names>` — stop tracking several artists
- `/status` — API health, tracked count, pass timing, next scan
- `/id` — show where alerts are sent and your own user/chat IDs
- `/pause` / `/resume` — pause or resume the scan

A `.txt`, `.csv` or `.json` file can be uploaded to import a list of artists.

## Alert destination

Telegram only lets a bot message someone who has started it, so the destination
cannot be hardcoded from a token alone. The **first account to send a private
`/start`** is recorded in the `settings` table of `itunes_radar.db` and becomes
the alert target; later users are told where alerts already go. Run `/id` to see
the current destination, or delete the `admin_chat_id` row to reassign it.

Because this is claimed rather than configured, anyone with the bot token can
take the destination. Keep the token private.

## What gets alerted

Each pass makes two lookups per tracked artist:

- `entity=album` — the artist's **own** albums, EPs and singles. Only collections
  whose `artistId` is the tracked artist are taken.
- `entity=song` — **features**. Apple reports the lead act of a track as its
  `artistName`, so a track whose lead act is not the tracked artist is a guest
  appearance and is reported as one. Any guests named in a `(feat. …)` suffix are
  added to the credit list, because Apple exposes only the lead act.

A release is only reported when all of the following hold:

- its day-precision release date falls within `RELEASE_LOOKBACK_DAYS` (default 5)
  and is not in the future;
- the tracked artist is credited exactly, compared on normalised names only —
  never a substring match;
- the credit is a real credit, so nothing credited to `Various Artists` passes;
- the title is not a third-party version or a set: remix, mashup, bootleg,
  rework, edit, dub, VIP, flip, slowed/sped-up/reverb copies, type beat, cover,
  remaster, compilation, DJ mix, continuous mix, live set, karaoke, tribute or
  instrumental.

When the tracked artist is not the first credit, the alert is a feature and
reads `Artist: <lead act>` / `Featuring: <tracked artist>`, so the notification
names who is actually featured rather than reporting a remix of their track.

## Filtering and reliability

- Apple rate-limits by IP, so requests are paced with `ITUNES_CHECK_DELAY_SECONDS`
  and a `429` is retried after a bounded pause rather than aborting the scan.
- Seen releases are keyed per destination chat, so a rescan never re-sends an
  alert, and a fresh database means the first pass reports everything still inside
  the five-day window.
- Deduplication is by lead act + normalised title + release date, so the same
  single arriving through both the album and the song lookup produces one alert.

## Optional tuning

All have working defaults and none need to be set: `DATABASE_PATH`,
`RELEASE_LOOKBACK_DAYS`, `MUSIC_MAX_RETRIES`, `ITUNES_CHECK_DELAY_SECONDS`,
`ITUNES_SCAN_INTERVAL_SECONDS`, `STARTUP_SCAN_DELAY_SECONDS`,
`IMPORT_DELAY_SECONDS`, `MAX_IMPORT_ARTISTS`, `MAX_IMPORT_BYTES`,
`ITUNES_COUNTRY`.

## Database

`itunes_radar.db` holds tracked artists, already-announced releases and the
claimed alert destination. It is created empty and rebuilt empty on every start:
there is no migration from an older database and no import path, because the
artist list is entered by hand through the bot. Delete the file to start over.