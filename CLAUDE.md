# ade-radar

Personal alert agent for Amsterdam Dance Event (ADE) 2026, 21 to 25 October 2026.
It watches where ADE events get announced (Instagram first, then email and the official
ADE programme), decides which events matter to a group of 5 friends, and sends WhatsApp
alerts. Owner: Jack (Windows laptop, Rotterdam, home internet).

## Why this exists (read this first)

- The events we want most are announced on Instagram BEFORE they appear on the ADE website
  or Resident Advisor: DJ-run pop-ups, secret lineups, presale/pre-registration sign-ups.
  Example: the Social Pleasure (Paige Tomlinson's brand) ADE pop-up is on Instagram only.
- These events sell out in minutes. Several already have (Josh Baker at Gashouder,
  Kyle Starkey Invites, Pegassi All Day Long). So the most valuable alert is often:
  presale / pre-registration / sign-up link, waitlist, second date, extra tickets, resale.
- Deadline: working MVP within 1 to 2 days. Today is 12 Oct 2026. Must run reliably from
  now until 26 Oct. Simplicity and reliability beat features.

## Hard rules

- Windows 11, native (NOT WSL). Python 3.12. PowerShell for scripts. Use `uv` if available,
  otherwise venv + pip.
- No frameworks. One SQLite file at `data/radar.sqlite`. Plain Python modules.
- Secrets only from environment / `.env` (python-dotenv). `.env` and `data/` are gitignored.
  Never print or log tokens. Never commit secrets.
- Every source returns `list[Item]` (see models). Sources NEVER send alerts. Only
  `notify/` sends.
- Alerts are idempotent: `alerts` table keyed by (item_id, reason_hash, recipient).
  Re-running must never send the same alert twice.
- Every feature has a `--dry-run` that prints the WhatsApp messages instead of sending.
- Tests never hit live sites: save real responses/screenshots into `tests/fixtures/`.
- Times are Europe/Amsterdam. Events after midnight belong to the previous "night".
- Instagram: use ONLY the separate Instagram account, never Jack's personal login.
  Be gentle: max one feed check per hour, human-like scrolling, stop at last seen post.
- Resident Advisor scraping is against RA's terms. Not in the MVP. If added later:
  max one sweep per hour, never logged in, personal use only.
- Alert text is English. Keep WhatsApp alerts short.

## Environment variables (.env)

```
ANTHROPIC_API_KEY=
WHATSAPP_TOKEN=                 # permanent System User token (radar-bot), stored in Keeper
WHATSAPP_PHONE_NUMBER_ID=1432080516647223
WHATSAPP_WABA_ID=1377545357487986
WHATSAPP_API_VERSION=v25.0
WHATSAPP_TEMPLATE=ade_alert     # use hello_world until ade_alert is approved
WHATSAPP_TEMPLATE_LANG=en       # VERIFY via API, may be en or en_US (see below)
WHATSAPP_RECIPIENTS=            # comma separated, international format without +, max 5
RECIPIENT_NAMES=                # comma separated, same order, e.g. jack,friend1,...
GMAIL_ADDRESS=                  # Jack's gmail, the agent reads only the ADE label
GMAIL_APP_PASSWORD=
GMAIL_LABEL=ADE
NTFY_TOPIC=                     # long random string, backup channel for heartbeat/errors
IG_PROFILE_DIR=data/ig_profile
```

## Notifications: WhatsApp via Meta Cloud API

- Meta app "ADE radar", business portfolio "Jacks App". Using Meta's FREE TEST NUMBER
  (+1 555 654 2649). It can only message up to 5 recipients that were added and verified in
  the app's API Setup page. Jack's number is verified and a test message worked.
- Token: permanent System User token (radar-bot) with whatsapp_business_messaging and
  whatsapp_business_management, assigned to the app and the WhatsApp account.
- Send: `POST https://graph.facebook.com/{version}/{PHONE_NUMBER_ID}/messages`
  with `Authorization: Bearer {token}`, JSON body type "template".
- No webhooks are configured (deliberately). Therefore we cannot know if a recipient has an
  open 24h window, so ALWAYS send templates, never free-form text or images.
- Template `ade_alert` (category Utility, submitted, pending approval). Structure:
  - Header (text, no variables): "Up the scouse"
  - Body: "New ADE event spotted by the radar. Event: {{1}} When: {{2}} Where: {{3}}
    Details: {{4}} Check the link and grab tickets fast, these sell out."
  - If the wording was changed during submission, fetch the real template with
    `GET /{version}/{WABA_ID}/message_templates?name=ade_alert` and use its exact
    language code and component structure. Do this check in a `check-template` CLI command.
- Template parameter rules: no newlines or tabs, no more than 4 consecutive spaces,
  keep each parameter short (target under 150 chars). Put the best link (ticket, sign-up,
  or Instagram permalink) in {{4}}.
- Until `ade_alert` is Active, test with `hello_world` (no parameters, lang en_US).
- Handle errors: log Meta error code + message, retry with backoff on 5xx/429, alert ntfy
  on repeated failure. Error 131030 means the recipient is not on the test allow list.
- Backup channel: ntfy (`POST https://ntfy.sh/{NTFY_TOPIC}`) for heartbeat and errors.

## Sources (in priority order)

### 1. Instagram feed watcher (most important)
- A separate Instagram account follows only relevant accounts (DJs, their event brands,
  promoters, venues, see `config/accounts.yaml`). Its Following feed = exactly the posts we
  care about, newest first.
- Playwright with `channel="chrome"` (installed Chrome), persistent context at IG_PROFILE_DIR,
  HEADED (not headless). First run: `ade-radar ig-login` opens Chrome so Jack logs in by hand,
  session is saved.
- Each run (hourly): open the Following feed (try `https://www.instagram.com/?variant=following`,
  verify it shows the chronological Following feed; otherwise find the "Following" option),
  also look at the stories tray at the top. Scroll slowly until reaching the last seen post
  (store post permalinks / ids in SQLite). Max ~40 new posts per run.
- For each new post: capture permalink, author handle, caption text, tagged / collab accounts,
  timestamp, and a screenshot of the post including the flyer image (and carousel first
  slides if cheap). For stories: screenshot each new story frame from followed accounts in
  the tray, with the author handle.
- Prefer screenshots + Claude vision over brittle DOM parsing. DOM is only used to find post
  boundaries, permalinks and handles. Expect Instagram markup to change; fail soft.
- If Instagram shows a login wall, checkpoint, or "try again later": stop the run, send an
  ntfy error, do not retry until next scheduled run.
- Discovery: collect every tagged / collab / mentioned account seen in posts. Accounts not in
  accounts.yaml go into a `suggested_follows` table. Send ONE daily WhatsApp/ntfy digest of new
  suggestions (with how often seen and which brand tagged them). This is how we find small
  pop-up brands and a label's roster (Social Pleasure's roster is not public).

