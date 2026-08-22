"""Tests for the firm-pipeline features (2026-08 audit):

* structured metadata extraction (dockets, LD numbers, effective dates)
* cross-source canonical deduplication in aggregate feeds
* minor-change tagging of tiny page-monitor diffs
* corpus export for the knowledge pipeline
"""
from __future__ import annotations

import json

import pytest

import build_feeds
import common
from common import item_id


# --------------------------------------------------------------------------- #
# Metadata extraction
# --------------------------------------------------------------------------- #
def test_extract_federal_district_docket():
    it = {"title": "2:25-cv-00264 SHORTILL v. RELIANCE STANDARD LIFE", "summary": ""}
    meta = build_feeds.extract_item_meta(it)
    assert "2:25-cv-00264" in meta["docket"]


def test_extract_maine_trial_docket_and_ld():
    it = {
        "title": "LD 1234 — An Act Regarding Probate Notices",
        "summary": "Pursuant to ANDSC-RE-2026-00123 the effective date is October 1, 2026.",
    }
    meta = build_feeds.extract_item_meta(it)
    assert meta["ld"] == 1234
    assert "ANDSC-RE-2026-00123" in meta["docket"]
    assert meta["effective_date"] == "October 1, 2026"


def test_extract_no_meta_returns_empty():
    assert build_feeds.extract_item_meta({"title": "Agency newsletter", "summary": ""}) == {}


def test_bankruptcy_short_docket_requires_case_context():
    plain = {"title": "Fee schedule updated", "summary": "cost is 22-1234 dollars"}
    assert build_feeds.extract_item_meta(plain).get("docket") is None
    case = {"title": "In re Smith, Bankr. D. Me. appeal", "summary": "case 24-20118 closed"}
    assert "24-20118" in build_feeds.extract_item_meta(case)["docket"]


def test_json_feed_carries_meta_extension():
    src = {"id": "s", "name": "S", "category": "C", "url": "https://x"}
    it = {
        "id": "abc",
        "title": "Order entered 1:26-cv-00309-LEW",
        "link": "https://x/doc.pdf",
        "summary": "",
        "published": "2026-07-01T00:00:00+00:00",
        "first_seen": "2026-07-01T00:00:00+00:00",
    }
    entry = build_feeds._json_feed_item(it, src)
    assert "1:26-cv-00309" in json.dumps(entry["_meta"])
    assert entry["attachments"][0]["mime_type"] == "application/pdf"


def test_minor_change_flag_in_json_feed():
    src = {"id": "s", "name": "S", "category": "C", "url": "https://x"}
    it = {
        "id": "abc",
        "title": "Page updated: S",
        "link": "https://x",
        "summary": "tiny",
        "_minor_change": True,
        "published": "2026-07-01T00:00:00+00:00",
    }
    entry = build_feeds._json_feed_item(it, src)
    assert entry["_minor_change"] is True


# --------------------------------------------------------------------------- #
# Canonical dedupe in aggregates
# --------------------------------------------------------------------------- #
SRC_A = {"id": "a", "name": "Source A", "category": "Cat", "subcategory": "Sub", "url": "https://a"}
SRC_B = {"id": "b", "name": "Source B", "category": "Cat", "subcategory": "Sub", "url": "https://b"}


def _mk(src, n, title):
    return {
        "id": f"{src['id']}{n}",
        "title": title,
        "link": f"https://{src['id']}.example/{n}",
        "summary": "",
        "published": "2026-07-01T00:00:00+00:00",
        "first_seen": "2026-07-01T00:00:00+00:00",
    }


def test_aggregate_dedupes_redundant_opinion_listings():
    a_items = [_mk(SRC_A, 1, "Smith v. Jones — order granting motion to dismiss")]
    b_items = [
        _mk(SRC_B, 1, "smith v. jones   order granting motion to dismiss!"),
        _mk(SRC_B, 2, "Different matter entirely"),
    ]
    pooled = build_feeds._aggregate_pool([(SRC_A, a_items), (SRC_B, b_items)])
    titles = [p["title"] for p in pooled]
    assert len(pooled) == 2
    # The surviving duplicate copy is Source A's (first seen; equal timestamps).
    assert any("Source A" in t and "Smith v. Jones" in t for t in titles)


