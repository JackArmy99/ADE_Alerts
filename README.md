# ade-radar

Personal alert agent for ADE 2026 (21 to 25 Oct). It watches Instagram, Gmail and the official ADE
programme, decides which events matter to each of the 5 friends, and sends WhatsApp alerts.
`CLAUDE.md` is the spec and `START_HERE.md` is the checklist. This file covers what is built and how to run it.

## What is built

| Step (START_HERE) | Status | Code |
|---|---|---|
| 1. Setup | done | `config.py`, `db.py` (SQLite at `data/radar.sqlite`), `cli.py`, `.env.example`, `scripts/setup.ps1` |
| 2. WhatsApp + ntfy | done, needs a live test | `notify/whatsapp.py`, `notify/ntfy.py`, `tools/check_template.py` |
| 3. Instagram | built against a synthetic fixture, needs a live check | `sources/instagram.py`, `sources/browser.py` |
| 4. Classification + alerts | done | `classify.py` (Claude Haiku with vision, cached taste prompt), `match.py`, `alerts.py`, `pipeline.py` |
| 5. Gmail | done, needs a live test | `sources/gmail.py` |
| 6. Scheduling + heartbeat | done | `scripts/*.ps1`, `reports.py` |
| 7. ADE programme | built against a synthetic fixture, needs `ade-discover` on your laptop | `sources/ade.py`, `dedupe.py` |
| 8. Slow follow | done, needs a live check | `tools/follow_slowly.py` |

48 tests run offline against fixtures (`.venv\Scripts\pytest`). This code was written in a cloud container
that **cannot reach Instagram or amsterdam-dance-event.nl**. The Instagram and ADE parsers are therefore built
against synthetic fixtures and have to be checked once against the real sites (steps 3 and 7 below).

## First run on the laptop (in order)

```powershell
git clone <this repo> C:\ade-radar ; cd C:\ade-radar
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1     # venv, deps, .env, database
notepad .env                                                   # paste the secrets
$r = ".venv\Scripts\ade-radar.exe"

# 2. WhatsApp
& $r check-template              # status + language of ade_alert; fixes WHATSAPP_TEMPLATE_LANG if needed
& $r test-whatsapp --to jack     # hello_world (+ ade_alert sample if approved) to Jack only
& $r test-whatsapp               # then everyone

# 3. Instagram (separate account only)
& $r ig-login                    # log in by hand in the Chrome window, then press Enter
& $r ig-inspect                  # saves the real feed to tests\fixtures\instagram\live_*; check parsed.json
& $r run --source instagram --dry-run --force

# 4. Fake-flyer test: post "ADE pop-up, presale sign-up, link in bio" from your own account, then
& $r run --source instagram --force   # must arrive as an URGENT WhatsApp

# 5. Gmail: forward an old event mail to the +ade address, then
& $r run --source gmail --dry-run

# 7. ADE programme
& $r ade-discover                # finds the JSON endpoint, saves fixtures, writes data\ade_endpoint.json
& $r run --source ade --dry-run

# 6. Schedule everything
powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
powershell -ExecutionPolicy Bypass -File scripts\status.ps1   # are the tasks running? last log lines
```

If `ig-inspect` shows `parsed.json` with empty permalinks or authors, Instagram's markup differs from the
synthetic fixture. Open Claude Code in the folder and ask it to fix `parse_post` against the new
`tests\fixtures\instagram\live_*` files.

## How it decides

1. Each source returns `Item`s. Sources never send anything.
2. Deterministic match first (`match.py`): artists (accents and country codes stripped; names of 4 characters
   or fewer must match exactly), brands, venues, urgent keywords, ADE keywords, and the author's
   `priority: urgent` account.
3. Claude Haiku (`claude-haiku-5-5`) reads the screenshot or mail and returns strict JSON. `taste.yaml` sits
   in a cached system prompt. There is a hard cap of 60 calls per run (`CLAUDE_MAX_CALLS_PER_RUN`), and the
   rules alone are used once the cap is hit or if the API fails.
4. Routing: an urgent post from a priority account goes to everyone. Other alerts go only to the people
   whose taste matches. Normal alerts are capped at 15 per person per day; urgent alerts are not capped.
5. Idempotency: the `alerts` table is keyed by (item, reason hash, recipient). The reason hash covers the
   canonical event plus the kind of change, so the same event posted by the DJ and by the brand only alerts
   once. Failed sends are retried on the next run.

Template mapping: {{1}} event and artists, {{2}} day and time (after midnight counts as the previous night),
{{3}} venue, {{4}} `URGENT: presale sign-up open: <best link>`. "Link in bio" points to the author's profile.

## Things to know

- **Until `ade_alert` is approved**, set `WHATSAPP_TEMPLATE=hello_world`. Alerts still go out as
  hello_world, and the full alert text is mirrored to ntfy so nothing is lost.
- **Heartbeat and digest** use the `ade_alert` template too, because no other template exists: the Event
  field reads "Radar heartbeat (not an event)". Both are also sent to ntfy.
- **Instagram** runs at most once per 50 minutes (`--force` overrides this for manual tests). A run stops at
  the third already-seen post or after 40 new posts (20 on the very first run). A login wall, checkpoint
  or "try again later" stops the run and sends an ntfy error.
- **ADE first run** is a baseline. It alerts only for `must_see` matches; everything else is stored, and
  later runs alert on new events, lineup additions, sold out and set times. Event pages are fetched slowly,
  at most 150 per run.
- **Gmail** opens the `ADE` label read-only. It never marks mail as read and never deletes anything. The
  first run looks back 7 days.
- Logs go to `data\logs\radar.log` (rotated daily) and to `data\logs\task_<date>.log` for the scheduled
  runs. Secrets are redacted from the logs.
