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
    r.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    r.add_argument("--dry-run", action="store_true", help="print WhatsApp messages instead of sending")
    r.add_argument("--force", action="store_true", help="ignore the Instagram once-per-hour guard")
    t = sub.add_parser("test-whatsapp", help="send hello_world (and ade_alert if approved)")
    t.add_argument("--to", help="only this recipient name, e.g. jack")
    sub.add_parser("check-template", help="print ade_alert status, language and components from Meta")
    for name in ("heartbeat", "digest"):
        p = sub.add_parser(name)
        p.add_argument("--dry-run", action="store_true")
    f = sub.add_parser("follow-slowly", help="follow Tier 2 accounts slowly")
    f.add_argument("--file", default="config/handles_tier2.txt")
    f.add_argument("--propose", action="store_true", help="look up handles and write --file for review")
    sub.add_parser("ade-discover", help="find the JSON endpoint behind the ADE filter page")
    sub.add_parser("status", help="last runs per source and alert counts")

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
        ok = True
        for src in (SOURCES if args.source == "all" else (args.source,)):
            res = run_source(ctx, src, dry_run=args.dry_run, force=args.force)
            print(f"{src}: {json.dumps(res, sort_keys=True)}")
            ok &= res["ok"]
        return 0 if ok else 1
    if args.cmd == "test-whatsapp":
        from .tools.check_template import test_whatsapp
        test_whatsapp(ctx, args.to)
        return 0
    if args.cmd == "check-template":
        from .tools.check_template import check_template
        return 0 if check_template(ctx) else 1
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
    if args.cmd == "status":
        return status(ctx)
    return 1


def status(ctx) -> int:
    from . import db
    conn = db.connect(ctx.settings.db_path)
    for src in SOURCES:
        rows = db.recent_runs(conn, src, 3)
        for r in rows:
            print(f"{src:10} {r['started']}  ok={r['ok']}  {(r['error'] or '')[:80]}  {r['counts']}")
        if not rows:
            print(f"{src:10} never ran")
    n = conn.execute("SELECT status, COUNT(*) c FROM alerts GROUP BY status").fetchall()
    print("alerts: " + ", ".join(f"{r['status']}={r['c']}" for r in n))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
