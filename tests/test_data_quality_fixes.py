"""Regression tests for the data-quality fixes.

Covers the bugs found in the 2026-08 feed audit:
  * rotating-window re-stamp bug (seen-map provenance memory)
  * page-monitor false positives (two-run debounce, challenge-page detection)
  * page-monitor artifacts lingering in item feeds after a source type change
  * scraped-date quality (future-date clamp, date-from-title parsing)
  * consumer-compatibility output (pubDate fallback, date-collision spreading)

No network is used — fetches are monkeypatched.
"""
from __future__ import annotations

import json

import pytest

import build_feeds
import common
from common import FetchResult, item_id


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path / "state")
    return tmp_path / "state"


def fake_fetch(text):
    return lambda url, client: FetchResult(ok=True, status_code=200, text=text, final_url=url)


def html_list(entries):
    lis = "".join(f'<li><a href="{href}">{title}</a></li>' for title, href in entries)
    return f"<html><body><ul>{lis}</ul></body></html>"


HTML_SOURCE = {
    "id": "test-html",
    "name": "Test HTML Source",
    "category": "Test",
    "url": "https://example.com/list",
    "type": "html",
    "selectors": {"item": "ul li", "title": "a", "link": "a"},
}

MONITOR_SOURCE = {
    "id": "test-monitor",
    "name": "Test Monitor Source",
    "category": "Test",
    "url": "https://example.com/page",
    "type": "page_monitor",
}


def page(text):
    return f"<html><body><main><p>{text}</p></main></body></html>"


# --------------------------------------------------------------------------- #
# Seen-map: evicted items keep their provenance stamps
# --------------------------------------------------------------------------- #
def test_evicted_item_keeps_original_stamps_via_seen_map(isolated_state, monkeypatch):
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(html_list([("Item A", "/a")])))
    state, items, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    original_published = items[0]["published"]
    original_first_seen = items[0]["first_seen"]
    assert state["seen"][items[0]["id"]]["published"] == original_published

    # Simulate eviction from the 50-item window: the item is gone from
    # state["items"] but remains in the seen map.
    state["items"] = []
    common.save_state(HTML_SOURCE["id"], state)

    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(html_list([("Item A", "/a")])))
    _, items2, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    assert items2[0]["published"] == original_published
    assert items2[0]["first_seen"] == original_first_seen


def test_seen_map_is_capped(isolated_state, monkeypatch):
    entries = [(f"Item {i}", f"/i{i}") for i in range(60)]
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(html_list(entries)))
    monkeypatch.setattr(build_feeds, "MAX_SEEN_ITEMS", 10)
    state, _, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    assert len(state["seen"]) <= 10


# --------------------------------------------------------------------------- #
# Page-monitor debounce
# --------------------------------------------------------------------------- #
def run_monitor(monkeypatch, text):
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(page(text)))
    return build_feeds.build_source(dict(MONITOR_SOURCE), client=None)


def test_stable_change_emits_after_two_runs(isolated_state, monkeypatch):
    run_monitor(monkeypatch, "version one")  # baseline
    _, items, note = run_monitor(monkeypatch, "version two")  # first sighting: held
    assert not any(it["title"].startswith("Page updated:") for it in items)
    assert "awaiting confirmation" in note
    _, items, _ = run_monitor(monkeypatch, "version two")  # confirmed: emitted
    assert any(it["title"].startswith("Page updated:") for it in items)


def test_flapping_page_never_emits(isolated_state, monkeypatch):
    run_monitor(monkeypatch, "variant A")  # baseline
    for text in ["variant B", "variant A", "variant B", "variant A"]:
        _, items, _ = run_monitor(monkeypatch, text)
        assert not any(it["title"].startswith("Page updated:") for it in items)


def test_diff_excerpt_present_on_confirmed_change(isolated_state, monkeypatch):
    run_monitor(monkeypatch, "old content here")
    run_monitor(monkeypatch, "new content here")
    _, items, _ = run_monitor(monkeypatch, "new content here")
    change = next(it for it in items if it["title"].startswith("Page updated:"))
    assert "What changed" in change["summary"]
    assert "new content here" in change["summary"]


# --------------------------------------------------------------------------- #
# Challenge-page detection
# --------------------------------------------------------------------------- #
def test_challenge_page_is_a_soft_failure(isolated_state, monkeypatch):
    run_monitor(monkeypatch, "real content")
    challenge = "<html><body>Please wait while your request is being verified...</body></html>"
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(challenge))
    state, _, note = build_feeds.build_source(dict(MONITOR_SOURCE), client=None)
    assert state["consecutive_failures"] == 1
    assert "challenge" in (note or "")
    # The stored fingerprint must be untouched so the real page's return
    # doesn't read as a change.
    _, items, _ = run_monitor(monkeypatch, "real content")
    assert not any(it["title"].startswith("Page updated:") for it in items)


def test_challenge_detection_matches_known_markers():
    assert build_feeds.looks_like_challenge_page("Please wait while your request is being verified...")
    assert build_feeds.looks_like_challenge_page("<title>Just a moment...</title>")
    assert not build_feeds.looks_like_challenge_page("<html><body>Court opinions list</body></html>")


