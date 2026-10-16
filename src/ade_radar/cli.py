"""ade-radar command line."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import logging_setup
from .config import load_context

log = logging.getLogger("ade_radar")
SOURCES = ("instagram", "gmail", "ade")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Windows consoles/redirects default to cp1252; captions have emoji
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="ade-radar", description="ADE 2026 alert radar")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create data/ and the SQLite database")
    sub.add_parser("ig-login", help="open Chrome with the radar profile to log in by hand")
    sub.add_parser("ig-inspect", help="save the live Following feed as fixtures (development)")
    r = sub.add_parser("run", help="run one or all sources")
    r.add_argument("--source", default="all", type=_sources,
                   help="instagram, gmail, ade, all, or a comma list such as instagram,gmail")
    r.add_argument("--dry-run", action="store_true", help="print WhatsApp messages instead of sending")
    r.add_argument("--force", action="store_true", help="ignore the Instagram once-per-hour guard")
    r.add_argument("--full", action="store_true",
                   help="ADE: read every event page now (takes ~15-30 min); sends no messages")
    t = sub.add_parser("test-whatsapp", help="send hello_world (and ade_alert if approved)")
    t.add_argument("--to", help="only this recipient name, e.g. jack")
    ct = sub.add_parser("check-template", help="print a template's status, language and components from Meta")
    ct.add_argument("--name", help="template name (default: WHATSAPP_TEMPLATE, or ade_alert)")
    for name in ("heartbeat", "digest"):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true")
    f = sub.add_parser("follow-slowly", help="follow Tier 2 accounts slowly")
    f.add_argument("--file", default="config/handles_tier2.txt")
    f.add_argument("--propose", action="store_true", help="look up handles and write --file for review")
    sub.add_parser("ade-report", help="programme events with the group's artists, pop-ups first")
    sub.add_parser("ade-discover", help="find the JSON endpoint behind the ADE filter page")
    sub.add_parser("meta-debug", help="ask Meta about the token, app, WhatsApp account and number")
    sub.add_parser("status", help="last runs per source and alert counts")
    sub.add_parser("email-report", help="per sender: are the ADE emails timely and useful?")

    args = ap.parse_args(argv)
    ctx = load_context()
    s = ctx.settings
    logging_setup.setup(s, args.verbose)

    if args.cmd == "init":
        from . import db
        db.connect(s.db_path).close()
        print(f"database ready: {s.db_path}")
        print(f"recipients: {', '.join(s.recipients) or '(none set in .env)'}")
        missing = [p for p in s.recipients if p not in ctx.taste.people]
        if missing:
            print(f"!! recipients without a taste block in config/taste.yaml: {', '.join(missing)}")
        return 0
    if args.cmd == "ig-login":
        from .sources import instagram
        instagram.login(ctx)
        return 0
    if args.cmd == "ig-inspect":
        from .sources import instagram
        print(f"saved to {instagram.inspect(ctx)}")
        return 0
    if args.cmd == "run":
        from .pipeline import run_source
        ok, results = True, []
        for src in args.source:
            res = run_source(ctx, src, dry_run=args.dry_run, force=args.force, full=args.full)
            logging.getLogger(__name__).debug("%s: %s", src, json.dumps(res, sort_keys=True))
            ok &= res["ok"]
            results.append((src, res))
        text = run_summary(ctx, results, dry_run=args.dry_run)
        print("\n" + text)
        if not args.dry_run:  # every real (scheduled) run, so you can look back: data\logs\summary.log
            s.logs_dir.mkdir(parents=True, exist_ok=True)
            with open(s.logs_dir / "summary.log", "a", encoding="utf-8") as fh:
                fh.write(text + "\n\n")
        return 0 if ok else 1
    if args.cmd == "test-whatsapp":
        from .tools.check_template import test_whatsapp
        test_whatsapp(ctx, args.to)
        return 0
    if args.cmd == "meta-debug":
        from .tools.check_template import meta_debug
        meta_debug(ctx)
        return 0
    if args.cmd == "check-template":
        from .tools.check_template import check_template
        return 0 if check_template(ctx, args.name) else 1
    if args.cmd == "heartbeat":
        from .reports import heartbeat
        print(heartbeat(ctx, args.dry_run))
        return 0
    if args.cmd == "digest":
        from .reports import digest
        print(digest(ctx, args.dry_run))
        return 0
    if args.cmd == "follow-slowly":
        from .tools import follow_slowly
        path = Path(args.file)
        path = path if path.is_absolute() else s.root / path
        if args.propose:
            follow_slowly.propose(ctx, path)
        elif not path.exists():
            print(f"{path} not found. Run with --propose first.")
            return 1
        else:
            follow_slowly.follow(ctx, path)
        return 0
    if args.cmd == "ade-discover":
        from .sources import ade
        print(f"saved to {ade.discover(ctx)}")
        return 0
    if args.cmd == "ade-report":
        from . import db
        from .sources import ade
        conn = db.connect(s.db_path)
        text = ade.report(ctx, conn)
        conn.close()
        out = s.data_dir / "ade_report.txt"
        out.write_text(text, encoding="utf-8")
        print(text)
        print(f"\n(saved to {out})")
        return 0
    if args.cmd == "email-report":
        from . import db
        from .reports import email_report
        conn = db.connect(s.db_path)
        print(email_report(conn))
        conn.close()
        return 0
    if args.cmd == "status":
        return status(ctx)
    return 1


def _sources(value: str) -> list[str]:
    names = SOURCES if value == "all" else [v.strip() for v in value.split(",") if v.strip()]
    bad = [n for n in names if n not in SOURCES]
    if bad or not names:
        raise argparse.ArgumentTypeError(f"unknown source {', '.join(bad) or value!r}; use {', '.join(SOURCES)} or all")
    return list(dict.fromkeys(names))


def _summary_line(src: str, r: dict) -> str:
    if not r.get("ok"):
        return f"{src:9} FAILED: {r.get('error', '')[:120]}"
    if r.get("skipped_rate_limit"):
        return f"{src:9} skipped: checked less than 25 min ago (OK, next scheduled run does it)"
    if src == "instagram":
        stop = r.get("story_stop", "")
        found = (f"{r.get('posts_checked', 0)} posts checked ({r.get('posts_new', 0)} new), "
                 f"{r.get('story_frames', 0)} stories viewed ({r.get('stories_new', 0)} new)"
                 + (f", stories: {stop}" if stop else ""))
    elif src == "gmail":
        found = f"{r.get('mails_found', 0)} mails in the label, {r.get('mails_new', 0)} new"
    elif src == "ade":
        found = (f"{r.get('events_listed', 0)} events listed, {r.get('events_fetched', 0)} event pages read, "
                 f"{r.get('changes_alerted', 0)} changes")
    else:
        found = ""
    sent, failed, dry = r.get("alerts_sent", 0), r.get("alerts_failed", 0), r.get("alerts_dry_run", 0)
    alerts = f"{dry} alerts (dry run, not sent)" if dry else f"{sent} WhatsApp alerts sent"
    if failed:
        alerts += f", {failed} FAILED to send"
    return (f"{src:9} OK: {found}\n{'':11}{alerts}, {r.get('teasers', 0)} for the digest, "
            f"{r.get('claude_calls', 0)} Claude checks (EUR {r.get('claude_cost_eur', 0):.3f})")


def run_summary(ctx, results: list[tuple[str, dict]], dry_run: bool = False) -> str:
    """Readable result of a run: did each source work, what was found, what was sent, what it cost."""
    from datetime import datetime
    from . import db
    from .config import TZ

    ok = all(r.get("ok") for _, r in results)
    head = (f"=== ade-radar run {datetime.now(TZ):%a %d %b %H:%M}"
            f"{' (DRY RUN)' if dry_run else ''}: {'ALL OK' if ok else 'PROBLEM'} ===")
    lines = [head, *(_summary_line(src, r) for src, r in results)]
    try:
        conn = db.connect(ctx.settings.db_path)
        today, total = db.spend_so_far(conn)
        conn.close()
        s = ctx.settings
        lines.append(f"Claude spend: EUR {today:.2f} today (cap {s.claude_budget_daily:.2f}), "
                     f"EUR {total:.2f} in total (cap {s.claude_budget_total:.2f})")
    except Exception:  # noqa: BLE001 - the summary must never fail the run
        pass
    return "\n".join(lines)


def status(ctx) -> int:
    from . import db
    conn = db.connect(ctx.settings.db_path)
    for src in SOURCES:
        rows = db.recent_runs(conn, src, 3)
        for r in rows:
            print(f"{src:10} {r['started']}  ok={r['ok']}  {(r['error'] or '')[:80]}  {r['counts']}")
        if not rows:
            print(f"{src:10} never ran")
        cut = rows[0]["started"] if rows else ""
        hung = conn.execute("SELECT started FROM runs WHERE source=? AND finished IS NULL AND started>?"
                            " ORDER BY id DESC LIMIT 5", (src, cut)).fetchall()
        for r in hung:
            print(f"{src:10} {r['started']}  NOT FINISHED (still running, or stopped by the 15-min task limit)")
    n = conn.execute("SELECT status, COUNT(*) c FROM alerts GROUP BY status").fetchall()
    print("alerts: " + ", ".join(f"{r['status']}={r['c']}" for r in n))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
