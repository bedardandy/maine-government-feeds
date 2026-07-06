"""Regression tests for in-feed staleness marking (audit fix #1).

When a source has been failing to fetch (consecutive_failures > 0), the feed
is regenerated from last-good items. A subscriber only reads the feed itself,
never the separate status.html dashboard, so the staleness must be surfaced
IN the feed's description. No network is used — we drive the pure formatting
helpers with fabricated state dicts.
"""
from __future__ import annotations

import build_feeds


SOURCE = {
    "id": "example-source",
    "name": "Example Source",
    "category": "Test Category",
    "url": "https://example.com/page",
    "notes": "Base notes for this source.",
}


def test_healthy_source_has_no_staleness_suffix():
    state = {"consecutive_failures": 0, "last_success": "2026-07-06T00:00:00+00:00"}
    assert build_feeds.staleness_suffix(state) == ""
    # Full description is exactly the base notes, unchanged.
    assert build_feeds.feed_description(SOURCE, state) == "Base notes for this source."


def test_none_state_is_treated_as_healthy():
    assert build_feeds.staleness_suffix(None) == ""
    assert build_feeds.feed_description(SOURCE, None) == "Base notes for this source."


def test_failing_source_stamps_last_verified_date_in_description():
    state = {
        "consecutive_failures": 5,
        "last_success": "2026-06-01T12:00:00+00:00",
    }
    suffix = build_feeds.staleness_suffix(state)
    assert "STALE" in suffix
    assert "2026-06-01T12:00:00+00:00" in suffix
    assert "5 consecutive fetch failures" in suffix

    desc = build_feeds.feed_description(SOURCE, state)
    assert desc.startswith("Base notes for this source.")
    assert "2026-06-01T12:00:00+00:00" in desc


def test_single_failure_uses_singular_wording():
    state = {"consecutive_failures": 1, "last_success": "2026-06-30T00:00:00+00:00"}
    suffix = build_feeds.staleness_suffix(state)
    assert "1 consecutive fetch failure since" in suffix
    assert "failures since" not in suffix  # singular, not plural


def test_never_succeeded_source_is_marked_stale():
    state = {"consecutive_failures": 3, "last_success": None}
    suffix = build_feeds.staleness_suffix(state)
    assert "STALE" in suffix
    assert "never successfully fetched" in suffix


def test_json_feed_description_carries_staleness(tmp_path, monkeypatch):
    """The JSON feed a subscriber reads must carry the same marker."""
    feeds_dir = tmp_path / "feeds"
    monkeypatch.setattr(build_feeds, "FEEDS_DIR", feeds_dir)

    state = {"consecutive_failures": 4, "last_success": "2026-05-15T00:00:00+00:00"}
    build_feeds.write_json_feed(SOURCE, [], state)

    import json

    slug = build_feeds.safe_filename(SOURCE["id"])
    data = json.loads((feeds_dir / "json" / f"{slug}.json").read_text())
    assert "STALE" in data["description"]
    assert "2026-05-15T00:00:00+00:00" in data["description"]


def test_rss_feed_description_carries_staleness(tmp_path, monkeypatch):
    """The RSS/Atom channel description must carry the marker too."""
    feeds_dir = tmp_path / "feeds"
    monkeypatch.setattr(build_feeds, "FEEDS_DIR", feeds_dir)

    state = {"consecutive_failures": 2, "last_success": "2026-05-20T00:00:00+00:00"}
    build_feeds.write_rss_atom(SOURCE, [], state)

    slug = build_feeds.safe_filename(SOURCE["id"])
    rss_text = (feeds_dir / "rss" / f"{slug}.xml").read_text()
    assert "STALE" in rss_text
    assert "2026-05-20" in rss_text
