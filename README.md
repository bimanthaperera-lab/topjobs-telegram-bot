# Sri Lanka Job Search Telegram Bot

A Telegram bot that searches **TopJobs.lk** and **XpressJobs** directly. It needs no Apify or any
other paid scraping service. Each search sends 5 jobs, and a **Thawa Jobs Pennanna (Show More)**
button sends the next 5.

> **XpressJobs is switched off by default.** Its terms of use forbid scraping
> ("spider, crawl, or scrape") without permission. Set `XPRESSJOBS_ENABLED=true` only after
> XpressJobs has given you written permission.

## How it works

1. `/start`: the bot welcomes the user and asks for a keyword.
2. The user sends a keyword. The bot normalizes it: trim, lowercase, collapse spaces.
3. The bot searches every enabled site in parallel. It reuses each site's cached results when
   they're fresh, and only contacts a site when it has to.
4. It ranks each site's results by relevance to the keyword (see `jobbot/relevance.py`). Then it
   picks up to 10 jobs this user hasn't seen, alternating between the sites.
5. The "Searching…" message turns into a header ("🔎 Found 8 new jobs for driver"). Then
   **each job goes in its own message** with an inline **🔗 View & Apply** button that opens
   the job page. If Telegram rejects a URL as a button, the bot sends a text link instead.
6. If more jobs are waiting, a last message says "Showing 5 of 8" and has the **Show More**
   button. The remaining jobs are saved in SQLite behind a short session ID.
7. **Show More** sends them from SQLite without contacting the sites, then removes the button.
   Messages are queued by PTB's rate limiter, so sending 5–7 messages in a row stays within
   Telegram's flood limits.

If a site can't be reached, the bot shows that site's earlier cached results, marked "may be
outdated". If it has none, the bot shows the other site's jobs with a note saying so.

## Sites

| | TopJobs.lk | XpressJobs |
|---|---|---|
| How | HTML search page, `GET /applicant/vacancybyfunctionalarea.jsp?txtKeyWord=` | JSON API, `GET /api/jobs/searchJobs?keyword=` |
| Order | Newest first, by opening date | Re-sorted newest first by `jobId` |
| Title / company / location | yes | yes |
| Posted date | yes | when the API provides it |
| Closing date | yes | yes |
| Description | Short note only; full ads are images | Job type + `overview` text |
| robots.txt / terms | no robots.txt, no scraping clause found | robots.txt allows all; **terms forbid scraping** |

Code lives in [`jobbot/sources/`](jobbot/sources/). Adding another site means writing one class
with `name`, `sorted_newest_first` and `async fetch(keyword, limit)`.

## Project structure

```
jobbot/
  __main__.py          entry point (python -m jobbot)
  config.py            .env loading and validation
  site_health.py       tracks failing sites and alerts the admins
  sources/
    http.py            shared httpx client: per-site request gap, retries, pause on 403/429
    topjobs.py         TopJobs.lk HTML parser
    xpressjobs.py      XpressJobs JSON API client
  job_service.py       per-site cache, shared in-flight requests, duplicate filtering, paging
  rate_limit.py        per-user and global abuse limits
  storage.py           SQLite (cache, sent history, Show More sessions, rate events)
  formatting.py        HTML-escaped messages under 4096 chars
  health.py            /health endpoint for Render + UptimeRobot
  bot.py               Telegram handlers
  logging_setup.py     log formatter that redacts the bot token
tests/                 pytest suite (no network)
```

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

