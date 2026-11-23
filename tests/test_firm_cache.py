"""Tests for firm/firm_cache.py, the local firm cache (sync, search, checks,
watchlist, web API). No external network: published files come from a temp
directory or a loopback HTTP server, and embeddings come from the built-in
hashing backend or a small stand-in "semantic" embedder."""
from __future__ import annotations

import functools
import hashlib
import http.server
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "firm"))

import firm_cache as fc  # noqa: E402


class ConceptEmbedder:
    """Stand-in for a semantic model: texts that mention words from the same
    concept group land on the same axis, whatever the exact wording."""

    identity = "fake:concepts"
    groups = (
        ("landlord", "lessor", "tenant", "eviction", "lease"),
        ("shoreland", "zoning", "setback", "wetland"),
        ("bankruptcy", "debtor", "creditor", "chapter"),
    )

    def embed(self, texts, is_query=False):
        out = np.zeros((len(texts), len(self.groups) + 1), dtype=np.float32)
        for row, text in enumerate(texts):
            lowered = text.lower()
            for axis, words in enumerate(self.groups):
                out[row, axis] = sum(lowered.count(w) for w in words)
            if not out[row].any():
                out[row, -1] = 1.0
        return fc._normalise(out)


def _entry(n, title, body, **extra):
    base = {
        "id": f"https://example.gov/doc/{n}#{n:016x}",
        "url": f"https://example.gov/doc/{n}",
        "title": title,
        "content_text": body,
        "date_published": f"2026-10-{n:02d}T00:00:00+00:00",
        "_first_seen": f"2026-10-{n:02d}T06:00:00+00:00",
        "tags": ["Courts", "Opinions"],
    }
    base.update(extra)
    return base


