"""The health watcher must consume the same merged state as the build."""
import json

import common
import health_watch


def test_snapshot_failures_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(common, "_health_store", None)
    sources = [{"id": "blocked", "name": "Blocked", "url": "https://example.gov"},
               {"id": "ignored", "health_ignore": True},
               {"id": "disabled", "enabled": False}]
    monkeypatch.setattr(health_watch, "load_sources", lambda: sources)
    failed = {
        "consecutive_failures": 52, "last_status": 403, "last_error": "HTTP 403",
        "last_success": "2026-10-15T21:19:39+00:00",
    }
    # Legacy inline health must lose to the current snapshot. Ignored and
    # disabled sources, and the internal _health record, must not be alerted.
    (tmp_path / "blocked.json").write_text(json.dumps({"consecutive_failures": 0}))
    (tmp_path / "_health.json").write_text(json.dumps({s["id"]: failed for s in sources}))
    rows = health_watch.failing_sources(3)
    assert len(rows) == 1
    assert rows[0]["id"] == "blocked"
    assert rows[0]["fails"] == 52
    assert rows[0]["status"] == 403
    assert failed["last_success"] in health_watch.issue_body(rows, 3)
    assert health_watch.failing_sources(53) == []
    common.save_state("blocked", {**failed, "consecutive_failures": 0, "last_status": 304})
    assert health_watch.failing_sources(3) == []


def test_legacy_inline_state_is_still_supported(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path)
    monkeypatch.setattr(common, "_health_store", None)
    monkeypatch.setattr(health_watch, "load_sources", lambda: [{"id": "legacy"}])
    (tmp_path / "legacy.json").write_text(json.dumps({"consecutive_failures": 4}))
    assert health_watch.failing_sources(3)[0]["fails"] == 4
