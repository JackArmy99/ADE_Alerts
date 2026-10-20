"""SQLite storage: one file at data/radar.sqlite."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import Item, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT,
    author TEXT,
    text TEXT,
    screenshot_path TEXT,
    first_seen TEXT NOT NULL,
    raw_json TEXT,
    UNIQUE (source, external_id)
);
CREATE TABLE IF NOT EXISTS classifications (
    item_id TEXT NOT NULL,
    json TEXT NOT NULL,
    model TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_class_item ON classifications(item_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    venue TEXT,
    date TEXT,
    time TEXT,
    artists TEXT,          -- json list
    ticket_status TEXT,
    sources TEXT,          -- json list of item ids
    ade_id TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS alerts (
    item_id TEXT NOT NULL,
    reason_hash TEXT NOT NULL,
    recipient TEXT NOT NULL,
    urgency TEXT,
    sent_at TEXT,
    wa_message_id TEXT,
    status TEXT,           -- sent | failed | dry_run
    error TEXT,
    PRIMARY KEY (item_id, reason_hash, recipient)
);
CREATE INDEX IF NOT EXISTS ix_alerts_reason ON alerts(reason_hash, recipient);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    ok INTEGER,
    error TEXT,
    counts TEXT
);
CREATE TABLE IF NOT EXISTS ig_seen (
    permalink TEXT PRIMARY KEY,
    author TEXT,
    seen_at TEXT
);
CREATE TABLE IF NOT EXISTS suggested_follows (
    handle TEXT PRIMARY KEY,
    times_seen INTEGER DEFAULT 0,
    seen_via TEXT,         -- comma separated handles that tagged/mentioned it
    first_seen TEXT,
    suggested_at TEXT
);
CREATE TABLE IF NOT EXISTS ade_snapshots (
    ade_id TEXT PRIMARY KEY,
    json TEXT NOT NULL,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS teasers (
    item_id TEXT PRIMARY KEY,
    author TEXT,
    url TEXT,
    summary TEXT,
    persons TEXT,
    created_at TEXT,
    digested_at TEXT
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def dry_run_copy(path: Path) -> Path:
    """Snapshot of the database for a dry run, so a dry run never locks the real one (scheduled runs keep going)."""
    copy = path.with_name("dryrun.sqlite")
    for p in (copy, copy.with_name(copy.name + "-wal"), copy.with_name(copy.name + "-shm")):
        p.unlink(missing_ok=True)
    src = connect(path)
    dst = sqlite3.connect(str(copy))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return copy


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=90)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=90000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn) -> None:
    """Columns added after the first release (databases created earlier lack them)."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
    for col, decl in (("params", "TEXT"), ("attempts", "INTEGER DEFAULT 0")):
        if col not in have:
            conn.execute(f"ALTER TABLE alerts ADD COLUMN {col} {decl}")


def ts() -> str:
    return now().isoformat(timespec="seconds")


# ---- items -------------------------------------------------------------

def item_exists(conn, item: Item) -> bool:
    return conn.execute("SELECT 1 FROM items WHERE id=?", (item.id,)).fetchone() is not None


def save_item(conn, item: Item) -> bool:
    """Insert an item; returns True if it was new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO items (id, source, external_id, url, author, text, screenshot_path, first_seen, raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (item.id, item.source, item.external_id, item.url, item.author, item.text,
         ";".join(item.screenshot_paths), ts(), item.to_json()),
    )
    return cur.rowcount > 0


def save_classification(conn, item_id: str, cls_json: str, model: str) -> None:
    conn.execute("INSERT INTO classifications (item_id, json, model, created_at) VALUES (?,?,?,?)",
                 (item_id, cls_json, model, ts()))


# ---- runs --------------------------------------------------------------

def start_run(conn, source: str) -> int:
    cur = conn.execute("INSERT INTO runs (source, started) VALUES (?,?)", (source, ts()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, ok: bool, counts: dict, error: str = "") -> None:
    conn.execute("UPDATE runs SET finished=?, ok=?, error=?, counts=? WHERE id=?",
                 (ts(), 1 if ok else 0, error[:2000], json.dumps(counts, sort_keys=True), run_id))
    conn.commit()


def recent_runs(conn, source: str, n: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM runs WHERE source=? AND finished IS NOT NULL ORDER BY id DESC LIMIT ?",
                        (source, n)).fetchall()


# ---- kv ----------------------------------------------------------------

def kv_get(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))


# ---- instagram ---------------------------------------------------------

def ig_is_seen(conn, permalink: str) -> bool:
    return conn.execute("SELECT 1 FROM ig_seen WHERE permalink=?", (permalink,)).fetchone() is not None


def ig_mark_seen(conn, permalink: str, author: str) -> None:
    conn.execute("INSERT OR IGNORE INTO ig_seen (permalink, author, seen_at) VALUES (?,?,?)",
                 (permalink, author, ts()))


def ig_seen_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM ig_seen").fetchone()[0]


def save_teaser(conn, item_id: str, author: str, url: str, summary: str, persons: list[str]) -> None:
    conn.execute("INSERT OR IGNORE INTO teasers (item_id, author, url, summary, persons, created_at)"
                 " VALUES (?,?,?,?,?,?)", (item_id, author, url, summary[:200], ",".join(persons), ts()))


def note_suggestion(conn, handle: str, via: str) -> None:
    row = conn.execute("SELECT seen_via FROM suggested_follows WHERE handle=?", (handle,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO suggested_follows (handle, times_seen, seen_via, first_seen) VALUES (?,1,?,?)",
                     (handle, via, ts()))
    else:
        vias = [v for v in (row["seen_via"] or "").split(",") if v]
        if via and via not in vias:
            vias.append(via)
        conn.execute("UPDATE suggested_follows SET times_seen=times_seen+1, seen_via=? WHERE handle=?",
                     (",".join(vias), handle))


# ---- Claude spending ledger (euros) ------------------------------------------

def spend_so_far(conn) -> tuple[float, float]:
    today = now().date().isoformat()
    return float(kv_get(conn, f"claude_spend_{today}", "0") or 0), float(kv_get(conn, "claude_spend_total", "0") or 0)


def add_spend(conn, amount: float) -> None:
    if amount <= 0:
        return
    today_spent, total = spend_so_far(conn)
    kv_set(conn, f"claude_spend_{now().date().isoformat()}", f"{today_spent + amount:.6f}")
    kv_set(conn, "claude_spend_total", f"{total + amount:.6f}")