### 2. Gmail (ADE label)
- Jack uses `<hisgmail>+ade@gmail.com` for RA, Dice, Shotgun, promoter mailing lists and the
  separate Instagram account. A Gmail filter puts those mails under the label `ADE`
  (skip inbox).
- Read via IMAP (`imap.gmail.com:993`, app password). SELECT only the `ADE` folder. Never
  read other folders. Track processed message UIDs in SQLite. Do not delete mail.
- For each new mail: subject, sender, plain text, links, and up to 3 inline images. Send to
  Claude for classification like Instagram posts. RA "Just Announced", Dice and Shotgun mails
  often contain presale links directly.

### 3. Official ADE programme
- Event pages: `https://www.amsterdam-dance-event.nl/en/program/2026/<slug>/<id>/`
- Festival listing filter:
  `https://www.amsterdam-dance-event.nl/en/program/filter/?section=events&type=8262%2C8263&from=2026-10-21&to=2026-10-25`
- Per event calendar file: `/en/events/calendar/<id>.ics`
- Lineups show country codes: "Amelie Lens (BE)". Strip them.
- There is probably a JSON list endpoint behind the filter page. Discover it with Playwright
  network capture first; fall back to HTML parsing. ~1,300 events. Poll every 30 to 60 min.
- Alert on: new events matching taste, lineup additions to matched events, sold out changes,
  set times published for matched events.

### 4. Optional later: Resident Advisor GraphQL (`POST https://ra.co/graphql`)
- Only if time allows, see Hard rules. Resolve the Amsterdam area id via areas(searchTerm).

## Classification with Claude

- Model: Claude Haiku via the Anthropic API (use `claude-haiku-5-5`; if that id errors,
  check the Anthropic models docs for the current Haiku id). Vision input for screenshots.
- Put `config/taste.yaml` in a cached system prompt. Ask for strict JSON only:

```json
{
  "is_ade_related": true,
  "is_event_announcement": true,
  "event_name": "Social Pleasure ADE pop-up",
  "artists": ["Paige Tomlinson"],
  "date": "2026-10-22 or null",
  "time": "23:00 or null",
  "venue": "A'DAM Toren or null",
  "ticket_status": "presale_signup | on_sale | sold_out | waitlist | second_date | resale | unknown",
  "links": ["..."],
  "urgency": "urgent | normal | ignore",
  "relevant_for": ["jack", "friend1"],
  "reason": "short reason"
}
```

- URGENT when any of: presale, pre-registration, sign-up, register, waitlist / waiting list,
  second date / extra show / added date, extra tickets / final release, resale / ticketswap,
  "link in bio" + ADE, secret / special guest reveal for a must_see artist, or any post by a
  `priority: urgent` account in accounts.yaml that is ADE related.
- Deterministic matching first (cheap), Claude for flyers and fuzzy cases. Artist matching:
  casefold, strip accents, strip "(GB)" style suffixes and "live / all night long / extended",
  split on b2b, &, x, vs, presents, pres. Short names (<= 4 chars) exact match only.
