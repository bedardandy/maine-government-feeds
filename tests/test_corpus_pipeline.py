"""Tests for the firm knowledge-pipeline consumer tools (#2 of the audit):

* write_corpus.py emits a sha256 sidecar that matches the file
* ingest_corpus.py: insert → update round-trip with FTS5 staying in sync,
  structured metadata columns, minor-change/pdf flags, glob input support
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

import write_corpus
from ingest_corpus import ingest


# --------------------------------------------------------------------------- #
# write_corpus sidecar
# --------------------------------------------------------------------------- #
def test_write_corpus_creates_matching_sha256(tmp_path, monkeypatch):
    monkeypatch.setattr(common := __import__("common"), "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(
        write_corpus,
        "load_sources",
        lambda: [
            {"id": "s1", "name": "S One", "category": "Cat", "subcategory": "Sub", "url": "https://x"}
        ],
    )
    (tmp_path / "state").mkdir()
    state = {
        "id": "s1",
        "items": [
            {
                "id": "abc",
                "title": "T",
                "link": "https://x/t",
                "summary": "body",
                "published": "2026-07-01T00:00:00+00:00",
                "first_seen": "2026-07-01T00:00:00+00:00",
            }
        ],
    }
    (tmp_path / "state" / "s1.json").write_text(json.dumps(state))

    out = tmp_path / "corpus.jsonl"
    write_corpus.main.__wrapped__ if False else None
    import sys as _sys

    _sys.argv = ["write_corpus.py", "--output", str(out)]
    assert write_corpus.main() == 0
    body = out.read_bytes()
    sidecar = out.with_name("corpus.jsonl.sha256")
    expected = hashlib.sha256(body).hexdigest()
    assert sidecar.read_text().startswith(expected)
    rows = [json.loads(l) for l in body.decode().splitlines()]
    assert len(rows) == 1 and rows[0]["source_id"] == "s1"


# --------------------------------------------------------------------------- #
# ingest_corpus
# --------------------------------------------------------------------------- #
def _corpus_file(tmp_path, rows):
    p = tmp_path / f"corpus-{len(rows)}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


ROWS = [
    {
        "id": "https://x/1#a1",
        "url": "https://x/1",
        "title": "Shoreland zoning order",
        "content_text": "Amended Chapter 1000 shoreland zoning rules.",
        "date_published": "2026-08-01T00:00:00+00:00",
        "_first_seen": "2026-08-01T05:00:00+00:00",
        "source_id": "s1",
        "source_name": "S One",
        "category": "C",
        "subcategory": "Sub",
        "roles": ["environmental-land-use"],
        "_meta": {"docket": ["2:25-cv-00264"], "ld": 1234, "effective_date": "October 1, 2026"},
    },
    {
        "id": "https://x/2#b2",
        "url": "https://x/2.pdf",
        "title": "Fee schedule update",
        "content_text": "tiny",
        "_first_seen": "2026-08-02T05:00:00+00:00",
        "source_id": "s1",
        "roles": [],
        "pdf_link": True,
        "_minor_change": True,
    },
]


def test_ingest_insert_update_and_fts_sync(tmp_path):
    db = tmp_path / "t.db"
    corpus = _corpus_file(tmp_path, ROWS)

    stats1 = ingest(db, [corpus])
    assert stats1["inserted"] == 2 and stats1["updated"] == 0 and stats1["total_items"] == 2

    # Re-ingest same content: idempotent updates, no FTS duplication.
    stats2 = ingest(db, [corpus])
    assert stats2["inserted"] == 0 and stats2["updated"] == 2
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 2

    # Update one item's title: FTS must reflect it, not duplicate it.
    ROWS[0]["title"] = "Shoreland zoning order REVISED"
    corpus = _corpus_file(tmp_path, ROWS)
    ingest(db, [corpus])
    hits = con.execute(
        "SELECT i.title FROM items_fts f JOIN items i ON i.rowid_index=f.rowid "
        "WHERE items_fts MATCH 'shoreland'"
    ).fetchall()
    assert [h[0] for h in hits] == ["Shoreland zoning order REVISED"]
    con.close()


def test_ingest_metadata_columns_and_flags(tmp_path):
    db = tmp_path / "t.db"
    ingest(db, [_corpus_file(tmp_path, ROWS)])
    con = sqlite3.connect(db)
    docket, ld, eff = con.execute(
        "SELECT docket, ld, effective_date FROM items WHERE guid='https://x/1#a1'"
    ).fetchone()
    assert docket == "2:25-cv-00264"
    assert ld == 1234
    assert eff == "October 1, 2026"
    pdf, minor, roles = con.execute(
        "SELECT pdf_link, minor_change, roles FROM items WHERE guid='https://x/2#b2'"
    ).fetchone()
    assert (pdf, minor) == (1, 1)
    assert json.loads(roles) == []
    con.close()


def test_ingest_globs_multiple_files(tmp_path):
    db = tmp_path / "t.db"
    a = tmp_path / "c-a.jsonl"
    b = tmp_path / "c-b.jsonl"
    a.write_text(json.dumps(ROWS[0]), encoding="utf-8")
    b.write_text(json.dumps(dict(ROWS[1], id="https://x/3#c3")), encoding="utf-8")
    stats = ingest(db, [tmp_path / "c-*.jsonl"])
    assert stats["files"] == 2 and stats["total_items"] == 2


def test_ingest_skips_malformed_lines(tmp_path):
    db = tmp_path / "t.db"
    p = tmp_path / "bad.jsonl"
    p.write_text('{"id": "x#1", "title": "ok"}\nnot-json-at-all\n', encoding="utf-8")
    stats = ingest(db, [p])
    assert stats["rows_read"] == 1 and stats["total_items"] == 1
