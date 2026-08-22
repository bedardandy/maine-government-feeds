"""Tests for the fetch-layer hardening (2026-08 audit):

* HTTP conditional requests: validators persisted from responses and sent
  back as If-None-Match / If-Modified-Since; 304 handled as not_modified.
* Per-source state hygiene: volatile health fields live in the combined
  _health.json snapshot, per-source files stop churning when nothing
  durable changed, and internal files are excluded from state iteration.
* Retry-After: a server-advertised wait is honored (bounded).

No network is used — httpx.MockTransport serves everything.
"""
from __future__ import annotations

import json

import httpx
import pytest

import common
from common import FetchResult


@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(common, "HOST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(common, "_last_request_at", {})


def _client_with(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


ROBOTS_OK = "User-agent: *\nDisallow:\n"


def test_validators_captured_and_replayed(no_delay):
    seen_headers = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OK)
        seen_headers.append(
            {
                "if_none_match": request.headers.get("If-None-Match"),
                "if_modified_since": request.headers.get("If-Modified-Since"),
            }
        )
        if seen_headers[-1]["if_none_match"]:
            return httpx.Response(304)
        return httpx.Response(
            200,
            text="<html><body>content</body></html>",
            headers={"ETag": '"abc123"', "Last-Modified": "Tue, 21 Jul 2026 00:00:00 GMT"},
        )

    client = _client_with(handler)
    first = common.fetch("https://example.gov/page", client)
    assert first.ok and not first.not_modified
    assert first.etag == '"abc123"'
    assert first.last_modified == "Tue, 21 Jul 2026 00:00:00 GMT"

    second = common.fetch(
        "https://example.gov/page", client, etag=first.etag, last_modified=first.last_modified
    )
    assert second.ok and second.not_modified
    assert second.text is None
    assert seen_headers[1] == {
        "if_none_match": '"abc123"',
        "if_modified_since": "Tue, 21 Jul 2026 00:00:00 GMT",
    }


def test_retry_after_is_parsed_and_bounded(no_delay):
    waits = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_OK)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "99999"})
        return httpx.Response(200, text="ok")

    monkey_sleep = lambda s: waits.append(s)
    import build_feeds  # noqa: F401 — just ensure module import works alongside
    original_sleep = common.time.sleep
    common.time.sleep = monkey_sleep
    try:
        client = _client_with(handler)
        result = common.fetch("https://example.gov/page", client)
    finally:
        common.time.sleep = original_sleep
    assert result.ok
    assert calls["n"] == 2
    # The advertised wait was honored but capped at RETRY_AFTER_CAP_SECONDS.
    assert waits and waits[0] == common.RETRY_AFTER_CAP_SECONDS


def test_health_fields_split_out_of_per_source_state(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path / "state")
    state = common.load_state("s1")
    state["last_checked"] = "2026-08-22T00:00:00+00:00"
    state["last_success"] = "2026-08-22T00:00:00+00:00"
    state["consecutive_failures"] = 0
    state["items"] = [{"id": "a", "title": "T", "link": "https://x/a"}]
    common.save_state("s1", state)

    # Per-source file carries no volatile fields.
    with open(tmp_path / "state" / "s1.json", encoding="utf-8") as f:
        on_disk = json.load(f)
    for k in common.HEALTH_KEYS:
        assert k not in on_disk
    assert on_disk["items"][0]["id"] == "a"

    # Combined snapshot carries them.
    with open(tmp_path / "state" / "_health.json", encoding="utf-8") as f:
        snap = json.load(f)
    assert snap["s1"]["last_success"] == "2026-08-22T00:00:00+00:00"

    # Round-trip: load_state reassembles the full shape.
    merged = common.load_state("s1")
    assert merged["last_success"] == "2026-08-22T00:00:00+00:00"
    assert merged["items"][0]["id"] == "a"


def test_unchanged_state_does_not_rewrite_file(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path / "state")
    st = common.load_state("s1")
    st["items"] = [{"id": "a", "title": "T", "link": "u"}]
    st["etag"] = '"v1"'
    common.save_state("s1", st)
    path = tmp_path / "state" / "s1.json"
    first_mtime = path.stat().st_mtime_ns

    # Same content again (e.g. next run, nothing changed): no rewrite.
    st2 = common.load_state("s1")
    common.save_state("s1", st2)
    assert path.stat().st_mtime_ns == first_mtime

    # A durable change does rewrite.
    st2["etag"] = '"v2"'
    common.save_state("s1", st2)
    assert path.stat().st_mtime_ns != first_mtime


def test_iter_source_state_paths_skips_internal_files(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path / "state")
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "real.json").write_text("{}")
    (tmp_path / "state" / "_health.json").write_text("{}")
    names = [p.name for p in common.iter_source_state_paths()]
    assert names == ["real.json"]


def test_fetch_result_defaults():
    r = FetchResult(ok=True)
    assert r.not_modified is False
    assert r.etag is None