- Log token usage per run. Hard cap: stop calling Claude after N calls per run (default 60).

## Alert routing

- Each recipient has their own taste block. Only send an alert to recipients it is relevant
  for. Urgent alerts for priority accounts go to everyone.
- Map to template: {{1}} event + key artists, {{2}} day/time or "TBA", {{3}} venue or "TBA",
  {{4}} "URGENT: presale sign-up open: <link>" style detail + best link.
- Daily 09:00 heartbeat to WhatsApp (to Jack only) and ntfy: runs ok/failed per source,
  posts checked, alerts sent, Claude calls. If a source failed 3 runs in a row, alert ntfy.
- Daily 09:00 digest of suggested follows (to Jack only).

## Data model (SQLite)

- `items` (id, source, external_id, url, author, text, screenshot_path, first_seen, raw_json)
- `classifications` (item_id, json, model, created_at)
- `events` (canonical event, venue, date, artists, ticket_status, sources)
- `alerts` (item_id, reason_hash, recipient, sent_at, wa_message_id, status)
- `runs` (source, started, finished, ok, error, counts)
- `ig_seen` (permalink, author, seen_at)
- `suggested_follows` (handle, times_seen, seen_via, first_seen, suggested_at)
- Dedupe events across sources: same date + venue + artist overlap, or very similar title.

## Repo layout

```
ade-radar/
  CLAUDE.md
  START_HERE.md
  .env.example
  pyproject.toml
  config/taste.yaml          # people, artists, brands, keywords
  config/accounts.yaml       # accounts the separate Instagram account follows
  src/ade_radar/
    models.py  db.py  config.py  cli.py
    sources/instagram.py  sources/gmail.py  sources/ade.py
    classify.py  match.py  normalize.py  dedupe.py
    notify/whatsapp.py  notify/ntfy.py
    tools/follow_slowly.py  tools/check_template.py
  tests/fixtures/  tests/test_*.py
  scripts/*.ps1             # Task Scheduler setup, run wrappers
  data/                     # sqlite, screenshots, ig_profile, logs (gitignored)
```

## CLI

- `ade-radar ig-login` : open Chrome with the persistent profile for manual login
- `ade-radar run --source instagram|gmail|ade|all [--dry-run]`
- `ade-radar test-whatsapp` : send hello_world (or ade_alert with sample values) to all recipients
- `ade-radar check-template` : print ade_alert status, language and components from Meta
- `ade-radar heartbeat` / `ade-radar digest`
- `ade-radar follow-slowly --file config/handles_tier2.txt` : 10 to 15 follows per hour,
  random waits, stop on any Instagram warning

## Scheduling (Windows Task Scheduler)

- Instagram: every 60 min. Gmail: every 15 to 30 min. ADE programme: every 30 min
  (every 15 min from 19 Oct). Heartbeat + digest: 09:00.
- Settings: run only when user is logged on (headed browser needs the desktop session),
  run as soon as possible after a missed start, stop if running longer than 15 min,
  do not start a new instance if one is running.
- Create tasks with a PowerShell script in `scripts/` so they are reproducible.
- Laptop is already set to never sleep on AC and Windows Update is paused.
- Logs to `data/logs/` with daily rotation.

## Known ADE 2026 facts (useful for tests and the golden list)

- Josh Baker (brand You&Me): Gashouder Presents, Sun 25 Oct, sold out (waitlist),
  with Marsolo, Alexandria, Marie Montexier.
- Prophecy by Prospa: Awakenings, Sunday 25 Oct closing, SugarFactory (Halfweg).
- Kyle Starkey Invites x Overbruggen: Pllek (NDSM), sold out.
- DGTL Opening Day (Thu 22 Oct, NDSM): Benwal, LAMMER, Paige Tomlinson, Helena Lauwaert,
  DJ EZ, Dr Dubplate, Locky, MPH.
- Pegassi All Day Long: sold out.
- CamelPhat pres. EXPERIENCE x Loveland. Paradise x Loveland. Glitterbox and Defected at
  Thuishaven.
- Social Pleasure (Paige Tomlinson) ADE pop-up: Instagram only, no details yet. TOP PRIORITY.

## Build order (each step: tests + dry run before moving on)

1. Project setup, config loading, SQLite schema, `.env.example`.
2. WhatsApp sender + `test-whatsapp` + `check-template` + ntfy. Verify a real message arrives.
3. Instagram login + feed watcher + screenshots + Claude classification + alerts.
   Test: Jack posts a fake flyer from his own account (which the separate account follows).
4. Gmail reader + classification. Test: forward an old event email to the +ade address.
5. Task Scheduler scripts + heartbeat + suggested-follows digest. Run overnight.
6. ADE programme source + dedupe.
7. follow-slowly tool for Tier 2 accounts.
8. Optional: RA, ticket page sold-out checks.

When unsure about an Instagram or Meta detail, look at the live page with Playwright,
save a fixture, and build against that. Ask Jack before anything that could get the
separate Instagram account blocked (bulk actions, high frequency).