Put your bot token from [@BotFather](https://t.me/BotFather) into `TELEGRAM_BOT_TOKEN` in `.env`.
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

The tests cover keyword normalization, duplicate filtering, stopping at the saved URL for each
site, mixing two sites, pagination (including double clicks and expired buttons), cache expiry,
shared requests, one site failing, retries and the pause after a block, stale-cache fallback,
admin alerts, rate limits, both parsers, request spacing, HTML escaping, message length and
token redaction.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | Bot token |
| `TOPJOBS_ENABLED` | true | Search TopJobs.lk |
| `XPRESSJOBS_ENABLED` | **false** | Search XpressJobs. Turn on only with their permission |
| `SCRAPE_MIN_INTERVAL_SECS` | 2 | Minimum gap between requests to the same site, across all users |
| `HTTP_TIMEOUT_SECS` | 20 | Request timeout |
| `HTTP_RETRIES` | 2 | Retries for timeouts and 5xx errors, waiting 2s, then 4s |
| `SITE_BACKOFF_MINUTES` | 30 | After a 403/429, how long to stop contacting that site (longer if the site's `Retry-After` asks for it) |
| `STALE_CACHE_MAX_HOURS` | 48 | When a site fails, how old its cached results can be and still be shown |
| `ALERT_AFTER_FAILURES` | 3 | Alert the admins after this many failures in a row (403/429 alert immediately) |
| `CACHE_TTL_HOURS` | 6 | How long a site's results for a keyword are reused for everyone |
| `SCRAPER_USER_AGENT` | `TopJobsTelegramBot/1.0 (+https://t.me/TopJobsSearch_Bot)` | How the bot identifies itself to the sites |
| `RATE_LIMIT_COOLDOWN_SECS` | 10 | Minimum gap between one user's searches |
| `RATE_LIMIT_SEARCHES_PER_HOUR` | 20 | Searches per user per rolling hour |
| `RATE_LIMIT_USER_FETCHES_PER_DAY` | 30 | Searches per user per 24h that have to contact the sites |
| `RATE_LIMIT_GLOBAL_FETCHES_PER_DAY` | 500 | Hard cap on site requests per 24h, across all users |
| `ADMIN_USER_IDS` | empty | Telegram IDs that skip the per-user limits **and get site alerts**. Each admin must press `/start` in the bot once |
| `DATABASE_PATH` | `jobbot.sqlite3` | SQLite file |
| `LOG_LEVEL` | INFO | Logging level |

Setting any `RATE_LIMIT_*` value to `0` disables that limit.

## Being a good guest on the job sites

- **Cache:** results are cached per site and keyword for 6 hours and shared by all users.
- **Shared requests:** if a keyword is already being fetched, later searches wait for that request
  instead of sending another.
- **Request gap:** at most one request every 2 seconds per site, however many users search at once.
- **Honest User-Agent:** the bot identifies itself and doesn't pretend to be a browser.
- **Fetch budgets:** per-user and global limits cap how often the sites get contacted. Cache hits
  and Show More are free.
- **No detail pages:** one search request per site, and nothing more.

## When a site has problems

| What happens | What the bot does |
|---|---|
| Timeout, connection error, 5xx | Retries twice, waiting 2s and then 4s |
| 4xx other than 403/429 | Gives up right away; retrying won't help |
| **403 or 429** (blocked or rate-limited) | Never retries. Pauses all requests to that site for 30 min, or longer if the site's `Retry-After` asks |
| Any failure | Shows that site's cached results up to 48h old, marked "may be outdated" |
| 3 failures in a row, or any 403/429 | Sends **one** alert to `ADMIN_USER_IDS`, and one more when the site works again |

The bot doesn't rotate IP addresses or proxies to get around a block. A block means the site is
refusing the bot, so the bot backs off and tells you.

## Abuse protection

- One active search per user.
- A cooldown, an hourly search cap and a daily fetch cap, all per user and stored in SQLite.
- A global daily fetch cap.
- Limit notices go out at most once every 5 seconds per user.
- Show More buttons are one-time, tied to their user, and expire after 24 hours. A new search
  retires older buttons.

## Deployment

The bot uses long polling and needs no public URL. Run **exactly one instance per bot token**.

### Render (free) + UptimeRobot

The bot runs as a free Render **web service** using the [`Dockerfile`](Dockerfile). When `PORT`
is set, which Render does, the bot also answers `GET`/`HEAD` on `/health`. UptimeRobot pings that
URL every 5 minutes so the free instance never goes to sleep.

1. Push to GitHub. `.env` is git-ignored.
2. In Render: **New → Web Service** → pick the repo → Language **Docker** → Instance **Free** →
   Health Check Path `/health`.
3. Add the environment variables: `TELEGRAM_BOT_TOKEN`, plus any from the table above that you
   want to change.
4. Once it's deployed, `https://<service>.onrender.com/health` should return `ok`.
5. In UptimeRobot, add an **HTTP(s)** monitor for that URL with a 5-minute interval.

**Free-plan limits:**
- **No persistent disk.** The SQLite file is wiped on every redeploy or restart, so the cache, sent
  history and rate-limit counters reset.
- **750 free instance hours a month.** One always-on service uses about 744.
- **Short `Conflict` errors during deploys** are normal while the old and new instances overlap.

### CI/CD pipeline (push → tests → Render deploy)

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on every push and pull request:

| Job | What it checks |
|---|---|
| Tests (Python 3.11, 3.12) | Installs dependencies, byte-compiles every module, runs `pytest` |
| Docker image | Builds the same image Render builds, imports the app inside it, and checks that a missing token gives a clean exit code 2 instead of a crash |

Render deploys **only `main`, and only when every check passes**:

```
push to main ──► GitHub Actions ──► all green? ──► Render deploys
                                 └─► any red?  ──► nothing deploys; the live bot keeps running
push to a branch / open a PR ──► same checks, never deploys
```

One-time setup in the Render dashboard (needed because the service wasn't created from the Blueprint):
**your service → Settings → Build & Deploy → Auto-Deploy → "After CI Checks Pass"**, with the
branch set to `main`. Render skips a commit that has no checks, so leave the workflow enabled.

Day-to-day:

```bash
git switch -c feat/my-change
```

```bash
git push -u origin feat/my-change
```

Open a pull request into `main`, wait for green checks, then merge. Merging is what deploys.

### Docker

```bash
docker build -t jobbot .
```

```bash
docker run -d --name jobbot --restart unless-stopped --env-file .env -v jobbot-data:/data jobbot
```

## Security

- The bot token is read from `.env` or the host's environment and never hardcoded.
- The log formatter redacts the token. `httpx` request logs, which include the bot token in the
  URL, are turned down to WARNING.
- All scraped text is HTML-escaped before it is sent.
- If the token ever leaks, revoke it with `/revoke` in @BotFather.
