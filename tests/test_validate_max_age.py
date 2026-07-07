"""Regression tests for the max-age staleness gate (audit fix #2).

Before this gate, a source with consecutive_failures=50 passed CI green as
long as its generated files were well-formed. check_staleness turns a source
whose last successful fetch is older than a (configurable) threshold into a
build-breaking error. Fabricated state files only; no network.
"""
from __future__ import annotations

import json
from datetime import timedelta

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


def test_fresh_source_passes(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    _write_state(tmp_path / "state", "fresh", iso(now_utc() - timedelta(days=1)))
    errors = validate_feeds.check_staleness([{"id": "fresh"}])
    assert errors == []


def test_stale_source_fails_with_clear_message(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    old = iso(now_utc() - timedelta(days=40))
    _write_state(tmp_path / "state", "stale", old, consecutive_failures=50)
    errors = validate_feeds.check_staleness([{"id": "stale"}])
    assert len(errors) == 1
    msg = errors[0]
    assert "stale" in msg
    assert "40" in msg  # ~40 days ago reported
    assert "50 consecutive" in msg


def test_never_succeeded_source_fails(tmp_path, monkeypatch):
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


def test_per_source_zero_disables_gate(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    old = iso(now_utc() - timedelta(days=500))
    _write_state(tmp_path / "state", "unbounded", old)
    assert validate_feeds.check_staleness([{"id": "unbounded", "max_age_days": 0}]) == []


def test_env_override_changes_default(tmp_path, monkeypatch):
    _patch_state_dir(monkeypatch, tmp_path / "state")
    old = iso(now_utc() - timedelta(days=20))
    _write_state(tmp_path / "state", "envtest", old)
    # 20 days > default 14 → stale; but a 30-day env default tolerates it.
    assert validate_feeds.check_staleness([{"id": "envtest"}]) != []
    monkeypatch.setenv("FEED_MAX_AGE_DAYS", "30")
    assert validate_feeds.check_staleness([{"id": "envtest"}]) == []
