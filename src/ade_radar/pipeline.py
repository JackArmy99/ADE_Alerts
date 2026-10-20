"""Run a source end to end: collect -> store -> classify -> match -> dedupe -> alert."""

from __future__ import annotations

import json
import logging
import traceback

from . import db
from .alerts import AlertStats, dispatch, retry_failed
from .classify import Classifier, rules_classification
from .config import Context
from .dedupe import upsert_event
from .match import Matcher, decide
from .models import Item, SourceBlocked, now
from .notify import ntfy
from .notify.whatsapp import WhatsApp

log = logging.getLogger(__name__)
FAIL_STREAK_ALERT = 3


def process_items(conn, ctx: Context, items: list[Item], *, dry_run: bool, classifier: Classifier | None,
                  wa: WhatsApp | None, stats: AlertStats, counts: dict) -> None:
    matcher = Matcher(ctx.taste, ctx.accounts)
    today = now().strftime("%A %d %B %Y")
    for item in items:
        try:
            _process_one(conn, ctx, item, matcher, today, dry_run=dry_run, classifier=classifier, wa=wa,
                         stats=stats, counts=counts)
        except Exception:  # noqa: BLE001 - one bad item must not stop the run
            counts["item_errors"] = counts.get("item_errors", 0) + 1
            log.error("item %s failed:\n%s", item.external_id, traceback.format_exc())
            if not dry_run:
                conn.rollback()
            continue
        if item.source.startswith("instagram"):
            db.ig_mark_seen(conn, item.external_id, item.author)
        if not dry_run:
            conn.commit()


def _process_one(conn, ctx: Context, item: Item, matcher: Matcher, today: str, *, dry_run: bool,
                 classifier: Classifier | None, wa: WhatsApp | None, stats: AlertStats, counts: dict) -> None:
    if not db.save_item(conn, item):
        counts["duplicates"] = counts.get("duplicates", 0) + 1
        return
    counts["new_items"] = counts.get("new_items", 0) + 1
    m = matcher.match(item.text, item.author, item.tagged)
    cls = classifier.classify(item, today) if classifier else None
    if cls is None:
        cls = rules_classification(item, m)
    db.save_classification(conn, item.id, cls.to_json(), cls.model)
    # Claude may name artists that appear only in the flyer image: match those too.
    if cls.artists or cls.event_name:
        m2 = matcher.match(" ".join([item.text, cls.event_name or "", ", ".join(cls.artists)]), item.author,
                           item.tagged)
        m.hits.update({p: h for p, h in m2.hits.items() if p not in m.hits})
    d = decide(item, cls, m, ctx)
    log.info("%s %s @%s -> %s %s %s | %s | %s", item.source, item.external_id, item.author,
             "ALERT" if d.send else "skip", d.urgency, d.recipients or "", d.reason or cls.reason,
             cls.event_name or "")
    if d.urgency == "teaser":
        summary = cls.event_name or ", ".join(cls.artists[:3]) or ""
        db.save_teaser(conn, item.id, item.author, item.url, f"{summary} - {d.reason}".strip(" -"), d.recipients)
        counts["teasers"] = counts.get("teasers", 0) + 1
    if not d.send:
        return
    event_id, _ = upsert_event(conn, cls, item.id)
    dispatch(conn, ctx, wa, item, cls, d, event_id, stats, dry_run)