# --------------------------------------------------------------------------- #
# Artifact purge when a source produces real items
# --------------------------------------------------------------------------- #
def test_page_monitor_artifacts_purged_on_item_parse(isolated_state, monkeypatch):
    state = common.load_state(HTML_SOURCE["id"])
    state["content_hash"] = "deadbeef"
    state["page_lines"] = ["old"]
    state["items"] = [
        {
            "id": item_id(HTML_SOURCE["url"], "baseline-monitoring"),
            "title": f"Monitoring started: {HTML_SOURCE['name']}",
            "link": HTML_SOURCE["url"],
            "summary": "",
            "published": "2026-07-01T00:00:00+00:00",
            "first_seen": "2026-07-01T00:00:00+00:00",
        },
        {
            "id": "aaaa000011112222",
            "title": f"Page updated: {HTML_SOURCE['name']}",
            "link": HTML_SOURCE["url"],
            "summary": "",
            "published": "2026-07-02T00:00:00+00:00",
            "first_seen": "2026-07-02T00:00:00+00:00",
        },
    ]
    common.save_state(HTML_SOURCE["id"], state)

    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(html_list([("Real Item", "/real")])))
    state, items, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    titles = [it["title"] for it in items]
    assert titles == ["Real Item"]
    assert "content_hash" not in state
    assert "page_lines" not in state


# --------------------------------------------------------------------------- #
# Date parsing
# --------------------------------------------------------------------------- #
def test_future_dates_are_rejected():
    assert build_feeds._parse_item_date("January 1, 2050") is None
    assert build_feeds._parse_item_date("July 1, 2026") is not None


def test_date_from_title_prefix():
    html = html_list([("7/24/2026: eFiling goes live in York County", "/news/1")])
    items = build_feeds.parse_html_selectors(
        html, "https://example.com", {"item": "ul li", "title": "a", "link": "a"},
        date_from_title=True,
    )
    assert items[0]["published"].startswith("2026-07-24")

    # Without the opt-in, no date is extracted from the title.
    items = build_feeds.parse_html_selectors(
        html, "https://example.com", {"item": "ul li", "title": "a", "link": "a"}
    )
    assert items[0]["published"] is None


def test_link_spaces_normalized():
    html = html_list([("Doc", "/files/June25Advisory Board.pdf")])
    items = build_feeds.parse_html_selectors(
        html, "https://example.com", {"item": "ul li", "title": "a", "link": "a"}
    )
    assert " " not in items[0]["link"]
    assert "%20" in items[0]["link"]


# --------------------------------------------------------------------------- #
# Consumer-facing output guarantees
# --------------------------------------------------------------------------- #
def test_entry_pubdate_falls_back_to_first_seen():
    it = {"id": "ab12", "published": None, "first_seen": "2026-07-01T10:00:00+00:00"}
    assert build_feeds._entry_pubdate(it) is not None


def test_entry_pubdate_spreads_midnight_collisions():
    a = {"id": "aaaa111122223333", "published": "2026-07-01T00:00:00+00:00"}
    b = {"id": "bbbb444455556666", "published": "2026-07-01T00:00:00+00:00"}
    pa, pb = build_feeds._entry_pubdate(a), build_feeds._entry_pubdate(b)
    assert pa != pb
    assert pa.date().isoformat() == "2026-07-01"
    assert pb.date().isoformat() == "2026-07-01"
    # Deterministic across builds.
    assert build_feeds._entry_pubdate(dict(a)) == pa


def test_item_tags_include_roles_and_category():
    source = {"category": "Maine Judicial Branch", "subcategory": "Opinions"}
    it = {"roles": ["civil-litigation-appellate"]}
    tags = build_feeds._item_tags(source, it)
    assert "Maine Judicial Branch" in tags
    assert "Opinions" in tags
    assert "role:civil-litigation-appellate" in tags


# --------------------------------------------------------------------------- #
# Aggregates
# --------------------------------------------------------------------------- #
def test_aggregate_pool_excludes_baselines_and_tags_origin():
    src = {"id": "s1", "name": "Source One", "category": "Cat", "subcategory": "Sub", "url": "https://x"}
    items = [
        {"id": "1", "title": "Monitoring started: Source One", "link": "https://x", "published": "2026-07-01T00:00:00+00:00"},
        {"id": "2", "title": "Real thing", "link": "https://x/2", "published": "2026-07-02T00:00:00+00:00"},
    ]
    pooled = build_feeds._aggregate_pool([(src, items)])
    assert len(pooled) == 1
    assert pooled[0]["_agg_tags"] == ["Cat", "Sub"]
    assert "Source One" in pooled[0]["title"]


def test_digest_only_covers_completed_days():
    from datetime import timedelta

    today = common.now_utc().date().isoformat()
    yesterday = (common.now_utc() - timedelta(days=1)).date().isoformat()
    src_tags = ["Cat"]
    pooled = [
        {"id": "1", "title": "Today item", "link": "https://x/1", "_agg_tags": src_tags,
         "first_seen": f"{today}T05:00:00+00:00", "published": f"{today}T05:00:00+00:00"},
        {"id": "2", "title": "Yesterday item", "link": "https://x/2", "_agg_tags": src_tags,
         "first_seen": f"{yesterday}T05:00:00+00:00", "published": f"{yesterday}T05:00:00+00:00"},
    ]
    digests = build_feeds._digest_items(pooled)
    assert all(today not in d["id"] for d in digests)
    assert any(d["id"] == f"digest-{yesterday}" for d in digests)
    day_digest = next(d for d in digests if d["id"] == f"digest-{yesterday}")
    assert "Yesterday item" in day_digest["summary"]
