# START HERE

## Status (12 Oct 2026)

Done:
- Meta app "ADE radar" + WhatsApp test number, permanent System User token (in Keeper)
- Test message to Jack's phone worked
- Template `ade_alert` submitted, waiting for approval

Still to do outside Claude Code:
- [ ] Add the other 4 phone numbers in Meta API Setup (each reads you their code)
- [ ] Gmail: filter for the `+ade` address -> label `ADE`, skip inbox. Create an app password
- [ ] Separate Instagram account (sign up with the +ade address), follow Tier 1 + Tier 3
      from config/accounts.yaml by hand, in 2 to 3 batches with breaks
- [ ] RA + Dice (+ Shotgun optional) with the +ade address, connect Spotify, follow promoters/venues
- [ ] Safety net: post + story notifications in your own Instagram app for Social Pleasure,
      Josh Baker, Kyle Starkey, Prospa, Pegassi
- [ ] Fill in the other 4 people in config/taste.yaml
- [ ] Get an Anthropic API key (console.anthropic.com) for the flyer reading

## Setup

1. Create a folder, e.g. `C:\ade-radar`, and copy CLAUDE.md, START_HERE.md and `config/` into it.
2. Create `.env` from the variable list in CLAUDE.md and paste the secrets
   (WhatsApp token from Keeper, Anthropic key, Gmail app password, ntfy topic, the 5 numbers).
3. Open a terminal in the folder and start `claude`.

## Prompts to give Claude Code, in order

Paste one at a time. Do not move on until the step's test passes.

### 1. Setup
Read CLAUDE.md, START_HERE.md and the config folder fully. Then in plan mode propose the
project structure, SQLite schema and module boundaries. After I approve, set up the project
on native Windows (Python 3.12), create .env.example, .gitignore, config loading, the
database and a `cli.py` skeleton with the commands listed in CLAUDE.md. Initialise git.

### 2. WhatsApp
Build notify/whatsapp.py and notify/ntfy.py per CLAUDE.md. Add `check-template` (prints
the status, language and components of ade_alert from Meta) and `test-whatsapp`
(sends hello_world to all recipients; if ade_alert is Active, also send it with sample
values). Run check-template, then test-whatsapp to my number only first.

### 3. Instagram login and feed watcher
Build `ig-login` (opens installed Chrome with the persistent profile so I log in by hand
to the separate account). Then use Playwright to inspect the live Following feed and stories
tray, save fixtures, and build sources/instagram.py: collect new posts and stories since the
last run, screenshot each, record permalink, author, caption and tagged accounts. Max one
run per hour, stop on any login wall or warning. Show me a dry run.

### 4. Classification and alerts
Build classify.py (Claude Haiku with vision, cached system prompt from taste.yaml, strict
JSON per CLAUDE.md), match.py and the alert routing. Connect Instagram -> classify -> WhatsApp
with idempotent alerts. Test: I will post a fake ADE flyer with "presale sign-up, link in bio"
from my own account; it must arrive as an URGENT WhatsApp alert within one run.

### 5. Gmail
Build sources/gmail.py (IMAP, only the ADE label, track UIDs). Classify mails the same way.
Test: I forward an old event email to the +ade address.

### 6. Scheduling and heartbeat
Write PowerShell scripts that create the Task Scheduler jobs per CLAUDE.md, plus the 09:00
heartbeat and suggested-follows digest. Show me how to check the tasks are running and
where the logs are. Then we let it run overnight.

### 7. ADE programme
Discover the JSON behind the ADE filter page (network capture), fall back to HTML. Build
sources/ade.py, dedupe with Instagram/Gmail items, and alert on matched new events, lineup
changes, sold out and set times.

### 8. Slow follow tool
Build tools/follow_slowly.py for Tier 2 in accounts.yaml: propose handles first and let me
confirm them, then follow 10 to 15 per hour with random waits, stop at any Instagram warning.

## If something breaks during ADE week
- No heartbeat at 09:00: check the laptop is on, logged in, plugged in; check data/logs.
- Instagram errors: open Chrome with `ade-radar ig-login`, clear any checkpoint by hand.
- WhatsApp errors: run `ade-radar check-template` and `test-whatsapp`; check the token in .env.