def publish(docs: Path, feeds: dict[str, list[dict]], generated_at=None, health=None, tamper=None) -> None:
    """Write a miniature published site: catalog, archive feeds, manifest."""
    (docs / "feeds" / "json").mkdir(parents=True, exist_ok=True)
    (docs / "feeds" / "archive").mkdir(parents=True, exist_ok=True)
    health = health or {}
    catalog = {
        "_generated_at": generated_at or fc.iso(fc.utcnow()),
        "_build_commit": "abc123",
        "items": [
            {
                "id": sid, "title": f"Source {sid}", "url": f"https://example.gov/{sid}",
                "_category": "Courts", "_subcategory": "Opinions", "_source_type": "html",
                "_archive_url": f"https://example.invalid/feeds/archive/{sid}.json",
                "_health": health.get(sid, "ok"), "_last_success": "2026-10-15T00:00:00+00:00",
                "_health_ignore": False, "_item_count": len(items),
            }
            for sid, items in feeds.items()
        ] + [{"id": "all", "title": "Everything", "_source_type": "aggregate"}],
    }
    (docs / "feeds" / "json" / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    files = []
    for sid, items in feeds.items():
        rel = f"feeds/archive/{sid}.json"
        data = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": items}).encode("utf-8")
        (docs / rel).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        files.append({"path": rel, "sha256": "0" * 64 if tamper == sid else digest, "bytes": len(data)})
    (docs / "feeds" / "json" / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")


ITEMS = [
    _entry(1, "State v. Doe", "Docket: Som-25-305. The lessor sought eviction of the tenant.",
           tags=["Courts", "Opinions", "role:criminal-defense"], _meta={"docket": ["Som-25-305"]}),
    _entry(2, "Shoreland zoning amendments adopted", "New setback rules near the wetland take effect.",
           tags=["Agencies", "DEP", "role:environmental-land-use"], _meta={"ld": 1234, "effective_date": "2026-11-01"}),
    _entry(3, "In re Acme Corp.", "Chapter 11 debtor and creditor committee dispute.",
           _meta={"docket": ["2:25-cv-00264"]}, attachments=[{"url": "x.pdf", "mime_type": "application/pdf"}]),
    _entry(4, "Page updated: Fee schedule", "This page's content changed.", _minor_change=True),
]


@pytest.fixture
def site(tmp_path):
    docs = tmp_path / "docs"
    publish(docs, {"courts": ITEMS[:1] + ITEMS[2:], "dep": ITEMS[1:2]})
    return docs


@pytest.fixture
def cache(tmp_path, site):
    fc._matrix_cache.clear()
    con = fc.connect(tmp_path / "cache.db")
    fc.sync(con, fc.DirOrigin(site), log=lambda *_: None)
    yield con
    con.close()


# ------------------------------------------------------------------ sync --- #
def test_sync_ingests_and_normalises(cache):
    rows = {r["title"]: r for r in cache.execute("SELECT * FROM items")}
    assert len(rows) == 4
    doe = rows["State v. Doe"]
    assert doe["source_id"] == "courts" and doe["dockets"] == "Som-25-305"
    assert json.loads(doe["roles"]) == ["criminal-defense"]
    zoning = rows["Shoreland zoning amendments adopted"]
    assert zoning["ld"] == 1234 and zoning["effective_date"] == "2026-11-01"
    assert rows["In re Acme Corp."]["pdf_link"] == 1
    assert rows["Page updated: Fee schedule"]["kind"] == "page-change"
    assert rows["Page updated: Fee schedule"]["minor_change"] == 1
    # The aggregate catalog entry (no archive feed) is not a source.
    assert {r[0] for r in cache.execute("SELECT source_id FROM sources")} == {"courts", "dep"}


def test_resync_is_incremental_and_cache_only_grows(cache, site):
    again = fc.sync(cache, fc.DirOrigin(site), log=lambda *_: None)
    assert again["files_fetched"] == 0 and again["files_unchanged"] == 2 and again["items_new"] == 0

    # Upstream rotates Doe out and publishes one new item.
    publish(site, {"courts": [ITEMS[2], _entry(9, "New opinion", "Fresh text.")], "dep": ITEMS[1:2]})
    third = fc.sync(cache, fc.DirOrigin(site), log=lambda *_: None)
    assert third["files_fetched"] == 1 and third["items_new"] == 1
    titles = {r[0] for r in cache.execute("SELECT title FROM items")}
    assert "State v. Doe" in titles and "New opinion" in titles


def test_text_change_rebuilds_passages_but_bodyless_reobservation_does_not(cache, site):
    fc.embed_pending(cache, fc.HashingEmbedder())
    doe_id = cache.execute("SELECT id FROM items WHERE title = 'State v. Doe'").fetchone()[0]
    assert cache.execute("SELECT count(*) FROM vectors v JOIN chunks c ON c.id = v.chunk_id WHERE c.item_id = ?", (doe_id,)).fetchone()[0] == 1

    bodyless = dict(ITEMS[0], content_text="State v. Doe")
    publish(site, {"courts": [bodyless], "dep": ITEMS[1:2]})
    fc.sync(cache, fc.DirOrigin(site), log=lambda *_: None)
    assert "lessor" in cache.execute("SELECT content_text FROM items WHERE id = ?", (doe_id,)).fetchone()[0]
    assert cache.execute("SELECT count(*) FROM chunks WHERE item_id = ?", (doe_id,)).fetchone()[0] == 1

    revised = dict(ITEMS[0], content_text="Corrected opinion text about a landlord.")
    publish(site, {"courts": [revised], "dep": ITEMS[1:2]})
    stats = fc.sync(cache, fc.DirOrigin(site), log=lambda *_: None)
    assert stats["items_updated"] == 1
    assert cache.execute("SELECT count(*) FROM chunks WHERE item_id = ?", (doe_id,)).fetchone()[0] == 0
    assert fc.embed_pending(cache, fc.HashingEmbedder()) == 1
    assert [r["title"] for r in fc.search(cache, None, "corrected landlord", "keyword")] == ["State v. Doe"]
    assert fc.search(cache, None, "lessor", "keyword") == []  # old text left the keyword index


def test_same_document_from_two_sources_is_one_item(tmp_path):
    docs = tmp_path / "docs"
    publish(docs, {"a-first": ITEMS[:1], "b-second": ITEMS[:1]})
    con = fc.connect(tmp_path / "c.db")
    stats = fc.sync(con, fc.DirOrigin(docs), log=lambda *_: None)
    assert stats["items_new"] == 1 and stats["items_updated"] == 0
    assert con.execute("SELECT source_id FROM items").fetchall()[0][0] == "a-first"


def test_file_not_matching_manifest_is_rejected(tmp_path):
    docs = tmp_path / "docs"
    publish(docs, {"courts": ITEMS[:1], "dep": ITEMS[1:2]}, tamper="courts")
    con = fc.connect(tmp_path / "c.db")
    stats = fc.sync(con, fc.DirOrigin(docs), log=lambda *_: None)
    assert stats["items_new"] == 1
    assert [e for e in stats["errors"] if e.startswith("integrity: feeds/archive/courts.json")]
    assert con.execute("SELECT count(*) FROM items WHERE source_id = 'courts'").fetchone()[0] == 0
    checks = {c["name"]: c for c in fc.run_checks(con)}
    assert checks["last sync errors"]["level"] == fc.CRIT


def test_import_corpus_jsonl(tmp_path):
    con = fc.connect(tmp_path / "c.db")
    row = {
        "id": "https://x/1#a", "url": "https://x/1", "title": "Corpus row", "content_text": "body",
        "source_id": "s1", "source_name": "S One", "category": "Cat", "roles": ["tax"],
        "_meta": {"docket": ["1:26-cv-00001"]}, "pdf_link": True,
    }
    path = tmp_path / "corpus.jsonl"
    path.write_text(json.dumps(row) + "\nnot json\n\n", encoding="utf-8")
    assert fc.import_corpus(con, path) == {"items_new": 1, "items_updated": 0}
    got = con.execute("SELECT * FROM items").fetchone()
    assert (got["source_id"], got["dockets"], got["pdf_link"], json.loads(got["roles"])) == ("s1", "1:26-cv-00001", 1, ["tax"])


# ---------------------------------------------------------------- search --- #
def test_chunk_text_overlaps_and_carries_title():
    body = " ".join(f"word{i}" for i in range(900))
    chunks = fc.chunk_text("A Title", body)
    assert len(chunks) > 3 and all(c.startswith("A Title\n") for c in chunks)
    assert all(len(c) < fc.CHUNK_CHARS + 200 for c in chunks)
    first_tail = chunks[0].split()[-1]
    assert first_tail in chunks[1].split()  # overlap
    assert "word899" in chunks[-1]
    assert fc.chunk_text("Only title", "") == ["Only title"]
    assert fc.chunk_text("Same", "Same") == ["Same"]
    assert len(fc.chunk_text("T", "x " * 200_000)) <= fc.MAX_CHUNKS_PER_ITEM


def test_keyword_vector_and_hybrid_search(cache):
    embedder = ConceptEmbedder()
    fc.embed_pending(cache, embedder)

    assert fc.search(cache, embedder, "Som-25-305", "keyword")[0]["title"] == "State v. Doe"
    # "landlord" appears nowhere in the corpus; only the vector leg can find it.
    assert fc.search(cache, embedder, "landlord", "keyword") == []
    by_meaning = fc.search(cache, embedder, "landlord", "vector", limit=1)[0]
    assert by_meaning["title"] == "State v. Doe" and by_meaning["similarity"] > 0.9
    hybrid = fc.search(cache, embedder, "landlord", "hybrid", limit=1)[0]
    assert hybrid["title"] == "State v. Doe" and "vector" in hybrid["matched"]
    both = fc.search(cache, embedder, "eviction", "hybrid", limit=1)[0]
    assert both["matched"] == ["keyword", "vector"]


def test_search_filters(cache):
    embedder = ConceptEmbedder()
    fc.embed_pending(cache, embedder)

    def titles(**filters):
        return {r["title"] for r in fc.search(cache, embedder, "zoning debtor tenant page", "hybrid", filters, 10)}

    assert titles(source="dep") == {"Shoreland zoning amendments adopted"}
    assert titles(role=["environmental-land-use"]) == {"Shoreland zoning amendments adopted"}
    assert titles(docket="25-cv-00264") == {"In re Acme Corp."}
    assert titles(ld=1234) == {"Shoreland zoning amendments adopted"}
    assert titles(since="2026-10-03") == {"In re Acme Corp.", "Page updated: Fee schedule"}
    assert "Page updated: Fee schedule" not in titles(kind="item")


def test_odd_queries_do_not_break_fts(cache):
    for query in ['"unbalanced', "AND OR NOT", "a*b (c) -d", "NEAR(", "§ 1804(1)(A)", "   "]:
        fc.search(cache, None, query, "keyword")


def test_hashing_fallback_keeps_hybrid_on_keyword_ranking(cache):
    embedder = fc.HashingEmbedder()
    fc.embed_pending(cache, embedder)
    results = fc.search(cache, embedder, "shoreland zoning", "hybrid")
    assert results[0]["title"] == "Shoreland zoning amendments adopted"
    assert all(r["matched"] == ["keyword"] for r in results)
    # Explicit vector mode still works and is deterministic across processes.
    assert fc.search(cache, embedder, "shoreland zoning", "vector", limit=1)[0]["title"] == "Shoreland zoning amendments adopted"
    a, b = embedder.embed(["wetland setback rules"]), fc.HashingEmbedder().embed(["wetland setback rules"])
    assert np.array_equal(a, b) and abs(float(np.linalg.norm(a)) - 1.0) < 1e-5


def test_switching_embedding_model_requires_reindex(cache):
    fc.embed_pending(cache, fc.HashingEmbedder())
    with pytest.raises(SystemExit, match="reindex"):
        fc.embed_pending(cache, ConceptEmbedder())
    assert fc.embed_pending(cache, ConceptEmbedder(), reindex=True) == 4
    assert fc.meta_get(cache, "embed_model") == "fake:concepts"
    assert cache.execute("SELECT count(DISTINCT model) FROM vectors").fetchone()[0] == 1
    # `auto` resolves to the stored model, never silently to another one.
    cache.execute("UPDATE meta SET value = 'hashing-v1' WHERE key = 'embed_model'")
    assert fc.make_embedder(cache, "auto", None, None).identity == "hashing-v1"


# ---------------------------------------------------------------- checks --- #
def _levels(con, **opts):
    return {c["name"]: c["level"] for c in fc.run_checks(con, opts)}


def test_checks_healthy_cache(cache):
    fc.embed_pending(cache, ConceptEmbedder())
    levels = _levels(cache)
    assert set(levels.values()) == {fc.OK}, levels


def test_checks_flag_stalled_upstream_build_and_failing_sources(tmp_path):
    docs = tmp_path / "docs"
    stale = fc.iso(fc.utcnow() - timedelta(days=25))
    publish(docs, {"courts": ITEMS[:1], "dep": ITEMS[1:2], "third": ITEMS[2:3]}, generated_at=stale, health={"dep": "failing"})
    con = fc.connect(tmp_path / "c.db")
    fc.sync(con, fc.DirOrigin(docs), log=lambda *_: None)
    results = {c["name"]: c for c in fc.run_checks(con)}
    assert results["upstream build"]["level"] == fc.CRIT
    assert results["upstream source health"]["level"] == fc.WARN
    assert "1 of 3" in results["upstream source health"]["detail"] and "dep" in results["upstream source health"]["detail"]
    assert results["vector index"]["level"] == fc.WARN  # nothing embedded yet
    assert _levels(con, build_warn_hours=24 * 30, build_crit_hours=24 * 60)["upstream build"] == fc.OK


def test_checks_flag_unsynced_and_stale_cache(tmp_path, cache):
    empty = fc.connect(tmp_path / "empty.db")
    assert _levels(empty)["last sync"] == fc.CRIT
    cache.execute("UPDATE sync_runs SET finished_at = ?", (fc.iso(fc.utcnow() - timedelta(hours=20)),))
    assert _levels(cache)["last sync"] == fc.WARN
    cache.execute("UPDATE items SET first_ingested = '2026-01-01T00:00:00+00:00'")
    assert _levels(cache)["new items"] == fc.WARN


def test_check_command_exit_code_and_json(tmp_path, site, capsys):
    db = str(tmp_path / "cli.db")
    assert fc.main(["--db", db, "--embed-backend", "hashing", "sync", "--from-dir", str(site)]) == 0
    capsys.readouterr()
    assert fc.main(["--db", db, "check", "--json"]) == fc.OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "OK" and len(payload["checks"]) >= 7
    assert fc.main(["--db", db, "check", "--quiet"]) == fc.OK
    assert capsys.readouterr().out == ""
    assert fc.main(["--db", db, "check", "--quiet", "--sync-warn-hours", "-1"]) == fc.WARN
    assert capsys.readouterr().out.splitlines()[0].startswith("[WARNING ] last sync")
    assert fc.main(["--db", db, "check", "--sync-warn-hours", "-1"]) == fc.WARN
    assert fc.main(["--db", db, "search", "shoreland", "--json"]) == 0
    assert json.loads(capsys.readouterr().out.split("Overall: WARNING\n")[-1])[0]["ld"] == 1234


# ------------------------------------------------------------- watchlist --- #
def test_watchlist_reports_each_match_once(cache, site):
    embedder = ConceptEmbedder()
    fc.embed_pending(cache, embedder)
    watches = [
        {"name": "leases", "query": "landlord tenant disputes", "min_similarity": 0.8},
        {"name": "acme", "keywords": ["Acme Corp"]},
        {"name": "docket", "docket": "Som-25-305"},
        {"name": "dep-everything", "sources": ["dep"]},
        {"name": "no-monitors", "keywords": ["page"], "include_page_changes": False},
    ]
    report = {b["watch"]: [h["title"] for h in b["hits"]] for b in fc.run_watchlist(cache, embedder, watches)}
    assert report == {
        "leases": ["State v. Doe"], "acme": ["In re Acme Corp."], "docket": ["State v. Doe"],
        "dep-everything": ["Shoreland zoning amendments adopted"],
    }
    assert fc.run_watchlist(cache, embedder, watches) == []

    publish(site, {"courts": ITEMS[:1] + [_entry(8, "Acme Corp. v. Roe", "A new lease eviction matter.")], "dep": ITEMS[1:2]})
    fc.sync(cache, fc.DirOrigin(site), log=lambda *_: None)
    fc.embed_pending(cache, embedder)
    fresh = {b["watch"]: [h["title"] for h in b["hits"]] for b in fc.run_watchlist(cache, embedder, watches)}
    assert fresh == {"leases": ["Acme Corp. v. Roe"], "acme": ["Acme Corp. v. Roe"]}


def test_watchlist_baseline_and_guard_rails(cache):
    watches = [{"name": "acme", "keywords": ["acme corp"]}]
    assert fc.run_watchlist(cache, None, watches, baseline=True) == []
    assert fc.run_watchlist(cache, None, watches) == []
    with pytest.raises(SystemExit, match="match everything"):
        fc.run_watchlist(cache, None, [{"name": "too-broad"}])
    with pytest.raises(SystemExit, match="needs a name"):
        fc.run_watchlist(cache, None, [{"keywords": ["x"]}])


# ------------------------------------------------------------------ HTTP --- #
@pytest.fixture
def http_site(site):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *a, **k: None
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_sync_over_http_uses_conditional_requests(tmp_path, http_site):
    con = fc.connect(tmp_path / "h.db")
    first = fc.sync(con, fc.HttpOrigin(http_site), log=lambda *_: None)
    assert first["items_new"] == 4 and first["errors"] == []
    second = fc.sync(con, fc.HttpOrigin(http_site), log=lambda *_: None)
    assert second["files_fetched"] == 0 and second["files_unchanged"] == 2
    missing = fc.sync(con, fc.HttpOrigin(http_site + "/nope"), log=lambda *_: None)
    assert missing["errors"] and missing["errors"][0].startswith("catalog:")


def test_http_embedder_against_openai_compatible_endpoint(tmp_path):
    seen = []

    class Stub(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            data = [{"index": i, "embedding": [float(len(t)), 1.0, 0.0]} for i, t in enumerate(body["input"])]
            payload = json.dumps({"data": list(reversed(data))}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        embedder = fc.HttpEmbedder("nomic-embed-text", f"http://127.0.0.1:{server.server_port}/v1/embeddings")
        texts = ["a" * n for n in range(1, 41)]
        matrix = embedder.embed(texts)
    finally:
        server.shutdown()
        server.server_close()
    assert embedder.identity == "http:nomic-embed-text"
    assert matrix.shape == (40, 3) and len(seen) == 2 and seen[0]["model"] == "nomic-embed-text"
    assert np.allclose(np.linalg.norm(matrix, axis=1), 1.0, atol=1e-5)
    # Rows come back in input order even though the server answered out of order.
    assert np.all(np.diff(matrix[:, 0]) > 0)


def test_web_api(tmp_path, cache):
    embedder = ConceptEmbedder()
    fc.embed_pending(cache, embedder)
    db = cache.execute("PRAGMA database_list").fetchone()[2]
    server = fc.make_server(db, embedder, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path) as resp:
                return resp.status, resp.headers.get("Content-Type"), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers.get("Content-Type"), err.read()

    try:
        status, ctype, body = get("/")
        assert status == 200 and ctype.startswith("text/html") and b"firm cache" in body
        status, _, body = get("/api/search?q=landlord&mode=vector&limit=1")
        assert status == 200 and json.loads(body)["results"][0]["title"] == "State v. Doe"
        status, _, body = get("/api/search?q=zoning&source=dep&limit=999")
        assert status == 200 and len(json.loads(body)["results"]) == 1
        assert get("/api/search?q=")[0] == 400
        assert get("/api/search?q=x&mode=bogus")[0] == 400
        status, _, body = get("/api/health")
        assert status == 200 and json.loads(body)["status"] == "OK"
        status, _, body = get("/api/sources")
        assert json.loads(body)["items"] == 4
        assert get("/etc/passwd")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_sync_against_this_repositorys_published_docs(tmp_path):
    docs = ROOT / "docs"
    if not (docs / "feeds" / "archive").is_dir():
        pytest.skip("no published docs in this checkout")
    con = fc.connect(tmp_path / "real.db")
    stats = fc.sync(con, fc.DirOrigin(docs), log=lambda *_: None)
    assert stats["errors"] == [] and stats["items_new"] > 100
    assert fc.embed_pending(con, fc.HashingEmbedder()) >= stats["items_new"]
    assert fc.search(con, fc.HashingEmbedder(), "court", "hybrid", limit=3)