def run_source(ctx: Context, source: str, dry_run: bool = False, force: bool = False, full: bool = False) -> dict:
    """Runs one source with run bookkeeping. Dry runs work on a copy of the database (data/dryrun.sqlite), so they
    change nothing and never block the scheduled runs; only the Claude spend is added to the real database."""
    s = ctx.settings
    conn = db.connect(db.dry_run_copy(s.db_path) if dry_run else s.db_path)
    run_id = db.start_run(conn, source)
    counts: dict = {}
    stats = AlertStats()
    classifier = Classifier(ctx)
    classifier.spent_today, classifier.spent_total = db.spend_so_far(conn)
    wa = None if dry_run else WhatsApp(s)
    ok, error = True, ""
    try:
        if source == "instagram":
            from .sources import instagram
            items = instagram.collect(ctx, conn, counts, force=force, dry_run=dry_run)
            process_items(conn, ctx, items, dry_run=dry_run, classifier=classifier, wa=wa, stats=stats,
                          counts=counts)
            instagram.record_suggestions(conn, ctx, items)
        elif source == "gmail":
            from .sources import gmail
            items = gmail.collect(ctx, conn, counts)
            process_items(conn, ctx, items, dry_run=dry_run, classifier=classifier, wa=wa, stats=stats,
                          counts=counts)
        elif source == "ade":
            from .sources import ade
            ade.run(ctx, conn, counts, wa=wa, stats=stats, dry_run=dry_run, full=full)
        else:
            raise ValueError(f"unknown source {source}")
    except SourceBlocked as e:
        ok, error = False, f"blocked: {e}"
        log.error("%s blocked: %s", source, e)
        ntfy.error(s, f"{source} stopped: {e}. Will try again at the next scheduled run.", dry_run=dry_run)
    except Exception as e:  # noqa: BLE001 - a source must never crash the scheduler loop
        ok, error = False, f"{type(e).__name__}: {e}"
        log.error("%s failed:\n%s", source, traceback.format_exc())
    finally:
        if dry_run or not ok:
            conn.rollback()  # unprocessed items stay unseen and are picked up next run
        else:
            conn.commit()
    if wa is not None:
        try:
            retry_failed(conn, ctx, wa, stats)
        except Exception:  # noqa: BLE001
            log.error("retrying failed alerts failed:\n%s", traceback.format_exc())
    counts.update({"alerts_" + k: v for k, v in stats.as_dict().items()})
    counts.update({"claude_" + k: v for k, v in classifier.usage.as_dict().items()})
    if dry_run:  # real money, also in dry runs: count it in the real database
        real = db.connect(s.db_path)
        db.add_spend(real, classifier.usage.cost)
        real.commit()
        real.close()
    else:
        db.add_spend(conn, classifier.usage.cost)
    conn.commit()
    _budget_notices(conn, ctx, classifier, dry_run)
    db.finish_run(conn, run_id, ok, counts, error)
    log.info("run %s finished ok=%s counts=%s", source, ok, json.dumps(counts, sort_keys=True))
    if stats.failed >= 3:
        ntfy.error(s, f"{stats.failed} WhatsApp sends failed in the {source} run. Run: ade-radar check-template",
                   dry_run=dry_run)
    if not ok:
        check_fail_streak(conn, ctx, source, dry_run)
    conn.close()
    return {"ok": ok, "error": error, **counts}


def _budget_notices(conn, ctx: Context, classifier: Classifier, dry_run: bool) -> None:
    """ntfy once per day when the Claude budget is 80% used or reached."""
    s = ctx.settings
    today, total = db.spend_so_far(conn)
    day = now().date().isoformat()
    if classifier.usage.skipped_budget and db.kv_get(conn, f"budget_cap_notified_{day}") is None:
        ntfy.error(s, f"Claude budget reached (today EUR {today:.2f} of {s.claude_budget_daily:.2f}, total "
                   f"EUR {total:.2f} of {s.claude_budget_total:.2f}). Using keyword rules only until it resets.",
                   dry_run=dry_run)
        db.kv_set(conn, f"budget_cap_notified_{day}", "1")
    elif total >= 0.8 * s.claude_budget_total and db.kv_get(conn, "budget_80_notified") is None:
        ntfy.send(s, f"Claude spend EUR {total:.2f} of the EUR {s.claude_budget_total:.2f} cap.",
                  title="ade-radar budget 80%", priority="high", dry_run=dry_run)
        db.kv_set(conn, "budget_80_notified", "1")
    conn.commit()


def check_fail_streak(conn, ctx: Context, source: str, dry_run: bool = False) -> None:
    runs = db.recent_runs(conn, source, FAIL_STREAK_ALERT + 1)
    failed = [r for r in runs[:FAIL_STREAK_ALERT] if not r["ok"]]
    streak_starts_now = len(runs) <= FAIL_STREAK_ALERT or runs[FAIL_STREAK_ALERT]["ok"]
    if len(failed) == FAIL_STREAK_ALERT and streak_starts_now:
        ntfy.error(ctx.settings, f"{source} failed {FAIL_STREAK_ALERT} runs in a row. Last error: "
                   f"{runs[0]['error'][:300]}", dry_run=dry_run)
