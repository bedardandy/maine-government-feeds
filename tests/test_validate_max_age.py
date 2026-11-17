"""Verification-age warnings and optional strict audits; no network."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

import build_feeds
import common
import validate_feeds
from common import now_utc, iso


def _write_state(state_dir, sid, last_success, consecutive_failures=0):
    state = {
        "id": sid,
        "last_success": last_success,
        "consecutive_failures": consecutive_failures,
        "items": [],
    }
    (state_dir / f"{sid}.json").write_text(json.dumps(state), encoding="utf-8")


def _patch_state_dir(monkeypatch, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    # load_state resolves STATE_DIR lazily via common.state_path, so patching
    # the module attribute is enough.
    monkeypatch.setattr(common, "STATE_DIR", state_dir)
    monkeypatch.setattr(common, "_health_store", None)
    monkeypatch.delenv("FEED_MAX_AGE_DAYS", raising=False)


def test_fresh_source_passes(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    _write_state(tmp_path / "state", "fresh", iso(now_utc() - timedelta(days=1)))
    errors = validate_feeds.check_staleness([{"id": "fresh"}])
    assert errors == []


def test_stale_source_reports_clear_message(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    old = iso(now_utc() - timedelta(days=40))
    _write_state(tmp_path / "state", "stale", old, consecutive_failures=50)
    errors = validate_feeds.check_staleness([{"id": "stale"}])
    assert len(errors) == 1
    msg = errors[0]
    assert "stale" in msg
    assert "40" in msg  # ~40 days ago reported
    assert "50 consecutive" in msg


def test_never_succeeded_source_warns(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    _write_state(tmp_path / "state", "neversucc", None, consecutive_failures=5)
    errors = validate_feeds.check_staleness([{"id": "neversucc"}])
    assert len(errors) == 1
    assert "never successfully fetched" in errors[0]


def test_health_ignore_exempts_source(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    old = iso(now_utc() - timedelta(days=99))
    _write_state(tmp_path / "state", "blocked", old, consecutive_failures=99)
    errors = validate_feeds.check_staleness([{"id": "blocked", "health_ignore": True}])
    assert errors == []


def test_per_source_override_tolerates_longer_staleness(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    old = iso(now_utc() - timedelta(days=30))
    _write_state(tmp_path / "state", "annual", old)
    # Default (14d) would flag it; a per-source 60d override tolerates it.
    assert validate_feeds.check_staleness([{"id": "annual"}]) != []
    assert validate_feeds.check_staleness([{"id": "annual", "max_age_days": 60}]) == []


@pytest.mark.parametrize("limit", [0, None])
def test_per_source_disables_age_warning(tmp_path, monkeypatch, limit):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    old = iso(now_utc() - timedelta(days=500))
    _write_state(tmp_path / "state", "unbounded", old)
    assert validate_feeds.check_staleness([{"id": "unbounded", "max_age_days": limit}]) == []


def test_env_override_changes_default(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    old = iso(now_utc() - timedelta(days=20))
    _write_state(tmp_path / "state", "envtest", old)
    # 20 days > default 14 → stale; but a 30-day env default tolerates it.
    assert validate_feeds.check_staleness([{"id": "envtest"}]) != []
    monkeypatch.setenv("FEED_MAX_AGE_DAYS", "30")
    assert validate_feeds.check_staleness([{"id": "envtest"}]) == []


@pytest.mark.parametrize("timestamp", ["not-a-date", "2026-10-15T21:19:39"])
def test_invalid_timestamp_warns_without_crashing(tmp_path, monkeypatch, timestamp):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    _write_state(tmp_path / "state", "invalid", timestamp)
    warnings = validate_feeds.check_staleness([{"id": "invalid"}])
    assert len(warnings) == 1
    assert "invalid last_success" in warnings[0]


@pytest.fixture
def generated_source(tmp_path, monkeypatch):
    """Generate real files once, then exercise the validator entry point."""
    _patch_state_dir(monkeypatch, tmp_path / "state")
    docs = tmp_path / "docs"
    for module in (common, build_feeds, validate_feeds):
        monkeypatch.setattr(module, "DOCS_DIR", docs)
        monkeypatch.setattr(module, "FEEDS_DIR", docs / "feeds")
        monkeypatch.setattr(module, "OPML_DIR", docs / "opml")
    monkeypatch.setattr(build_feeds, "CALENDAR_DIR", docs / "calendar")
    monkeypatch.setattr(build_feeds, "_roles_for_index", lambda: [])
    source = {
        "id": "blocked", "name": "Blocked <source>", "category": "Test",
        "url": "https://example.gov/notices", "type": "html",
    }
    monkeypatch.setattr(validate_feeds, "load_sources", lambda: [source])
    old = iso(now_utc() - timedelta(days=32))
    state = {
        "id": source["id"], "last_success": old, "consecutive_failures": 51,
        "items": [{"id": "retained", "title": "Last-good notice",
                   "link": "https://example.gov/notice/1", "published": old,
                   "first_seen": old, "summary": "Retained content"}],
    }
    common.save_state(source["id"], state)
    # Reproduce an upstream 403: retain last-good items and verification time.
    monkeypatch.setattr(build_feeds, "fetch", lambda *a, **kw: common.FetchResult(
        ok=False, status_code=403, error="HTTP 403 <blocked>"
    ))
    state, items, _ = build_feeds.build_source(source, client=None)
    assert state["last_success"] == old
    assert state["consecutive_failures"] == 52
    assert items[0]["title"] == "Last-good notice"
    build_feeds.write_rss_atom(source, items, state)
    build_feeds.write_json_feed(source, items, state)
    events = build_feeds.write_ics(source, items)
    build_feeds.write_combined_ics(events)
    build_feeds.write_opml([source])
    build_feeds.write_aggregate_feeds([(source, items)])
    build_feeds.write_catalog([source], {source["id"]: state})
    return source, docs


def test_403_does_not_block_validation_and_health_stays_visible(generated_source, capsys):
    source, docs = generated_source
    assert validate_feeds.main([]) == 0
    output = capsys.readouterr()
    assert "Validation OK: 1 sources" in output.out
    assert "SOURCE HEALTH WARNING: 1" in output.err
    assert "32.0 days ago > max 14d; 52 consecutive" in output.err
    dashboard = (docs / "status.html").read_text(encoding="utf-8")
    assert "STALE / UNVERIFIED (52x failing)" in dashboard
    assert "HTTP 403 &lt;blocked&gt;" in dashboard
    assert "32.0 days ago &gt; max 14d" in dashboard
    assert "Blocked &lt;source&gt;" in dashboard
    assert "1 source(s) with stale or unverified records" in dashboard
    assert "STALE" in (docs / "feeds/rss/blocked.xml").read_text(encoding="utf-8")
    assert "STALE" in (docs / "feeds/atom/blocked.xml").read_text(encoding="utf-8")
    assert "STALE" in json.loads((docs / "feeds/json/blocked.json").read_text())["description"]
    assert (docs / "feeds/json/manifest.json").exists()


def test_successful_304_clears_warning(generated_source, monkeypatch, capsys):
    source, docs = generated_source
    monkeypatch.setattr(build_feeds, "fetch", lambda *a, **kw: common.FetchResult(
        ok=True, status_code=304, not_modified=True
    ))
    state, items, _ = build_feeds.build_source(source, client=None)
    assert state["consecutive_failures"] == 0
    assert items[0]["title"] == "Last-good notice"
    assert validate_feeds.main([]) == 0
    assert "SOURCE HEALTH WARNING" not in capsys.readouterr().err
    assert "STALE / UNVERIFIED" not in (docs / "status.html").read_text(encoding="utf-8")


def test_strict_audit_fails_on_staleness(generated_source, capsys):
    assert validate_feeds.main(["--strict-staleness"]) == 1
    assert "VALIDATION FAILED with 1 build-breaking error(s)" in capsys.readouterr().err


@pytest.mark.parametrize("path, contents", [
    ("feeds/rss/blocked.xml", "<broken"),
    ("feeds/json/blocked.json", "{broken"),
    ("opml/maine-government-feeds.opml", None),
])
def test_structural_errors_remain_fatal(generated_source, capsys, path, contents):
    _, docs = generated_source
    target = docs / path
    if contents is None:
        target.unlink()
    else:
        target.write_text(contents, encoding="utf-8")
    assert validate_feeds.main([]) == 1
    assert "VALIDATION FAILED" in capsys.readouterr().err


@pytest.mark.parametrize("last_success", [None, "invalid", "2026-10-15T21:19:39"])
def test_unverified_records_do_not_block_default_build(generated_source, capsys, last_success):
    source, docs = generated_source
    state = common.load_state(source["id"])
    state["last_success"] = last_success
    common.save_state(source["id"], state)
    assert validate_feeds.main([]) == 0
    assert "SOURCE HEALTH WARNING: 1" in capsys.readouterr().err
    assert "STALE / UNVERIFIED" in (docs / "status.html").read_text(encoding="utf-8")
