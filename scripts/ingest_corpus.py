#!/usr/bin/env python3
"""Ingest corpus.jsonl into a local SQLite database with full-text search.

This is the firm-side half of the knowledge pipeline: each build publishes a
rolling ~3,000-item window as the `corpus` artifact; this script merges any
number of those files into ONE cumulative, queryable store, so history
accumulates even though every individual artifact rotates.

Schema (table `items`):
    guid            TEXT PRIMARY KEY   — link#itemid (stable across builds)
    url, title      TEXT
    content_text    TEXT               — extracted body (truncated upstream)
    date_published  TEXT               — best-effort publication timestamp (ISO)
    first_seen      TEXT               — when the monitor first observed it
    source_id/name/category/subcategory TEXT
    roles           TEXT               — JSON array of practice-area role ids
    docket          TEXT               — semicolon-joined dockets (or NULL)
    ld              INTEGER            — Maine Legislative Document number
    effective_date  TEXT
    pdf_link        INTEGER, minor_change INTEGER
    ingested_at     TEXT               — last ingest touch (UTC)

Full text lives in FTS5 table `items_fts` (title + content_text), kept in
sync by triggers. Re-ingesting the same guid updates in place — safe to run
after every build.

Usage:
    python scripts/ingest_corpus.py --db firm_corpus.db --corpus corpus.jsonl
    python scripts/ingest_corpus.py --db firm_corpus.db --corpus 'corpus-dl/*.jsonl'

Query examples:
    sqlite3 firm_corpus.db "SELECT title,url FROM items_fts JOIN items USING(rowid)
        WHERE items_fts MATCH 'shoreland zoning' ORDER BY date_published DESC LIMIT 10"
    sqlite3 firm_corpus.db "SELECT title FROM items WHERE docket LIKE '%25-cv-%'
        ORDER BY first_seen DESC LIMIT 5"
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    rowid_index INTEGER PRIMARY KEY,
    guid TEXT NOT NULL UNIQUE,
    url TEXT, title TEXT,
    content_text TEXT,
    date_published TEXT, first_seen TEXT,
    source_id TEXT, source_name TEXT, category TEXT, subcategory TEXT,
    roles TEXT,
    docket TEXT, ld INTEGER, effective_date TEXT,
    pdf_link INTEGER DEFAULT 0, minor_change INTEGER DEFAULT 0,
    ingested_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items(first_seen);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_id);
CREATE INDEX IF NOT EXISTS idx_items_docket ON items(docket);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, content_text, content='', contentless_delete=1
);
"""

INSERT_ITEM = """
INSERT INTO items (guid, url, title, content_text, date_published, first_seen,
                   source_id, source_name, category, subcategory, roles,
                   docket, ld, effective_date, pdf_link, minor_change, ingested_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(guid) DO UPDATE SET
    title=excluded.title, content_text=excluded.content_text,
    date_published=excluded.date_published,
    roles=excluded.roles, docket=excluded.docket, ld=excluded.ld,
    effective_date=excluded.effective_date,
    minor_change=excluded.minor_change, ingested_at=excluded.ingested_at
"""


def _norm_row(raw: dict) -> tuple | None:
    guid = raw.get("id")
    if not guid:
        return None
    meta = raw.get("_meta") or {}
    dockets = meta.get("docket") or []
    return (
        guid,
        raw.get("url"),
        raw.get("title"),
        raw.get("content_text"),
        raw.get("date_published"),
        raw.get("_first_seen"),
        raw.get("source_id"),
        raw.get("source_name"),
        raw.get("category"),
        raw.get("subcategory"),
        json.dumps(raw.get("roles") or []),
        ";".join(dockets) if dockets else None,
        int(meta["ld"]) if isinstance(meta.get("ld"), int) else None,
        meta.get("effective_date"),
        1 if raw.get("pdf_link") else 0,
        1 if raw.get("_minor_change") else 0,
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def ingest(db_path: Path, corpus_paths: list[Path]) -> dict:
    con = sqlite3.connect(db_path)
    con.executescript(SCHEMA)
    cur = con.cursor()
    stats = {"files": 0, "rows_read": 0, "inserted": 0, "updated": 0}

    for path in corpus_paths:
        # Accept concrete files or glob patterns; patterns resolve relative
        # to the pattern's own parent directory (absolute or relative both work).
        if path.exists():
            matches = [path]
        else:
            matches = sorted(path.parent.glob(path.name))
        if not matches:
            print(f"skip (no match): {path}")
            continue
        for real in matches:
            _ingest_one(cur, stats, real)

    con.commit()
    total = cur.execute("SELECT count(*) FROM items").fetchone()[0]
    con.close()
    stats["total_items"] = total
    return stats


def _ingest_one(cur: sqlite3.Cursor, stats: dict, path: Path) -> None:
    if not path.exists():
        print(f"skip (missing): {path}")
        return
    stats["files"] += 1
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = _norm_row(raw)
        if row is None:
            continue
        stats["rows_read"] += 1
        prior = cur.execute(
            "SELECT rowid_index, coalesce(title,''), coalesce(content_text,'') "
            "FROM items WHERE guid = ?",
            (row[0],),
        ).fetchone()
        cur.execute(INSERT_ITEM, row)
        rid = cur.execute("SELECT rowid_index FROM items WHERE guid = ?", (row[0],)).fetchone()[0]
        # Keep the FTS mirror in sync: remove stale text, index current.
        if prior is not None:
            cur.execute("DELETE FROM items_fts WHERE rowid = ?", (prior[0],))
            stats["updated"] += 1
        else:
            stats["inserted"] += 1
        cur.execute(
            "INSERT INTO items_fts(rowid, title, content_text) VALUES(?, ?, ?)",
            (rid, row[2] or "", row[3] or ""),
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=Path("firm_corpus.db"))
    ap.add_argument("--corpus", type=Path, nargs="+", default=[Path("corpus.jsonl")],
                    help="corpus.jsonl file(s) or glob patterns")
    args = ap.parse_args()

    stats = ingest(args.db, args.corpus)
    print(
        f"Ingested {stats['rows_read']} rows from {stats['files']} file(s) "
        f"({stats['inserted']} new, {stats['updated']} updated); "
        f"{stats['total_items']} total items in {args.db}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
