# TopJobs.lk Telegram Job Search Bot

A Telegram bot that searches [TopJobs.lk](https://topjobs.lk) through the Apify Actor
[`fanndev/topjobs-lk-scraper`](https://apify.com/fanndev/topjobs-lk-scraper). It sends 5 jobs
per search, has a **Thawa Jobs Pennanna (Show More)** button for the next 5, and is built to
keep Apify spending low.

## How it works

1. `/start`: the bot welcomes the user and asks for a keyword.
2. The user sends a keyword. The bot normalizes it: trim, lowercase, collapse spaces.
3. The bot gets up to 10 jobs this user hasn't seen. It uses the shared cache when it can and runs the Actor only when it has to.
4. It sends the first 5 jobs. The other 5 are saved in SQLite behind a short session ID that goes in the button.
5. **Show More** sends those 5 from SQLite, never from the Actor, and removes the button.

## Project structure

```
jobbot/
  __main__.py        entry point (python -m jobbot)
  config.py          .env loading and validation
  actor_mapping.py   Actor input + output -> Job mapping (Actor-specific code lives here)
  apify_service.py   async Actor runs: memory, fallback, timeout, item limits
  job_service.py     cache, shared in-flight runs, duplicate filtering, paging
  rate_limit.py      per-user and global abuse limits
  storage.py         SQLite (cache, sent history, Show More sessions, rate events)
  formatting.py      HTML-escaped messages under 4096 chars
  bot.py             Telegram handlers
  logging_setup.py   log formatter that redacts tokens
tests/               pytest suite (no network)
```

## Actor field mapping

| Bot field   | Actor output field(s)                                                    |
|-------------|--------------------------------------------------------------------------|
| Title       | `position`                                                               |
| Company     | `employer`                                                               |
| Location    | `location` (full detail) or `town` (compact)                             |
| Posted date | `openingDate` (compact) or `jsonLd.datePosted`                           |
| Description | `jsonLd.description`; if missing, `jobType` badges + `closingDate`       |
| Link        | `detailUrl`                                                              |

Items with `_error`, without a title, or without an http(s) link are skipped.

The input sent is `{"mode": "search", "keyword": ..., "maxResults": 10, "fetchFullDetails": ...}`.

**Limits of this Actor:**
- TopJobs.lk publishes job descriptions as images, so a real text description exists only
  when `jsonLd` has one. Otherwise the bot shows the job type and closing date instead.
- The Actor has no "sort by newest" option. Early stopping at the last-sent URL is therefore
  **disabled** (`ACTOR_SORTS_NEWEST = False` in `actor_mapping.py`), and the bot logs a warning at
  startup. Duplicates are still filtered by each user's sent-URL history. If you switch to an Actor
  that can sort by date, set the flag to `True` and add the sort option to `build_run_input()`.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

```bash
pip install -r requirements.txt
```

```bash
cp .env.example .env
```

Fill in `.env`:

- `TELEGRAM_BOT_TOKEN` from [@BotFather](https://t.me/BotFather)
- `APIFY_API_TOKEN` from Apify Console → Settings → API & Integrations
- `APIFY_ACTOR_ID` (already set to `fanndev/topjobs-lk-scraper`)

`.env` is in `.gitignore`. Never commit it.

## Run

```bash
python -m jobbot
```

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

The tests cover keyword normalization, duplicate filtering, stopping at the saved URL, pagination
(including double clicks and expired buttons), cache expiry and shared runs, rate limits, memory
fallback, HTML escaping, the message length limit and token redaction.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | Bot token |
| `APIFY_API_TOKEN` | required | Apify token |
| `APIFY_ACTOR_ID` | required | `fanndev/topjobs-lk-scraper` |
| `APIFY_MEMORY_MB` | 256 | Memory for every run (raised to the Actor's minimum if it declares a higher one) |
| `APIFY_FALLBACK_MEMORY_MB` | 1024 | One retry at this memory when a run fails for lack of memory |
| `APIFY_TIMEOUT_SECS` | 120 | Run timeout on Apify; runs still going are aborted |
| `CACHE_TTL_HOURS` | 6 | How long a keyword's results are reused across all users |
| `RATE_LIMIT_COOLDOWN_SECS` | 10 | Minimum gap between one user's searches |
| `RATE_LIMIT_SEARCHES_PER_HOUR` | 20 | Searches per user per rolling hour |
| `RATE_LIMIT_USER_RUNS_PER_DAY` | 5 | **New Actor runs** a user can trigger per rolling 24h |
| `RATE_LIMIT_GLOBAL_RUNS_PER_DAY` | 200 | Hard cap on Actor runs for the whole bot per rolling 24h |
| `ADMIN_USER_IDS` | empty | Comma-separated Telegram IDs exempt from the per-user limits |
| `APIFY_FETCH_FULL_DETAILS` | true | `true`: location, job type and description data. `false`: faster compact rows |
| `DATABASE_PATH` | `jobbot.sqlite3` | SQLite file |
| `LOG_LEVEL` | INFO | Logging level |

Setting any `RATE_LIMIT_*` value to `0` disables that limit.

## Cost controls

- **Memory:** runs start at 256 MB, or at the Actor's declared minimum if that is higher. The bot
  reads the minimum from the Actor's latest build. If a run fails because it ran out of memory, the
  bot retries once with the fallback memory. Memory and run status are logged for every run.
- **Per-run limits:** `maxResults` (in the Actor input) and `max_items` (on the run) are both set
  to 10. The dataset read is capped at 10, and `APIFY_TIMEOUT_SECS` applies to every run.
- **Caching:** results are cached per keyword in SQLite for `CACHE_TTL_HOURS` and shared by all
  users. Each user still has their own sent history.
- **Shared runs:** if a keyword is already being fetched, later searches wait for that run instead
  of starting another.
- **Show More:** reads from SQLite only and never starts an Actor run.
- **Rate limits:** only searches that would start a new Actor run count toward the run budgets.
  Cache hits are free. The global daily cap limits the worst-case daily cost: roughly
  `200 × (start fee + 10 results)`.

This Actor is billed per event: a start fee plus a fee per result. Apify platform compute isn't
billed separately, so the result limits and the run caps save more money than low memory does.

## Abuse protection

- One active search per user. Extra messages get "please wait".
- A cooldown, an hourly search cap and a daily cap on new Actor runs, all per user and stored in
  SQLite so they survive restarts.
- A global daily cap on Actor runs that applies to admins too.
- Limit notices go out at most once every 5 seconds per user, so a flooding user can't make the
  bot flood Telegram back.
- Show More buttons are one-time, tied to their user, and expire after 24 hours. A new search
  retires older buttons.

## Deployment

The bot uses long polling, so it doesn't need a public URL. Run **exactly one instance per bot
token**. Two instances polling the same token conflict, and each would keep its own in-flight run
tracking.

### Render (free) + UptimeRobot

Render's free plan doesn't offer background workers, so the bot runs as a free **web service**.
When the `PORT` environment variable is set (Render sets it), the bot also serves `GET`/`HEAD`
requests on `/health`. UptimeRobot pings that URL every 5 minutes, which keeps the instance from
sleeping after 15 idle minutes.

1. Push this project to a GitHub repo. `.env` is git-ignored and must stay out of the repo.
2. In Render, go to **New → Blueprint**, pick the repo, and Render reads [`render.yaml`](render.yaml).
   Enter `TELEGRAM_BOT_TOKEN`, `APIFY_API_TOKEN` and, if you want, `ADMIN_USER_IDS` when Render asks
   for them.
3. Once it's deployed, open `https://<your-service>.onrender.com/health`. It should say `ok`.
4. In UptimeRobot, add an **HTTP(s)** monitor for that `/health` URL with a 5-minute interval.

Free-plan limits:
- **No persistent disk.** The SQLite file (cache, sent history, rate-limit counters) is wiped on
  every redeploy or restart. The bot keeps working, but users may see jobs again and the counters
  reset. For lasting data, use a paid instance with a disk mounted at `DATABASE_PATH`.
- **750 free instance hours a month** per workspace. One always-on service uses about 744, so
  don't run a second free service in the same workspace.
- During a deploy, the old and new instances overlap for a moment. Telegram may then log a short
  `Conflict` error, which clears on its own.

### Linux VPS with systemd

```bash
sudo useradd --system --create-home --home-dir /opt/jobbot jobbot
```

Copy the project to `/opt/jobbot`, create the venv and `.env` as described above, then create
`/etc/systemd/system/jobbot.service`:

```ini
[Unit]
Description=TopJobs Telegram bot
After=network-online.target

[Service]
User=jobbot
WorkingDirectory=/opt/jobbot
ExecStart=/opt/jobbot/.venv/bin/python -m jobbot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now jobbot
```

```bash
journalctl -u jobbot -f
```

### Docker

```bash
docker build -t jobbot .
```

```bash
docker run -d --name jobbot --restart unless-stopped --env-file .env -v jobbot-data:/data jobbot
```

The SQLite database lives in the `jobbot-data` volume, so the cache, sent history and rate limits
survive restarts and redeploys.

## Security

- Tokens are read from `.env` and never hardcoded.
- The log formatter redacts both tokens and anything shaped like a bot or Apify token. The `httpx`
  request logs, which include the bot token in the URL, are turned down to WARNING.
- All scraped text is HTML-escaped before it is sent.
- If a token ever leaks (for example, pasted into a chat or committed), revoke it: use `/revoke`
  in @BotFather, or regenerate the Apify token in Apify Console.
