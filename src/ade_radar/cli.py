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
                   help="ADE: read every event page now (takes ~15-30 min); only brand-new events alert")
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
        ok, summary = True, []
        for src in args.source:
            res = run_source(ctx, src, dry_run=args.dry_run, force=args.force, full=args.full)
            print(f"{src}: {json.dumps(res, sort_keys=True)}")
            ok &= res["ok"]
            summary.append(_summary_line(src, res))
        if len(args.source) > 1:
            print("\n=== summary ===\n" + "\n".join(summary))
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
    found = {"instagram": f"{r.get('posts_new', 0)} posts, {r.get('stories_new', 0)} stories",
             "gmail": f"{r.get('mails_new', 0)} mails ({r.get('mails_security_skipped', 0)} security skipped)",
             "ade": f"{r.get('events_listed', 0)} events listed"}.get(src, "")
    alerts = r.get("alerts_sent", 0) + r.get("alerts_dry_run", 0)
    return (f"{src:9} ok: {found}; {r.get('new_items', 0)} new, {alerts} alerts"
            f"{' (dry run)' if r.get('alerts_dry_run') else ''}, {r.get('teasers', 0)} teasers, "
            f"{r.get('claude_calls', 0)} Claude calls")


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