def test_aggregate_keeps_distinct_same_source_items():
    items = [
        _mk(SRC_A, 1, "Opinion one"),
        _mk(SRC_A, 2, "Opinion two"),
    ]
    pooled = build_feeds._aggregate_pool([(SRC_A, items)])
    assert len(pooled) == 2


def test_bare_titles_never_collide():
    a_items = [_mk(SRC_A, 1, "Updated")]
    b_items = [_mk(SRC_B, 1, "Updated")]
    pooled = build_feeds._aggregate_pool([(SRC_A, a_items), (SRC_B, b_items)])
    assert len(pooled) == 2  # too short/bare to dedupe safely


def test_newer_duplicate_copy_wins():
    older = _mk(SRC_A, 1, "Smith v. Jones opinion")
    newer = _mk(SRC_B, 1, "Smith v. Jones opinion")
    newer["published"] = "2026-07-02T00:00:00+00:00"
    pooled = build_feeds._aggregate_pool([(SRC_A, [older]), (SRC_B, [newer])])
    assert len(pooled) == 1
    assert pooled[0]["published"].startswith("2026-07-02")
    assert "Source B" in pooled[0]["title"]


# --------------------------------------------------------------------------- #
# Minor-change tagging (page monitors)
# --------------------------------------------------------------------------- #
MONITOR = {
    "id": "m1",
    "name": "Monitor One",
    "category": "Test",
    "url": "https://example.gov/page",
    "type": "page_monitor",
}


def _page(text):
    return f"<html><body><main><p>{text}</p></main></body></html>"


def test_minor_change_tagging(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path / "state")

    def fetch(text):
        return lambda url, client, **kw: common.FetchResult(
            ok=True, status_code=200, text=text, final_url=url
        )

    monkeypatch.setattr(build_feeds, "fetch", fetch(_page("original long-ish content here")))
    build_feeds.build_source(dict(MONITOR), client=None)  # baseline
    monkeypatch.setattr(build_feeds, "fetch", fetch(_page("original long-ish content herX")))
    build_feeds.build_source(dict(MONITOR), client=None)  # first sighting: held
    _, items, _ = build_feeds.build_source(dict(MONITOR), client=None)  # confirmed
    change = next(it for it in items if it["title"].startswith("Page updated:"))
    assert change.get("_minor_change") is True

    # A substantive diff is not tagged minor.
    new_text = (
        "The Board has adopted amendments to Chapter 5 concerning filing procedures "
        "and these changes take effect on January 1, 2027, with comment deadlines posted."
    )
    monkeypatch.setattr(build_feeds, "fetch", fetch(_page(new_text)))
    build_feeds.build_source(dict(MONITOR), client=None)  # hold run
    _, items, _ = build_feeds.build_source(dict(MONITOR), client=None)  # confirm run
    change = next(it for it in items if it["title"].startswith("Page updated:"))
    assert not change.get("_minor_change")


# --------------------------------------------------------------------------- #
# Corpus export
# --------------------------------------------------------------------------- #
def test_corpus_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(common, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    state = {
        "id": "src1",
        "items": [
            {
                "id": item_id("https://x/1", "Order 2:26-cv-00001"),
                "title": "Order 2:26-cv-00001 — Smith v. Jones",
                "link": "https://x/1",
                "summary": "Full opinion text.",
                "published": "2026-07-01T00:00:00+00:00",
                "first_seen": "2026-07-01T00:00:00+00:00",
                "roles": ["civil-litigation-appellate"],
            },
            {
                "id": "base",
                "title": "Monitoring started: Src",
                "link": "https://x",
                "summary": "",
                "published": "2026-06-30T00:00:00+00:00",
            },
        ],
    }
    (tmp_path / "state" / "src1.json").write_text(json.dumps(state))
    import write_corpus

    monkeypatch.setattr(
        write_corpus,
        "load_sources",
        lambda: [
            {"id": "src1", "name": "Src One", "category": "Cat", "subcategory": "Sub", "url": "https://x"}
        ],
    )
    rows = write_corpus.build_corpus_rows()
    assert len(rows) == 1  # baseline excluded
    row = rows[0]
    assert row["source_id"] == "src1"
    assert row["roles"] == ["civil-litigation-appellate"]
    assert "2:26-cv-00001" in row["_meta"]["docket"]
