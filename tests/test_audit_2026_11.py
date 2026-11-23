"""Regression tests for the 2026-11 audit fixes. No network.

  * control characters in upstream text no longer break feed writing
  * a pending page change is confirmed even when the server answers 304
  * a blank page shell is a failed fetch, not a "page updated" item, and the
    junk pairs it left in state/archives are pruned
  * broken selectors are recorded (and cleared) instead of reading as healthy
  * validators are not kept for an unparseable feed
  * an item's date never moves forward after it was published
  * the hearings schedule is a snapshot ordered nearest-first
  * cross-source dedupe leaves same-source items alone
  * Atom/RSS timestamps are stable between builds
  * live feeds of retired sources are removed
  * the seen-map evicts oldest entries, the classifier keeps partial work,
    the corpus tools tolerate malformed rows, and the failure trackers work
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

import build_feeds
import classify_items
import common
import download_corpus
import health_watch
import ingest_corpus
import validate_feeds
import watcher_report
import write_corpus
from common import FetchResult, iso, now_utc

from test_data_quality_fixes import HTML_SOURCE, MONITOR_SOURCE, fake_fetch, html_list, page


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(common, "_health_store", None)
    docs = tmp_path / "docs"
    for module in (common, build_feeds, validate_feeds):
        monkeypatch.setattr(module, "DOCS_DIR", docs)
        monkeypatch.setattr(module, "FEEDS_DIR", docs / "feeds")
        monkeypatch.setattr(module, "OPML_DIR", docs / "opml")
    monkeypatch.setattr(build_feeds, "CALENDAR_DIR", docs / "calendar")
    return tmp_path


def long_page(marker: str) -> str:
    body = "".join(f"<p>Paragraph {i} {marker}</p>" for i in range(30))
    return f"<html><body><main>{body}</main></body></html>"


# ----------------------------------------------------------- control chars --
def test_control_characters_do_not_break_feed_writing(isolated, monkeypatch):
    html = html_list([("Order\x0bon Motion\x1f", "/a"), ("Clean title", "/b")])
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(html))
    monkeypatch.setattr(build_feeds, "enrich_new_items", lambda *a, **k: 0)
    state, items, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    assert all("\x0b" not in it["title"] and "\x1f" not in it["title"] for it in items)
    build_feeds.write_rss_atom(HTML_SOURCE, items, state)
    build_feeds.write_aggregate_feeds([(HTML_SOURCE, items)])
    assert "Order on Motion" in (isolated / "docs/feeds/rss/all.xml").read_text(encoding="utf-8")
    # Poisoned text already sitting in state is healed on the way out too.
    assert build_feeds.scrub_items([{"title": "a\x00b", "summary": "c\x0cd", "link": "x"}])[0] == {
        "title": "a b", "summary": "c d", "link": "x",
    }


# ------------------------------------------------- page monitor: 304 + pend --
def test_pending_page_change_is_confirmed_despite_conditional_get(isolated, monkeypatch):
    sent = []

    def server(body):
        def _fetch(url, client, etag=None, last_modified=None):
            sent.append(etag)
            if etag == "v-" + body:
                return FetchResult(ok=True, status_code=304, not_modified=True, final_url=url)
            return FetchResult(ok=True, status_code=200, text=page(body), final_url=url, etag="v-" + body)
        return _fetch

    monkeypatch.setattr(build_feeds, "fetch", server("original"))
    build_feeds.build_source(dict(MONITOR_SOURCE), client=None)           # baseline
    monkeypatch.setattr(build_feeds, "fetch", server("changed"))
    state, _, note = build_feeds.build_source(dict(MONITOR_SOURCE), client=None)   # first sighting
    assert state.get("pending_hash") and "awaiting confirmation" in note
    state, items, _ = build_feeds.build_source(dict(MONITOR_SOURCE), client=None)  # must NOT be a 304
    assert sent[-1] is None, "validators must be withheld while a change is pending"
    assert items[0]["title"].startswith("Page updated:") and "pending_hash" not in state
    # Once confirmed, conditional requests resume.
    state, _, note = build_feeds.build_source(dict(MONITOR_SOURCE), client=None)
    assert sent[-1] == "v-changed" and "304" in note


# ------------------------------------------------- page monitor: blank shell --
def test_blank_shell_is_a_failed_fetch_not_a_page_update(isolated, monkeypatch):
    blank = "<html><body><main></main></body></html>"
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(long_page("v1")))
    first, _, _ = build_feeds.build_source(dict(MONITOR_SOURCE), client=None)
    baseline_hash, baseline_success = first["content_hash"], first["last_success"]

    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(blank))
    for expected_failures in (1, 2, 3):
        state, items, note = build_feeds.build_source(dict(MONITOR_SOURCE), client=None)
        assert "nearly empty" in note and state["consecutive_failures"] == expected_failures
        assert state["content_hash"] == baseline_hash and state["last_success"] == baseline_success
        assert "pending_hash" not in state and "etag" not in state
        assert not any(it["title"].startswith("Page updated:") for it in items)

    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(long_page("v1")))
    state, items, _ = build_feeds.build_source(dict(MONITOR_SOURCE), client=None)
    assert state["consecutive_failures"] == 0
    assert [it["title"] for it in items] == ["Monitoring started: Test Monitor Source"]

    # A real edit is still reported (after its confirming second observation).
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(long_page("v2")))
    build_feeds.build_source(dict(MONITOR_SOURCE), client=None)
    _, items, _ = build_feeds.build_source(dict(MONITOR_SOURCE), client=None)
    assert items[0]["title"].startswith("Page updated:") and "Added:" in items[0]["summary"]


def _change(n, summary):
    return {"id": f"c{n}", "title": "Page updated: County Deeds", "link": "https://x", "summary": summary,
            "content_text": summary, "published": f"2026-10-{n:02d}T00:00:00+00:00"}


def test_prune_blank_flap_pairs():
    base = build_feeds._NO_EXCERPT_SUMMARY
    went_blank = base + "\n\nWhat changed (text excerpt):\nRemoved:\n  - Home\n  - Contact"
    real = base + "\n\nWhat changed (text excerpt):\nAdded:\n  + New fee schedule\nRemoved:\n  - Old fee schedule"
    removal_only = base + "\n\nWhat changed (text excerpt):\nRemoved:\n  - Holiday closure notice"
    items = [
        _change(9, base), _change(8, went_blank),        # flap pair -> dropped
        _change(7, real),                                 # real edit -> kept
        _change(6, base), _change(5, went_blank),        # flap pair -> dropped
        _change(4, removal_only),                         # genuine removal, not followed by a bare item -> kept
        _change(3, base),                                 # legacy item with no excerpt, nothing blank before it -> kept
        {"id": "x", "title": "A real item", "summary": "text"},
    ]
    kept = build_feeds.prune_blank_flap_items(items)
    assert [it["id"] for it in kept] == ["c7", "c4", "c3", "x"]
    assert build_feeds.prune_blank_flap_items(kept) == kept
    # Pairs that only become adjacent once an inner pair is gone are removed
    # in the same call, so one build leaves a stable result.
    nested = [_change(9, base), _change(8, base), _change(7, went_blank), _change(6, went_blank), _change(5, real)]
    assert [it["id"] for it in build_feeds.prune_blank_flap_items(nested)] == ["c5"]
    assert [e["id"] for e in build_feeds.prune_blank_flap_items(items, text_key="content_text")] == ["c7", "c4", "c3", "x"]


# ------------------------------------------------------ selectors + health --
def test_selector_breakage_is_recorded_surfaced_and_cleared(isolated, monkeypatch):
    monkeypatch.setattr(build_feeds, "enrich_new_items", lambda *a, **k: 0)
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(html_list([("Item A", "/a")])))
    state, _, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    assert not state.get("selector_fallback_since") and build_feeds._source_health(state) == "ok"

    monkeypatch.setattr(build_feeds, "fetch", fake_fetch("<html><body><div>redesigned</div></body></html>"))
    state, items, note = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    since = state["selector_fallback_since"]
    assert since and "selectors returned no items" in note and items[0]["title"] == "Item A"
    assert state["consecutive_failures"] == 0 and build_feeds._source_health(state) == "degraded"
    state, _, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    assert state["selector_fallback_since"] == since  # the first day it broke, not the latest run

    # ...in the health issue, the watcher's batch and the dashboard.
    monkeypatch.setattr(health_watch, "load_sources", lambda: [dict(HTML_SOURCE)])
    rows = health_watch.failing_sources(3)
    assert rows[0]["id"] == "test-html" and "selectors match nothing" in rows[0]["status"]
    monkeypatch.setattr(validate_feeds, "load_state", common.load_state)
    (isolated / "docs").mkdir(parents=True, exist_ok=True)
    validate_feeds.write_status_html([dict(HTML_SOURCE)])
    assert "DEGRADED (selectors match nothing since" in (isolated / "docs/status.html").read_text(encoding="utf-8")

    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(html_list([("Item A", "/a"), ("Item B", "/b")])))
    state, _, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    assert state["selector_fallback_since"] is None and build_feeds._source_health(state) == "ok"
    assert health_watch.failing_sources(3) == []


def test_watcher_report_prefers_fixable_sources_and_skips_host_blocks(isolated, monkeypatch):
    sources = [
        {"id": "blocked", "name": "Blocked", "url": "https://a.example/x", "type": "html", "enabled": True},
        {"id": "robots", "name": "Robots", "url": "https://b.example/x", "type": "html", "enabled": True},
        {"id": "selectors", "name": "Selectors", "url": "https://c.example/x", "type": "html", "enabled": True},
        {"id": "timeout", "name": "Timeout", "url": "https://d.example/x", "type": "native_rss",
         "rss_url": "https://d.example/{scotus_ot_term}/feed", "enabled": True},
        {"id": "healthy", "name": "Healthy", "url": "https://e.example/x", "type": "html", "enabled": True},
    ]
    monkeypatch.setattr(common, "load_sources", lambda: sources)
    common.save_state("blocked", {"consecutive_failures": 99, "last_status": 403, "last_error": "HTTP 403"})
    common.save_state("robots", {"consecutive_failures": 80, "last_error": "blocked by robots.txt"})
    common.save_state("selectors", {"consecutive_failures": 0, "selector_fallback_since": "2026-07-18T00:00:00+00:00"})
    common.save_state("timeout", {"consecutive_failures": 4, "last_error": "ReadTimeout"})
    common.save_state("healthy", {"consecutive_failures": 0})
    fetched = []
    monkeypatch.setattr(common, "fetch", lambda url, client: fetched.append(url) or FetchResult(ok=True, text="<p>hi</p>"))
    assert watcher_report.build(isolated / "w", client=object(), max_sources=5) == 2
    report = (isolated / "w/report.md").read_text(encoding="utf-8")
    assert report.index("`selectors`") < report.index("`timeout`")
    assert "`blocked`" not in report and "`robots`" not in report and "2 more are refused by the host" in report
    assert fetched == ["https://c.example/x", f"https://d.example/{common.scotus_ot_term()}/feed"]


def test_unparseable_feed_does_not_keep_validators(isolated, monkeypatch):
    source = {"id": "rss", "name": "RSS", "category": "T", "url": "https://x/", "rss_url": "https://x/rss", "type": "native_rss"}
    monkeypatch.setattr(build_feeds, "fetch", lambda *a, **k: FetchResult(
        ok=True, status_code=200, text="Service Unavailable", etag='"bad"', last_modified="yesterday"))
    state, _, note = build_feeds.build_source(source, client=None)
    assert "unparseable" in note and state["consecutive_failures"] == 1
    assert "etag" not in state and "last_modified" not in state
    # An HTML error page at the feed URL is not "an empty feed" either...
    assert build_feeds.parse_native_rss("<html><body>Down for maintenance</body></html>", "https://x") is None
    # ...but a well-formed feed that has no entries right now is.
    assert build_feeds.parse_native_rss('<rss version="2.0"><channel><title>t</title></channel></rss>', "https://x") == []
    assert build_feeds.parse_native_rss('<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title></feed>', "https://x") == []


# -------------------------------------------------------------------- dates --
def test_published_date_never_moves_forward(isolated, monkeypatch):
    monkeypatch.setattr(build_feeds, "enrich_new_items", lambda *a, **k: 0)
    source = dict(HTML_SOURCE, selectors={"item": "ul li", "title": "a", "link": "a", "date": "span"})
    future = (now_utc() + timedelta(days=30)).strftime("%B %d, %Y")

    def listing(date_text):
        return f'<html><body><ul><li><a href="/rule">Rule 101</a><span>{date_text}</span></li></ul></body></html>'

    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(listing(future)))
    _, items, _ = build_feeds.build_source(source, client=None)
    stamped = items[0]["published"]
    assert stamped == items[0]["first_seen"]  # the far-future date was rejected

    # Weeks later the same row's date is now "plausible" (here: today).
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(listing((now_utc() + timedelta(days=1)).strftime("%B %d, %Y"))))
    _, items, _ = build_feeds.build_source(source, client=None)
    assert items[0]["published"] == stamped

    # A date moving BACKWARD (the real, earlier publication date) is accepted.
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(listing("January 5, 2026")))
    _, items, _ = build_feeds.build_source(source, client=None)
    assert items[0]["published"].startswith("2026-01-05")


# ----------------------------------------------------------------- hearings --
def test_hearings_schedule_is_a_nearest_first_snapshot(isolated, monkeypatch):
    source = {"id": "hearings", "name": "Hearings", "category": "Leg", "url": "https://leg.example/", "type": "me_leg_hearings"}
    monkeypatch.setattr(build_feeds, "enrich_new_items", lambda *a, **k: 0)

    def hearing(n, when):
        return {"id": f"h{n}", "title": f"Public Hearing: LD {n}", "link": f"https://leg.example/{n}",
                "summary": "", "published": iso(when), "event_start": iso(when), "event_location": "Room 1"}

    now = now_utc()
    upcoming = [hearing(n, now + timedelta(days=n)) for n in range(1, 121)]
    past = [hearing(900 + n, now - timedelta(days=n)) for n in (1, 2)]
    monkeypatch.setattr(build_feeds, "fetch_me_leg_hearings", lambda s, c: (past + list(reversed(upcoming)), None))
    _, items, _ = build_feeds.build_source(source, client=None)
    assert [it["id"] for it in items] == [f"h{n}" for n in range(1, 51)]  # the next 50, soonest first

    # LD 1 is rescheduled: its old slot disappears instead of lingering.
    rescheduled = [hearing(1001, now + timedelta(days=3, hours=12))] + upcoming[1:10] + past
    monkeypatch.setattr(build_feeds, "fetch_me_leg_hearings", lambda s, c: (rescheduled, None))
    _, items, _ = build_feeds.build_source(source, client=None)
    ids = [it["id"] for it in items]
    assert "h1" not in ids and ids[:3] == ["h2", "h3", "h1001"] and ids[-2:] == ["h901", "h902"]
    assert next(it for it in items if it["id"] == "h2")["first_seen"] < next(it for it in items if it["id"] == "h1001")["first_seen"]


# ------------------------------------------------------------------- dedupe --
def _it(n, title, link="https://x/doc"):
    return {"id": f"{n:016x}", "title": title, "link": f"{link}{n}", "summary": "", "published": f"2026-10-{n:02d}T00:00:00+00:00"}


def test_dedupe_is_cross_source_only():
    court = {"id": "court", "name": "Court", "category": "Courts"}
    mirror = {"id": "mirror", "name": "Mirror", "category": "Courts"}
    bar = {"id": "bar", "name": "Bar", "category": "Regulation"}
    deeds = {"id": "deeds", "name": "Deeds", "category": "Counties"}
    pooled = build_feeds._aggregate_pool([
        (court, [_it(1, "Smith v. Jones Construction Co.")]),
        (mirror, [_it(2, "Smith v. Jones Construction Co.")]),                    # same opinion, other publisher
        (bar, [_it(3, "Board of Overseers v. Jane Q. Attorney"), _it(4, "Board of Overseers v. Jane Q. Attorney")]),
        (deeds, [_it(5, "Page updated: County Registry of Deeds"), _it(6, "Page updated: County Registry of Deeds")]),
    ])
    titles = sorted(p["title"] for p in pooled)
    assert len(pooled) == 5
    assert sum(t.startswith("Smith v. Jones") for t in titles) == 1
    assert sum(t.startswith("Board of Overseers") for t in titles) == 2
    assert sum(t.startswith("Page updated") for t in titles) == 2


# --------------------------------------------------------------- timestamps --
def test_feed_files_are_byte_stable_between_builds(isolated, monkeypatch):
    items = [dict(_it(n, f"Notice {n}"), first_seen=f"2026-10-{n:02d}T06:00:00+00:00") for n in (3, 2, 1)]
    state = {"last_success": iso(now_utc()), "consecutive_failures": 0}
    build_feeds.write_rss_atom(HTML_SOURCE, items, state)
    rss = (isolated / "docs/feeds/rss/test-html.xml").read_bytes()
    atom = (isolated / "docs/feeds/atom/test-html.xml").read_bytes()

    real_now = now_utc()
    monkeypatch.setattr(build_feeds, "now_utc", lambda: real_now + timedelta(hours=6))
    import feedgen.feed, feedgen.entry  # the libraries' own "now" must not leak in either
    build_feeds.write_rss_atom(HTML_SOURCE, items, state)
    assert (isolated / "docs/feeds/rss/test-html.xml").read_bytes() == rss
    assert (isolated / "docs/feeds/atom/test-html.xml").read_bytes() == atom
    text = atom.decode("utf-8")
    assert "<updated>2026-10-03T" in text and iso(real_now)[:13] not in text


# ------------------------------------------------------------ retired feeds --
def test_live_feeds_of_retired_sources_are_removed(isolated):
    docs = isolated / "docs"
    for sub, ext in (("feeds/rss", "xml"), ("feeds/atom", "xml"), ("feeds/json", "json"), ("feeds/archive", "json"), ("calendar", "ics")):
        (docs / sub).mkdir(parents=True, exist_ok=True)
        for name in ("live-source", "retired-source", "all", "role-tax"):
            (docs / sub / f"{name}.{ext}").write_text("x")
    for sid in ("live-source", "retired-source"):
        common.save_state(sid, {"items": []})
    removed = build_feeds.prune_retired_feeds({"live-source"})
    assert sorted(removed) == [
        "calendar/retired-source.ics", "feeds/atom/retired-source.xml",
        "feeds/json/retired-source.json", "feeds/rss/retired-source.xml",
    ]
    assert (docs / "feeds/archive/retired-source.json").exists()          # the record is kept
    assert (docs / "feeds/rss/live-source.xml").exists() and (docs / "feeds/rss/all.xml").exists()
    assert (docs / "feeds/json/role-tax.json").exists()
    assert build_feeds.prune_retired_feeds({"live-source"}) == []


# ---------------------------------------------------------------- seen map --
def test_seen_map_evicts_oldest_not_lowest_hash(isolated, monkeypatch):
    monkeypatch.setattr(build_feeds, "enrich_new_items", lambda *a, **k: 0)
    monkeypatch.setattr(build_feeds, "MAX_SEEN_ITEMS", 5)
    monkeypatch.setattr(build_feeds, "fetch", fake_fetch(html_list([("Current A", "/a"), ("Current B", "/b")])))
    state, _, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    for n in range(8):  # eight rows that rotated out long ago, oldest = old0
        state["seen"][f"old{n}"] = {"first_seen": f"2026-01-{n + 1:02d}T00:00:00+00:00", "published": None}
    common.save_state(HTML_SOURCE["id"], state)
    state, items, _ = build_feeds.build_source(dict(HTML_SOURCE), client=None)
    assert len(state["seen"]) == 5
    assert {it["id"] for it in items} <= set(state["seen"])
    assert sorted(k for k in state["seen"] if k.startswith("old")) == ["old5", "old6", "old7"]


# --------------------------------------------------------------- classifier --
ROLES = [{"id": "tax", "label": "Tax", "description": "tax"}]


def _openai_transport(replies):
    import httpx

    calls = iter(replies)

    def handler(request):
        reply = next(calls)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": "nope"})
        return httpx.Response(200, json=reply)

    return httpx.MockTransport(handler)


def _run_openai(monkeypatch, replies, n_items):
    import httpx

    transport = _openai_transport(replies)
    real_client = httpx.Client
    monkeypatch.setattr(classify_items.httpx, "Client", lambda **kw: real_client(transport=transport))
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(classify_items, "BATCH_SIZE", 2)
    return classify_items.classify_openai([{"title": f"t{i}"} for i in range(n_items)], ROLES)


def _ok(*pairs):
    body = {"classifications": [{"i": i, "roles": roles} for i, roles in pairs]}
    return {"choices": [{"message": {"content": json.dumps(body)}}]}


def test_openai_backend_keeps_earlier_batches_when_one_fails(monkeypatch):
    replies = [_ok((0, ["tax"]), (1, [])), {"choices": []}, _ok((4, ["tax", "bogus"]), (5, None))]
    assert _run_openai(monkeypatch, replies, 6) == [["tax"], [], None, None, ["tax"], []]


@pytest.mark.parametrize("bad", [
    {"choices": [{"message": {"content": None}}]},          # refusal: content is null
    {"choices": [{"message": {"content": "[1, 2]"}}]},      # valid JSON, wrong shape
    {"choices": [{"message": {"content": '{"classifications": [7, {"i": "0"}]}'}}]},
    {"nothing": True},
])
def test_openai_backend_never_raises_on_odd_responses(monkeypatch, bad):
    result = _run_openai(monkeypatch, [bad, _ok((2, ["tax"]))], 4)
    assert result == [None, None, ["tax"], None]


def test_openai_backend_stops_after_repeated_failures(monkeypatch):
    # Only two replies exist: a third request would raise StopIteration.
    assert _run_openai(monkeypatch, [401, 401], 10) is None


# ------------------------------------------------------------ corpus tools --
def test_corpus_tools_tolerate_malformed_rows(isolated, monkeypatch, tmp_path):
    monkeypatch.setattr(write_corpus, "load_sources", lambda: [{"id": "s1", "name": "S", "category": "C", "url": "https://x"}])
    common.save_state("s1", {"items": [
        {"title": "no link or id"},
        {"id": "abc", "link": "https://x/1", "title": "Good", "summary": "body", "first_seen": "2026-10-01T00:00:00+00:00"},
    ]})
    assert [r["title"] for r in write_corpus.build_corpus_rows()] == ["Good"]

    corpus = tmp_path / "corpus.jsonl"
    corpus.write_text('[]\n"text"\n{"id": "g1", "title": "Shoreland zoning", "url": "https://x/1"}\n', encoding="utf-8")
    db = tmp_path / "c.db"
    assert ingest_corpus.ingest(db, [corpus])["inserted"] == 1
    con = sqlite3.connect(db)
    # The query documented in the module docstring and the README must run.
    assert con.execute(
        "SELECT i.title, i.url FROM items_fts f JOIN items i ON i.rowid_index = f.rowid "
        "WHERE items_fts MATCH 'shoreland zoning' ORDER BY i.date_published DESC LIMIT 10"
    ).fetchall() == [("Shoreland zoning", "https://x/1")]


def test_download_corpus_finds_the_artifact_by_name(monkeypatch):
    import httpx

    requested = []

    def handler(request):
        requested.append(request.url.path + ("?" + request.url.query.decode() if request.url.query else ""))
        if request.url.path.endswith("/actions/artifacts"):
            return httpx.Response(200, json={"artifacts": [
                {"id": 1, "name": "corpus", "expired": True, "created_at": "2026-10-30", "workflow_run": {"id": 11, "head_branch": "main"}},
                {"id": 2, "name": "corpus", "expired": False, "created_at": "2026-10-28", "workflow_run": {"id": 22, "head_branch": "feature"}},
                {"id": 3, "name": "corpus", "expired": False, "created_at": "2026-10-27", "workflow_run": {"id": 33, "head_branch": "main"}},
                {"id": 4, "name": "corpus", "expired": False, "created_at": "2026-10-29", "workflow_run": {"id": 44, "head_branch": "main"}},
            ]})
        return httpx.Response(200, json={"artifacts": []})

    real_client = httpx.Client
    monkeypatch.setattr(download_corpus.httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(handler), base_url=kw["base_url"]))
    art, run_id = download_corpus.latest_artifact("o/r", "tok", None)
    assert (art["id"], run_id) == (4, 44)
    assert requested == ["/repos/o/r/actions/artifacts?name=corpus&per_page=30"]
    with pytest.raises(SystemExit, match="on run 99"):
        download_corpus.latest_artifact("o/r", "tok", 99)


# ----------------------------------------------------- build-failure issue --
class FakeGH:
    def __init__(self, issues=()):
        self.issues = list(issues)
        self.log = []

    def find_tracker(self, marker=health_watch.MARKER):
        return next((i for i in self.issues if marker in i["body"]), None)

    def create(self, title, body):
        self.issues.append({"number": 7, "body": body})
        self.log.append(("create", title))

    def comment(self, number, body):
        self.log.append(("comment", number))

    def close(self, number):
        self.issues = [i for i in self.issues if i["number"] != number]
        self.log.append(("close", number))


def test_build_failure_issue_lifecycle():
    source_issue = {"number": 3, "body": health_watch.MARKER + " sources"}
    gh = FakeGH([source_issue])
    assert "no build-failure issue" in health_watch.reconcile_build_issue(gh, "success", "u")
    assert "opened" in health_watch.reconcile_build_issue(gh, "failure", "https://run/1")
    assert "commented" in health_watch.reconcile_build_issue(gh, "failure", "https://run/2")
    assert "closed" in health_watch.reconcile_build_issue(gh, "success", "https://run/3")
    assert [e[0] for e in gh.log] == ["create", "comment", "comment", "close"]
    assert gh.issues == [source_issue]  # the source-health issue is a different tracker
