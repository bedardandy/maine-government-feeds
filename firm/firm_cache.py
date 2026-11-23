#!/usr/bin/env python3
"""Local firm cache for Maine Government Feeds: one SQLite file holding every
published item, searchable by keyword (FTS5) and by meaning (vector
embeddings), with health checks and saved-search alerts.

Runs entirely on a machine the firm controls. The only outbound traffic is
HTTPS GETs of the public feed files (and, once, the embedding model download
if the fastembed backend is used). Search queries and watchlist terms never
leave the machine.

    firm_cache.py sync                    # pull published feeds -> cache, embed new text
    firm_cache.py search "shoreland zoning setback"
    firm_cache.py check                   # exit 0 ok / 1 warning / 2 critical
    firm_cache.py watch watchlist.yml     # print items newly matching saved searches
    firm_cache.py serve                   # http://127.0.0.1:8765 search page + JSON API
    firm_cache.py stats

See firm/README.md for installation, scheduling and the data model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

DEFAULT_BASE_URL = "https://bedardandy.github.io/maine-government-feeds"
DEFAULT_DB = "firm_cache.db"
DEFAULT_FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_HTTP_EMBED_URL = "http://127.0.0.1:11434/v1/embeddings"  # Ollama's OpenAI-compatible endpoint
USER_AGENT = "maine-feeds-firm-cache/1.0"

CHUNK_CHARS = 1200
CHUNK_OVERLAP_CHARS = 150
MAX_CHUNKS_PER_ITEM = 40
RRF_K = 60
CANDIDATES = 200

OK, WARN, CRIT = 0, 1, 2
LEVEL_NAMES = {OK: "OK", WARN: "WARNING", CRIT: "CRITICAL"}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sources (
    source_id TEXT PRIMARY KEY, name TEXT, category TEXT, subcategory TEXT,
    url TEXT, source_type TEXT, health TEXT, last_success TEXT,
    health_ignore INTEGER NOT NULL DEFAULT 0, item_count INTEGER, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    guid TEXT NOT NULL UNIQUE,
    url TEXT, title TEXT, content_text TEXT,
    date_published TEXT, first_seen TEXT,
    source_id TEXT, source_name TEXT, category TEXT, subcategory TEXT,
    roles TEXT NOT NULL DEFAULT '[]',
    dockets TEXT, ld INTEGER, effective_date TEXT,
    pdf_link INTEGER NOT NULL DEFAULT 0,
    minor_change INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'item',
    text_hash TEXT NOT NULL,
    first_ingested TEXT NOT NULL,
    last_changed TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_source ON items(source_id);
CREATE INDEX IF NOT EXISTS idx_items_first_seen ON items(first_seen);
CREATE INDEX IF NOT EXISTS idx_items_ingested ON items(first_ingested);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, content_text, content='items', content_rowid='id',
    tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, content_text)
    VALUES (new.id, new.title, new.content_text);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, content_text)
    VALUES ('delete', old.id, old.title, old.content_text);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF title, content_text ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, content_text)
    VALUES ('delete', old.id, old.title, old.content_text);
    INSERT INTO items_fts(rowid, title, content_text)
    VALUES (new.id, new.title, new.content_text);
END;
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    text TEXT NOT NULL,
    UNIQUE (item_id, seq)
);
CREATE TABLE IF NOT EXISTS vectors (
    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL,
    PRIMARY KEY (chunk_id, model)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY, etag TEXT, last_modified TEXT, sha256 TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL, finished_at TEXT, origin TEXT,
    upstream_generated_at TEXT, upstream_commit TEXT,
    files_fetched INTEGER DEFAULT 0, files_unchanged INTEGER DEFAULT 0,
    items_new INTEGER DEFAULT 0, items_updated INTEGER DEFAULT 0,
    errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS watch_hits (
    watch TEXT NOT NULL,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    matched_at TEXT NOT NULL, score REAL,
    PRIMARY KEY (watch, item_id)
) WITHOUT ROWID;
"""


def connect(db_path: str | Path, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        con = sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True)
    else:
        con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 10000")
    if not readonly:
        con.execute("PRAGMA journal_mode = WAL")
        try:
            con.executescript(SCHEMA)
        except sqlite3.OperationalError as exc:
            if "fts5" in str(exc).lower():
                raise SystemExit("This Python's SQLite lacks FTS5; use a standard python.org/distro build.") from exc
            raise
    return con


def meta_get(con: sqlite3.Connection, key: str) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def meta_set(con: sqlite3.Connection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# --------------------------------------------------------------------------- #
# Embedding backends. Each returns L2-normalised float32 rows, so cosine
# similarity is a plain dot product.
# --------------------------------------------------------------------------- #
def _normalise(matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were will with".split()
)
_WORD_RE = re.compile(r"[a-z0-9]+(?:[-'][a-z0-9]+)*")


class HashingEmbedder:
    """Dependency-free lexical fallback: hashed word + word-pair counts.

    It matches shared vocabulary, not meaning ("landlord" will not find
    "lessor"). It exists so the cache works on an air-gapped box and so the
    test suite needs no model download; use fastembed or an HTTP embedding
    server for real semantic search."""

    dim = 1024
    semantic = False

    def __init__(self) -> None:
        self.identity = "hashing-v1"

    @staticmethod
    def _features(text: str) -> list[str]:
        words = [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 1]
        return words + [f"{a} {b}" for a, b in zip(words, words[1:])]

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            counts: dict[str, int] = {}
            for feat in self._features(text):
                counts[feat] = counts.get(feat, 0) + 1
            for feat, count in counts.items():
                digest = hashlib.blake2b(feat.encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] & 1 else -1.0
                out[row, index] += sign * (1.0 + math.log(count))
        return _normalise(out)


class FastEmbedEmbedder:
    """Local ONNX model via fastembed (CPU is fine). The model is downloaded
    from Hugging Face on first use and cached on disk afterwards."""

    def __init__(self, model: str) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise SystemExit(
                "The fastembed backend needs `pip install fastembed` (see firm/requirements.txt)."
            ) from exc
        self.identity = f"fastembed:{model}"
        self._model = TextEmbedding(model_name=model, cache_dir=os.environ.get("MGF_MODEL_DIR") or None)

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        produce = self._model.query_embed if is_query else self._model.embed
        return _normalise(np.vstack(list(produce(texts))))


class HttpEmbedder:
    """Any OpenAI-compatible /v1/embeddings endpoint: Ollama, llama.cpp,
    LM Studio, text-embeddings-inference. Point it at a server on the firm's
    own network to keep text local."""

    def __init__(self, model: str, url: str) -> None:
        if not model:
            raise SystemExit("The http embedding backend needs --embed-model (e.g. nomic-embed-text).")
        import httpx

        self.identity = f"http:{model}"
        self._model = model
        self._url = url
        headers = {"User-Agent": USER_AGENT}
        if os.environ.get("MGF_EMBED_API_KEY"):
            headers["Authorization"] = f"Bearer {os.environ['MGF_EMBED_API_KEY']}"
        self._client = httpx.Client(timeout=120.0, headers=headers)

    def embed(self, texts: list[str], is_query: bool = False) -> np.ndarray:
        rows: list[list[float]] = []
        for start in range(0, len(texts), 32):
            resp = self._client.post(self._url, json={"model": self._model, "input": texts[start : start + 32]})
            resp.raise_for_status()
            data = sorted(resp.json()["data"], key=lambda d: d.get("index", 0))
            rows.extend(d["embedding"] for d in data)
        return _normalise(np.array(rows, dtype=np.float32))


def make_embedder(con: sqlite3.Connection, backend: str, model: str | None, url: str | None):
    """Resolve the embedding backend.

    `auto` reuses whatever model the cache was built with, so a search can
    never silently compare a query against vectors from a different model.
    On a brand-new cache `auto` means fastembed when installed, otherwise the
    lexical hashing fallback (with a notice)."""
    stored = meta_get(con, "embed_model")
    if backend == "auto":
        if stored:
            kind, _, name = stored.partition(":")
            if kind == "fastembed":
                return FastEmbedEmbedder(name)
            if kind == "http":
                return HttpEmbedder(name, url or meta_get(con, "embed_url") or DEFAULT_HTTP_EMBED_URL)
            return HashingEmbedder()
        try:
            import fastembed  # noqa: F401
        except ImportError:
            print(
                "note: fastembed is not installed; using the lexical hashing fallback. "
                "Install fastembed for semantic search, then run `embed --reindex`.",
                file=sys.stderr,
            )
            return HashingEmbedder()
        return FastEmbedEmbedder(model or DEFAULT_FASTEMBED_MODEL)
    if backend == "fastembed":
        return FastEmbedEmbedder(model or DEFAULT_FASTEMBED_MODEL)
    if backend == "http":
        return HttpEmbedder(model or "", url or DEFAULT_HTTP_EMBED_URL)
    return HashingEmbedder()


# --------------------------------------------------------------------------- #
# Ingest
# --------------------------------------------------------------------------- #
_PAGE_CHANGE_PREFIXES = ("Page updated:", "Page changed:")


def chunk_text(title: str, body: str) -> list[str]:
    """Split one item into embedding-sized passages, each prefixed with the
    title so a passage from the middle of an opinion still says what it is."""
    title = " ".join((title or "").split())
    body = (body or "").strip()
    if not body or body == title:
        return [title] if title else []
    words = body.split()
    pieces: list[str] = []
    current: list[str] = []
    size = 0
    for word in words:
        current.append(word)
        size += len(word) + 1
        if size >= CHUNK_CHARS:
            pieces.append(" ".join(current))
            tail: list[str] = []
            tail_size = 0
            for w in reversed(current):
                tail_size += len(w) + 1
                if tail_size > CHUNK_OVERLAP_CHARS:
                    break
                tail.insert(0, w)
            current, size = tail, tail_size
            if len(pieces) >= MAX_CHUNKS_PER_ITEM:
                current = []
                break
    if current and (not pieces or len(" ".join(current)) > CHUNK_OVERLAP_CHARS):
        pieces.append(" ".join(current))
    return [f"{title}\n{p}" if title else p for p in pieces]


def _row_from_feed_entry(entry: dict, source: dict) -> dict | None:
    """Normalise one JSON Feed item (or corpus.jsonl row) to a cache row."""
    guid = entry.get("id")
    if not guid:
        return None
    meta = entry.get("_meta") or {}
    tags = entry.get("tags") or []
    roles = entry.get("roles")
    if roles is None:
        roles = [t[5:] for t in tags if isinstance(t, str) and t.startswith("role:")]
    dockets = meta.get("docket") or []
    if isinstance(dockets, str):
        dockets = [dockets]
    title = entry.get("title") or ""
    url = entry.get("url") or ""
    pdf = bool(entry.get("pdf_link")) or any(
        (a.get("mime_type") == "application/pdf") for a in entry.get("attachments") or [] if isinstance(a, dict)
    )
    return {
        "guid": guid,
        "url": url,
        "title": title,
        "content_text": entry.get("content_text") or "",
        "date_published": entry.get("date_published"),
        "first_seen": entry.get("_first_seen") or entry.get("date_published"),
        "source_id": entry.get("source_id") or source.get("source_id"),
        "source_name": entry.get("source_name") or source.get("name"),
        "category": entry.get("category") or source.get("category"),
        "subcategory": entry.get("subcategory") or source.get("subcategory"),
        "roles": json.dumps(sorted(set(roles))),
        "dockets": ";".join(dockets) if dockets else None,
        "ld": meta.get("ld") if isinstance(meta.get("ld"), int) else None,
        "effective_date": meta.get("effective_date"),
        "pdf_link": 1 if pdf else 0,
        "minor_change": 1 if entry.get("_minor_change") else 0,
        "kind": "page-change" if title.startswith(_PAGE_CHANGE_PREFIXES) else "item",
    }


_ITEM_COLUMNS = (
    "url", "title", "content_text", "date_published", "first_seen", "source_id", "source_name",
    "category", "subcategory", "roles", "dockets", "ld", "effective_date", "pdf_link", "minor_change", "kind",
)


def upsert_item(con: sqlite3.Connection, row: dict, now: str) -> str:
    """Insert or update one item. Returns 'new', 'updated' or 'same'.

    The cache only ever grows: an item that has rotated out of the upstream
    feeds stays here. A body-less re-observation (text equal to the title)
    never overwrites a body that was captured earlier."""
    prior = con.execute("SELECT * FROM items WHERE guid = ?", (row["guid"],)).fetchone()
    if prior is not None and row["content_text"] in ("", row["title"]) and prior["content_text"] not in (
        "", prior["title"],
    ):
        row = dict(row, content_text=prior["content_text"])
    if prior is not None and prior["source_id"] and prior["source_id"] != row["source_id"]:
        # The same document published through two sources (one upstream feed
        # split by filters, say) is one cache item; the first source keeps it.
        row = dict(row, **{c: prior[c] for c in ("source_id", "source_name", "category", "subcategory", "roles")})
    text_hash = hashlib.sha256(f"{row['title']}\n{row['content_text']}".encode("utf-8")).hexdigest()
    if prior is None:
        con.execute(
            f"INSERT INTO items (guid, {', '.join(_ITEM_COLUMNS)}, text_hash, first_ingested, last_changed) "
            f"VALUES (?, {', '.join('?' for _ in _ITEM_COLUMNS)}, ?, ?, ?)",
            (row["guid"], *(row[c] for c in _ITEM_COLUMNS), text_hash, now, now),
        )
        return "new"
    if all(prior[c] == row[c] for c in _ITEM_COLUMNS):
        return "same"
    con.execute(
        f"UPDATE items SET {', '.join(f'{c} = ?' for c in _ITEM_COLUMNS)}, text_hash = ?, last_changed = ? WHERE id = ?",
        (*(row[c] for c in _ITEM_COLUMNS), text_hash, now, prior["id"]),
    )
    if prior["text_hash"] != text_hash:
        # Text changed: drop the old passages (and, by cascade, their vectors)
        # so the next embed pass rebuilds them.
        con.execute("DELETE FROM chunks WHERE item_id = ?", (prior["id"],))
    return "updated"


def build_chunks(con: sqlite3.Connection) -> int:
    """Create passages for every item that has none yet."""
    rows = con.execute(
        "SELECT id, title, content_text FROM items WHERE NOT EXISTS (SELECT 1 FROM chunks WHERE item_id = items.id)"
    ).fetchall()
    made = 0
    for row in rows:
        for seq, text in enumerate(chunk_text(row["title"], row["content_text"])):
            con.execute("INSERT INTO chunks(item_id, seq, text) VALUES (?, ?, ?)", (row["id"], seq, text))
            made += 1
    return made


def embed_pending(con: sqlite3.Connection, embedder, batch: int = 64, reindex: bool = False) -> int:
    """Embed every passage that has no vector for the active model."""
    stored = meta_get(con, "embed_model")
    if stored and stored != embedder.identity and not reindex:
        raise SystemExit(
            f"This cache was embedded with '{stored}' but '{embedder.identity}' was requested. "
            "Run `embed --reindex` with the new backend to switch models."
        )
    if reindex:
        con.execute("DELETE FROM vectors")
    meta_set(con, "embed_model", embedder.identity)
    build_chunks(con)
    done = 0
    while True:
        rows = con.execute(
            "SELECT c.id, c.text FROM chunks c WHERE NOT EXISTS "
            "(SELECT 1 FROM vectors v WHERE v.chunk_id = c.id AND v.model = ?) ORDER BY c.id LIMIT ?",
            (embedder.identity, batch),
        ).fetchall()
        if not rows:
            break
        matrix = embedder.embed([r["text"] for r in rows])
        con.executemany(
            "INSERT INTO vectors(chunk_id, model, dim, vec) VALUES (?, ?, ?, ?)",
            [(r["id"], embedder.identity, int(matrix.shape[1]), matrix[i].tobytes()) for i, r in enumerate(rows)],
        )
        done += len(rows)
        meta_set(con, "vectors_rev", str(int(meta_get(con, "vectors_rev") or 0) + 1))
        con.commit()
    con.commit()
    return done


# --------------------------------------------------------------------------- #
# Sync: published files -> cache
# --------------------------------------------------------------------------- #
class DirOrigin:
    """Read published files from a local `docs/` directory (a git checkout or
    an rsync mirror) instead of the network."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.label = f"dir:{self.root}"

    def get(self, con: sqlite3.Connection, rel: str, force: bool = False) -> bytes | None:
        path = self.root / rel
        if not path.is_file():
            raise FileNotFoundError(rel)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        key = f"{self.label}/{rel}"
        row = con.execute("SELECT sha256 FROM http_cache WHERE key = ?", (key,)).fetchone()
        if row and row["sha256"] == digest and not force:
            return None
        self._pending = (key, None, None, digest)
        return data

    def remember(self, con: sqlite3.Connection) -> None:
        key, etag, last_modified, digest = self._pending
        con.execute(
            "INSERT INTO http_cache(key, etag, last_modified, sha256, fetched_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET etag=excluded.etag, last_modified=excluded.last_modified, "
            "sha256=excluded.sha256, fetched_at=excluded.fetched_at",
            (key, etag, last_modified, digest, iso(utcnow())),
        )


class HttpOrigin(DirOrigin):
    """Fetch published files over HTTPS with conditional requests, so an
    unchanged file costs one 304 and no transfer."""

    def __init__(self, base_url: str) -> None:
        import httpx

        self.base = base_url.rstrip("/")
        self.label = self.base
        self._client = httpx.Client(timeout=30.0, follow_redirects=True, headers={"User-Agent": USER_AGENT})

    def get(self, con: sqlite3.Connection, rel: str, force: bool = False) -> bytes | None:
        key = f"{self.base}/{rel}"
        headers = {}
        row = con.execute("SELECT etag, last_modified FROM http_cache WHERE key = ?", (key,)).fetchone()
        if row and not force:
            if row["etag"]:
                headers["If-None-Match"] = row["etag"]
            if row["last_modified"]:
                headers["If-Modified-Since"] = row["last_modified"]
        resp = self._client.get(key, headers=headers)
        if resp.status_code == 304:
            return None
        resp.raise_for_status()
        data = resp.content
        self._pending = (key, resp.headers.get("ETag"), resp.headers.get("Last-Modified"), hashlib.sha256(data).hexdigest())
        return data


def _manifest_hashes(data: bytes) -> dict[str, str]:
    manifest = json.loads(data)
    return {e["path"].replace("\\", "/"): e["sha256"] for e in manifest.get("files", []) if "path" in e}


def sync(con: sqlite3.Connection, origin, live_only: bool = False, log=print) -> dict:
    """Pull the catalog and every source's feed into the cache.

    Each downloaded feed file is checked against the SHA-256 the build
    recorded in manifest.json; a file that does not match is not ingested."""
    started = iso(utcnow())
    stats = {"files_fetched": 0, "files_unchanged": 0, "items_new": 0, "items_updated": 0, "errors": []}
    run_id = con.execute("INSERT INTO sync_runs(started_at, origin) VALUES (?, ?)", (started, origin.label)).lastrowid
    con.commit()
    generated_at = commit = None
    try:
        catalog = json.loads(origin.get(con, "feeds/json/catalog.json", force=True))
        generated_at, commit = catalog.get("_generated_at"), catalog.get("_build_commit")
        try:
            hashes = _manifest_hashes(origin.get(con, "feeds/json/manifest.json", force=True))
        except Exception as exc:  # a missing manifest downgrades verification, it does not stop the sync
            hashes = {}
            stats["errors"].append(f"manifest unavailable ({type(exc).__name__}: {exc}); files ingested unverified")
        manifest_refreshed = False

        entries = [e for e in catalog.get("items", []) if e.get("_archive_url") and e.get("id")]
        for entry in entries:
            source = {
                "source_id": entry["id"], "name": entry.get("title"), "category": entry.get("_category"),
                "subcategory": entry.get("_subcategory"),
            }
            con.execute(
                "INSERT INTO sources(source_id, name, category, subcategory, url, source_type, health, last_success, "
                "health_ignore, item_count, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(source_id) DO UPDATE SET name=excluded.name, category=excluded.category, "
                "subcategory=excluded.subcategory, url=excluded.url, source_type=excluded.source_type, "
                "health=excluded.health, last_success=excluded.last_success, health_ignore=excluded.health_ignore, "
                "item_count=excluded.item_count, updated_at=excluded.updated_at",
                (
                    entry["id"], entry.get("title"), entry.get("_category"), entry.get("_subcategory"),
                    entry.get("url"), entry.get("_source_type"), entry.get("_health"), entry.get("_last_success"),
                    1 if entry.get("_health_ignore") else 0, entry.get("_item_count"), started,
                ),
            )
            rel = f"feeds/{'json' if live_only else 'archive'}/{entry['id']}.json"
            try:
                data = origin.get(con, rel)
                if data is None:
                    stats["files_unchanged"] += 1
                    continue
                digest = hashlib.sha256(data).hexdigest()
                if hashes and hashes.get(rel) != digest and not manifest_refreshed:
                    # A new build may have been published between our manifest
                    # fetch and this file; re-read the manifest once.
                    manifest_refreshed = True
                    hashes = _manifest_hashes(origin.get(con, "feeds/json/manifest.json", force=True))
                if hashes and hashes.get(rel) != digest:
                    stats["errors"].append(f"integrity: {rel} does not match manifest.json sha256; not ingested")
                    continue
                now = iso(utcnow())
                for feed_item in json.loads(data).get("items", []):
                    row = _row_from_feed_entry(feed_item, source)
                    if row is None:
                        continue
                    outcome = upsert_item(con, row, now)
                    if outcome == "new":
                        stats["items_new"] += 1
                    elif outcome == "updated":
                        stats["items_updated"] += 1
                origin.remember(con)
                stats["files_fetched"] += 1
                con.commit()
            except Exception as exc:  # one bad file must not stop the rest
                con.rollback()
                stats["errors"].append(f"{rel}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        con.rollback()
        stats["errors"].append(f"catalog: {type(exc).__name__}: {exc}")
    con.execute(
        "UPDATE sync_runs SET finished_at=?, upstream_generated_at=?, upstream_commit=?, files_fetched=?, "
        "files_unchanged=?, items_new=?, items_updated=?, errors=? WHERE id=?",
        (
            iso(utcnow()), generated_at, commit, stats["files_fetched"], stats["files_unchanged"],
            stats["items_new"], stats["items_updated"], json.dumps(stats["errors"]), run_id,
        ),
    )
    con.commit()
    log(
        f"sync: {stats['files_fetched']} file(s) read, {stats['files_unchanged']} unchanged, "
        f"{stats['items_new']} new item(s), {stats['items_updated']} updated, {len(stats['errors'])} error(s)"
    )
    for err in stats["errors"]:
        log(f"  ! {err}")
    return stats


def import_corpus(con: sqlite3.Connection, path: Path) -> dict:
    """Merge a corpus.jsonl export (the build's CI artifact) into the cache."""
    stats = {"items_new": 0, "items_updated": 0}
    now = iso(utcnow())
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = _row_from_feed_entry(json.loads(line), {})
            except json.JSONDecodeError:
                continue
            if row is None:
                continue
            outcome = upsert_item(con, row, now)
            if outcome != "same":
                stats[f"items_{outcome}"] += 1
    con.commit()
    return stats


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
def _filter_sql(filters: dict) -> tuple[str, list]:
    clauses, params = [], []
    for column, key in (("source_id", "source"), ("category", "category"), ("kind", "kind")):
        values = filters.get(key)
        if values:
            values = [values] if isinstance(values, str) else list(values)
            clauses.append(f"items.{column} IN ({', '.join('?' for _ in values)})")
            params.extend(values)
    roles = filters.get("role")
    if roles:
        roles = [roles] if isinstance(roles, str) else list(roles)
        clauses.append(
            f"EXISTS (SELECT 1 FROM json_each(items.roles) WHERE value IN ({', '.join('?' for _ in roles)}))"
        )
        params.extend(roles)
    if filters.get("since"):
        clauses.append("COALESCE(items.first_seen, items.date_published, items.first_ingested) >= ?")
        params.append(filters["since"])
    if filters.get("docket"):
        clauses.append("items.dockets LIKE ?")
        params.append(f"%{filters['docket']}%")
    if filters.get("ld") is not None:
        clauses.append("items.ld = ?")
        params.append(int(filters["ld"]))
    return (" AND ".join(clauses) or "1=1"), params


def _fts_expression(query: str, joiner: str) -> str:
    terms = [t for t in re.findall(r"\w+", query.lower()) if t not in _STOPWORDS] or re.findall(r"\w+", query.lower())
    return joiner.join(f'"{t}"' for t in terms)


def keyword_candidates(con: sqlite3.Connection, query: str, where: str, params: list, limit: int) -> list[tuple[int, str]]:
    """FTS5 matches, best first: all terms required, then any term to fill."""
    found: dict[int, str] = {}
    for joiner in (" AND ", " OR "):
        expr = _fts_expression(query, joiner)
        if not expr:
            break
        rows = con.execute(
            "SELECT items.id, snippet(items_fts, 1, '[', ']', ' … ', 24) AS snip FROM items_fts "
            f"JOIN items ON items.id = items_fts.rowid WHERE items_fts MATCH ? AND {where} "
            "ORDER BY bm25(items_fts, 4.0, 1.0) LIMIT ?",
            (expr, *params, limit),
        ).fetchall()
        for row in rows:
            found.setdefault(row["id"], row["snip"])
        if len(found) >= limit:
            break
    return list(found.items())[:limit]


_matrix_cache: dict[tuple, tuple] = {}


def load_matrix(con: sqlite3.Connection, model: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(chunk ids, item ids, vectors) for one model, cached per process and
    reloaded when new vectors have been written."""
    db_file = con.execute("PRAGMA database_list").fetchone()[2]
    key = (db_file, model)
    rev = meta_get(con, "vectors_rev")
    cached = _matrix_cache.get(key)
    if cached and cached[0] == rev:
        return cached[1]
    rows = con.execute(
        "SELECT v.chunk_id, c.item_id, v.dim, v.vec FROM vectors v JOIN chunks c ON c.id = v.chunk_id "
        "WHERE v.model = ? ORDER BY v.chunk_id",
        (model,),
    ).fetchall()
    if rows:
        dim = rows[0]["dim"]
        matrix = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), dim)
    else:
        matrix = np.zeros((0, 1), dtype=np.float32)
    loaded = (
        np.array([r["chunk_id"] for r in rows], dtype=np.int64),
        np.array([r["item_id"] for r in rows], dtype=np.int64),
        matrix,
    )
    _matrix_cache[key] = (rev, loaded)
    return loaded


def vector_candidates(
    con: sqlite3.Connection, embedder, query: str, where: str, params: list, limit: int
) -> list[tuple[int, float, int]]:
    """(item id, cosine similarity, best chunk id), best first."""
    chunk_ids, item_ids, matrix = load_matrix(con, embedder.identity)
    if not len(chunk_ids):
        return []
    sims = matrix @ embedder.embed([query], is_query=True)[0]
    if where != "1=1":
        allowed = {r[0] for r in con.execute(f"SELECT id FROM items WHERE {where}", params)}
        sims = np.where(np.isin(item_ids, list(allowed)), sims, -np.inf)
    best: dict[int, tuple[float, int]] = {}
    for index in np.argsort(-sims)[: limit * 4]:
        score = float(sims[index])
        if score == -np.inf:
            break
        item = int(item_ids[index])
        if item not in best:
            best[item] = (score, int(chunk_ids[index]))
            if len(best) >= limit:
                break
    return [(item, score, chunk) for item, (score, chunk) in best.items()]


def search(
    con: sqlite3.Connection, embedder, query: str, mode: str = "hybrid", filters: dict | None = None, limit: int = 10
) -> list[dict]:
    """Search the cache.

    keyword: FTS5/BM25.  vector: cosine similarity of the query embedding
    against every passage.  hybrid (default): both lists merged by reciprocal
    rank fusion, so an exact docket number and a paraphrase both surface."""
    where, params = _filter_sql(filters or {})
    if mode == "hybrid" and not getattr(embedder, "semantic", True):
        # The lexical fallback adds nothing BM25 does not already do better.
        mode = "keyword"
    keyword = keyword_candidates(con, query, where, params, CANDIDATES) if mode in ("keyword", "hybrid") else []
    vector = vector_candidates(con, embedder, query, where, params, CANDIDATES) if mode in ("vector", "hybrid") else []

    scores: dict[int, float] = {}
    for rank, (item, _snip) in enumerate(keyword):
        scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, (item, _sim, _chunk) in enumerate(vector):
        scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + rank + 1)
    snippets = dict(keyword)
    similarity = {item: (sim, chunk) for item, sim, chunk in vector}

    results = []
    for item in sorted(scores, key=scores.get, reverse=True)[:limit]:
        row = dict(con.execute("SELECT * FROM items WHERE id = ?", (item,)).fetchone())
        snippet = snippets.get(item)
        if not snippet and item in similarity:
            chunk = con.execute("SELECT text FROM chunks WHERE id = ?", (similarity[item][1],)).fetchone()
            snippet = " ".join(chunk["text"].split("\n", 1)[-1].split())[:240] if chunk else None
        results.append(
            {
                "score": round(scores[item], 5),
                "similarity": round(similarity[item][0], 4) if item in similarity else None,
                "matched": [m for m, present in (("keyword", item in snippets), ("vector", item in similarity)) if present],
                "title": row["title"], "url": row["url"], "source_id": row["source_id"],
                "source_name": row["source_name"], "category": row["category"],
                "date_published": row["date_published"], "first_seen": row["first_seen"],
                "roles": json.loads(row["roles"] or "[]"), "dockets": row["dockets"], "ld": row["ld"],
                "kind": row["kind"], "guid": row["guid"], "id": row["id"],
                "snippet": snippet or (row["content_text"] or "")[:240],
            }
        )
    return results


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #
def run_checks(con: sqlite3.Connection, opts: dict | None = None) -> list[dict]:
    """Health checks for monitoring. Each result: name, level (0/1/2), detail."""
    opts = opts or {}
    now = utcnow()
    results: list[dict] = []

    def add(name: str, level: int, detail: str) -> None:
        results.append({"name": name, "level": level, "status": LEVEL_NAMES[level], "detail": detail})

    def age_check(name: str, when: datetime | None, warn_h: float, crit_h: float, missing: str) -> None:
        if when is None:
            add(name, CRIT, missing)
            return
        hours = (now - when).total_seconds() / 3600.0
        level = CRIT if hours > crit_h else WARN if hours > warn_h else OK
        add(name, level, f"{hours:.1f}h ago ({iso(when)}); warn >{warn_h:g}h, critical >{crit_h:g}h")

    # 1. The database itself.
    try:
        verdict = con.execute("PRAGMA quick_check").fetchone()[0]
        add("database integrity", OK if verdict == "ok" else CRIT, verdict)
    except sqlite3.DatabaseError as exc:
        add("database integrity", CRIT, str(exc))
        return results
    items = con.execute("SELECT count(*) FROM items").fetchone()[0]
    indexed = con.execute("SELECT count(*) FROM items_fts").fetchone()[0]
    add(
        "keyword index", OK if items == indexed else CRIT,
        f"{indexed} of {items} item(s) indexed" + ("" if items == indexed else "; rebuild with `repair`"),
    )

    # 2. Is this cache being refreshed?
    last = con.execute("SELECT * FROM sync_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    age_check(
        "last sync", parse_ts(last["finished_at"]) if last else None,
        opts.get("sync_warn_hours", 13), opts.get("sync_crit_hours", 48), "no sync has completed yet",
    )
    if last:
        errors = json.loads(last["errors"] or "[]")
        integrity = [e for e in errors if e.startswith("integrity")]
        if integrity:
            add("last sync errors", CRIT, f"{len(integrity)} file(s) failed SHA-256 verification: {integrity[0]}")
        elif errors:
            add("last sync errors", WARN, f"{len(errors)} error(s); first: {errors[0]}")
        else:
            add("last sync errors", OK, "none")

    # 3. Is the upstream build still publishing? (Builds run every 6 hours.)
    age_check(
        "upstream build", parse_ts(last["upstream_generated_at"]) if last else None,
        opts.get("build_warn_hours", 18), opts.get("build_crit_hours", 72),
        "upstream build time unknown (no successful catalog fetch)",
    )

    # 4. Which upstream sources are not being verified?
    failing = con.execute(
        "SELECT source_id, health, last_success FROM sources "
        "WHERE health_ignore = 0 AND COALESCE(health, '') != 'ok' ORDER BY source_id"
    ).fetchall()
    total_sources = con.execute("SELECT count(*) FROM sources").fetchone()[0]
    if failing:
        names = ", ".join(r["source_id"] for r in failing[:8]) + (" …" if len(failing) > 8 else "")
        oldest = min((r["last_success"] or "never") for r in failing)
        share = len(failing) / max(total_sources, 1)
        add(
            "upstream source health", CRIT if share > opts.get("failing_crit_share", 0.5) else WARN,
            f"{len(failing)} of {total_sources} source(s) failing upstream (oldest last success {oldest[:10]}): {names}",
        )
    else:
        add("upstream source health", OK, f"all {total_sources} source(s) verified upstream")

    # 5. Is new material arriving?
    quiet_days = opts.get("quiet_days", 7)
    newest = con.execute("SELECT max(first_ingested) FROM items").fetchone()[0]
    recent = con.execute(
        "SELECT count(*) FROM items WHERE first_ingested >= ?", (iso(now - timedelta(days=quiet_days)),)
    ).fetchone()[0]
    add(
        "new items", OK if recent else WARN,
        f"{recent} new item(s) in the last {quiet_days} day(s); newest ingested {newest or 'never'}",
    )

    # 6. Is every passage embedded with the active model?
    model = meta_get(con, "embed_model")
    chunks = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
    unchunked = con.execute(
        "SELECT count(*) FROM items WHERE NOT EXISTS (SELECT 1 FROM chunks WHERE item_id = items.id) "
        "AND COALESCE(title, '') != ''"
    ).fetchone()[0]
    embedded = con.execute("SELECT count(*) FROM vectors WHERE model = ?", (model or "",)).fetchone()[0]
    if not model:
        add("vector index", WARN if items else OK, "no embeddings yet; run `embed`")
    elif unchunked or embedded < chunks:
        add("vector index", WARN, f"{embedded} of {chunks} passage(s) embedded with {model}; {unchunked} item(s) not yet split; run `embed`")
    else:
        note = " (lexical fallback, not semantic)" if model.startswith("hashing") else ""
        add("vector index", OK, f"{embedded} passage(s) embedded with {model}{note}")
    return results


# --------------------------------------------------------------------------- #
# Watchlist: saved searches that report only what is new
# --------------------------------------------------------------------------- #
def run_watchlist(con: sqlite3.Connection, embedder, watches: list[dict], baseline: bool = False) -> list[dict]:
    """Evaluate each saved search and return items that match it for the
    first time. Matches are recorded, so each item is reported once."""
    now = iso(utcnow())
    report = []
    for watch in watches:
        name = watch.get("name")
        if not name:
            raise SystemExit("Every watch needs a name.")
        filters = {
            "source": watch.get("sources"), "category": watch.get("categories"), "role": watch.get("roles"),
            "docket": watch.get("docket"), "ld": watch.get("ld"), "since": watch.get("since"),
            "kind": None if watch.get("include_page_changes", True) else "item",
        }
        where, params = _filter_sql(filters)
        matched: dict[int, float | None] = {}
        keywords = watch.get("keywords") or []
        if keywords:
            phrases = []
            for k in keywords:
                words = re.findall(r"\w+", str(k).lower())
                if words:
                    phrases.append('"' + " ".join(words) + '"')
            expr = " OR ".join(phrases)
            if expr:
                for row in con.execute(
                    f"SELECT items.id FROM items_fts JOIN items ON items.id = items_fts.rowid "
                    f"WHERE items_fts MATCH ? AND {where}",
                    (expr, *params),
                ):
                    matched[row[0]] = None
        if watch.get("query"):
            floor = float(watch.get("min_similarity", 0.6))
            for item, sim, _chunk in vector_candidates(con, embedder, watch["query"], where, params, CANDIDATES):
                if sim >= floor:
                    matched[item] = sim
        if not keywords and not watch.get("query"):
            if where == "1=1":
                raise SystemExit(f"Watch '{name}' has no query, keywords or filters; it would match everything.")
            for row in con.execute(f"SELECT id FROM items WHERE {where}", params):
                matched[row[0]] = None

        seen = {r[0] for r in con.execute("SELECT item_id FROM watch_hits WHERE watch = ?", (name,))}
        fresh = [i for i in matched if i not in seen]
        con.executemany(
            "INSERT INTO watch_hits(watch, item_id, matched_at, score) VALUES (?, ?, ?, ?)",
            [(name, i, now, matched[i]) for i in fresh],
        )
        if fresh and not baseline:
            rows = con.execute(
                f"SELECT id, title, url, source_name, date_published, first_seen, dockets FROM items "
                f"WHERE id IN ({', '.join('?' for _ in fresh)}) ORDER BY COALESCE(first_seen, date_published) DESC",
                fresh,
            ).fetchall()
            report.append(
                {"watch": name, "hits": [dict(r, similarity=matched[r["id"]]) for r in rows]}
            )
    con.commit()
    return report


# --------------------------------------------------------------------------- #
# Local web interface
# --------------------------------------------------------------------------- #
_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maine Government Feeds: firm cache</title>
<style>
:root { color-scheme: light dark; --fg: #1b1f24; --muted: #5b6570; --bg: #fff; --line: #d6dbe1; --accent: #0b5cad; }
@media (prefers-color-scheme: dark) { :root { --fg: #e6e9ed; --muted: #9aa5b1; --bg: #14171a; --line: #2e353d; --accent: #6db3f2; } }
body { font: 16px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg); margin: 0; }
main { max-width: 860px; margin: 0 auto; padding: 24px 16px 64px; }
h1 { font-size: 1.25rem; margin: 0 0 16px; }
form { display: flex; flex-wrap: wrap; gap: 8px; }
input[type=search] { flex: 1 1 320px; }
input, select, button { font: inherit; padding: 8px 10px; border: 1px solid var(--line); border-radius: 6px; background: var(--bg); color: var(--fg); }
button { background: var(--accent); color: #fff; border-color: var(--accent); cursor: pointer; }
#status { color: var(--muted); font-size: .875rem; margin: 12px 0; }
article { border-top: 1px solid var(--line); padding: 14px 0; }
article h2 { font-size: 1rem; margin: 0 0 4px; }
article a { color: var(--accent); text-decoration: none; overflow-wrap: anywhere; }
article a:hover { text-decoration: underline; }
.meta { color: var(--muted); font-size: .8125rem; }
.snip { margin: 6px 0 0; overflow-wrap: anywhere; }
</style></head><body><main>
<h1>Maine Government Feeds: firm cache</h1>
<form id="f">
  <input type="search" id="q" placeholder="Search opinions, rules, notices…" autofocus required>
  <select id="mode" aria-label="Search mode"><option value="hybrid">Hybrid</option><option value="vector">Meaning</option><option value="keyword">Keyword</option></select>
  <select id="category" aria-label="Category"><option value="">All categories</option></select>
  <select id="since" aria-label="Date range"><option value="">Any date</option><option value="7">Last 7 days</option><option value="30">Last 30 days</option><option value="365">Last year</option></select>
  <button>Search</button>
</form>
<div id="status"></div><div id="results"></div>
</main><script>
const $ = id => document.getElementById(id);
const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = text; if (cls) n.className = cls; return n; };
fetch('api/sources').then(r => r.json()).then(d => {
  [...new Set(d.sources.map(s => s.category).filter(Boolean))].sort().forEach(c => $('category').append(new Option(c, c)));
  $('status').textContent = d.items + ' items cached from ' + d.sources.length + ' sources. Last sync ' + (d.last_sync || 'never') + '.';
});
$('f').addEventListener('submit', async ev => {
  ev.preventDefault();
  const p = new URLSearchParams({ q: $('q').value, mode: $('mode').value, limit: 25 });
  if ($('category').value) p.set('category', $('category').value);
  if ($('since').value) p.set('since', new Date(Date.now() - $('since').value * 864e5).toISOString());
  $('status').textContent = 'Searching…';
  const res = await fetch('api/search?' + p); const data = await res.json();
  $('results').replaceChildren();
  if (!res.ok) { $('status').textContent = data.error || 'Search failed.'; return; }
  $('status').textContent = data.results.length + ' result(s).';
  for (const r of data.results) {
    const a = el('a', r.title || r.url); a.href = /^https?:/.test(r.url || '') ? r.url : '#'; a.rel = 'noopener noreferrer'; a.target = '_blank';
    const h = el('h2'); h.append(a);
    const bits = [r.source_name, (r.date_published || r.first_seen || '').slice(0, 10), r.dockets, r.roles.join(', '), r.similarity != null ? 'similarity ' + r.similarity.toFixed(2) : null];
    const art = el('article'); art.append(h, el('div', bits.filter(Boolean).join(' · '), 'meta'), el('p', r.snippet, 'snip'));
    $('results').append(art);
  }
});
</script></body></html>
"""


def make_server(db_path: str, embedder, host: str, port: int):
    """Build (but do not start) the local HTTP server."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        server_version = "firm-cache"

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            url = urlparse(self.path)
            args = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                con = connect(db_path, readonly=True)
            except sqlite3.Error as exc:
                self._json({"error": f"database unavailable: {exc}"}, 503)
                return
            try:
                if url.path in ("/", "/index.html"):
                    self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif url.path == "/api/search":
                    if not args.get("q", "").strip():
                        self._json({"error": "missing q"}, 400)
                        return
                    mode = args.get("mode", "hybrid")
                    if mode not in ("hybrid", "vector", "keyword"):
                        self._json({"error": "mode must be hybrid, vector or keyword"}, 400)
                        return
                    filters = {k: args.get(k) for k in ("source", "category", "role", "since", "docket", "kind")}
                    if args.get("ld", "").isdigit():
                        filters["ld"] = int(args["ld"])
                    limit = max(1, min(int(args["limit"]) if args.get("limit", "").isdigit() else 10, 100))
                    self._json({"results": search(con, embedder, args["q"], mode, filters, limit)})
                elif url.path == "/api/health":
                    results = run_checks(con)
                    worst = max(r["level"] for r in results)
                    self._json({"status": LEVEL_NAMES[worst], "checks": results}, 200 if worst < CRIT else 503)
                elif url.path == "/api/sources":
                    last = con.execute("SELECT max(finished_at) FROM sync_runs").fetchone()[0]
                    self._json(
                        {
                            "items": con.execute("SELECT count(*) FROM items").fetchone()[0],
                            "last_sync": last,
                            "sources": [dict(r) for r in con.execute("SELECT * FROM sources ORDER BY category, name")],
                        }
                    )
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as exc:  # keep the server up; report the failure to the caller
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            finally:
                con.close()

        def log_message(self, fmt: str, *args) -> None:
            # Access logs deliberately omit the query string: search terms can
            # be client-confidential and should not land in a log file.
            sys.stderr.write(f"{self.address_string()} {self.command} {urlparse(self.path).path} {args[1] if len(args) > 1 else ''}\n")

    return ThreadingHTTPServer((host, port), Handler)


def serve(db_path: str, embed_args: tuple, host: str, port: int) -> None:
    setup = connect(db_path)
    embedder = make_embedder(setup, *embed_args)
    setup.close()
    server = make_server(db_path, embedder, host, port)
    print(f"Serving {db_path} on http://{host}:{port}/  (Ctrl+C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("note: listening beyond localhost with no authentication; restrict access at the firewall or a reverse proxy.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _print_results(results: list[dict]) -> None:
    if not results:
        print("No results.")
        return
    for n, r in enumerate(results, 1):
        when = (r["date_published"] or r["first_seen"] or "")[:10]
        sim = f"  sim {r['similarity']:.2f}" if r["similarity"] is not None else ""
        print(f"{n:>2}. {r['title']}")
        print(f"    {r['source_name']} · {when}{sim} · via {'+'.join(r['matched'])}")
        print(f"    {r['url']}")
        if r["snippet"]:
            print(f"    {' '.join(r['snippet'].split())[:220]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.environ.get("MGF_DB", DEFAULT_DB), help="SQLite cache file (env MGF_DB)")
    ap.add_argument(
        "--embed-backend", choices=("auto", "fastembed", "http", "hashing"),
        default=os.environ.get("MGF_EMBED_BACKEND", "auto"),
        help="auto = whatever the cache was built with (fastembed on a new cache when installed)",
    )
    ap.add_argument("--embed-model", default=os.environ.get("MGF_EMBED_MODEL"))
    ap.add_argument("--embed-url", default=os.environ.get("MGF_EMBED_URL"), help="OpenAI-compatible embeddings URL for the http backend")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="pull published feeds into the cache and embed new text")
    p.add_argument("--base-url", default=os.environ.get("MGF_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--from-dir", type=Path, help="read a local docs/ directory instead of the network")
    p.add_argument("--corpus", type=Path, help="also merge a corpus.jsonl export")
    p.add_argument("--live-only", action="store_true", help="read the 50-item live feeds instead of the archives")
    p.add_argument("--no-embed", action="store_true")

    p = sub.add_parser("embed", help="embed passages that have no vector yet")
    p.add_argument("--reindex", action="store_true", help="discard all vectors and re-embed with the selected backend")

    p = sub.add_parser("search", help="search the cache")
    p.add_argument("query")
    p.add_argument("--mode", choices=("hybrid", "vector", "keyword"), default="hybrid")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--source", action="append")
    p.add_argument("--category", action="append")
    p.add_argument("--role", action="append")
    p.add_argument("--since", help="ISO date; only items first seen on or after it")
    p.add_argument("--docket")
    p.add_argument("--ld", type=int)
    p.add_argument("--kind", choices=("item", "page-change"))
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("check", help="health checks; exit 0 ok, 1 warning, 2 critical")
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true", help="print only checks that are not OK (nothing when healthy)")
    p.add_argument("--sync-warn-hours", type=float, default=13)
    p.add_argument("--sync-crit-hours", type=float, default=48)
    p.add_argument("--build-warn-hours", type=float, default=18)
    p.add_argument("--build-crit-hours", type=float, default=72)
    p.add_argument("--quiet-days", type=int, default=7)

    p = sub.add_parser("watch", help="report items newly matching the saved searches in a YAML file")
    p.add_argument("watchlist", type=Path)
    p.add_argument("--baseline", action="store_true", help="record current matches without reporting them")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("serve", help="local search page and JSON API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)

    sub.add_parser("stats", help="what the cache holds")
    sub.add_parser("repair", help="rebuild the keyword index from the stored items")

    args = ap.parse_args(argv)
    embed_args = (args.embed_backend, args.embed_model, args.embed_url)

    if args.command == "serve":
        serve(args.db, embed_args, args.host, args.port)
        return 0

    con = connect(args.db)
    try:
        if args.command == "sync":
            origin = DirOrigin(args.from_dir) if args.from_dir else HttpOrigin(args.base_url)
            stats = sync(con, origin, live_only=args.live_only)
            if args.corpus:
                merged = import_corpus(con, args.corpus)
                print(f"corpus: {merged['items_new']} new item(s), {merged['items_updated']} updated")
            if not args.no_embed:
                embedder = make_embedder(con, *embed_args)
                if args.embed_url and embedder.identity.startswith("http:"):
                    meta_set(con, "embed_url", args.embed_url)
                print(f"embed: {embed_pending(con, embedder)} passage(s) embedded with {embedder.identity}")
            return 1 if stats["errors"] else 0

        if args.command == "embed":
            if args.reindex and args.embed_backend == "auto":
                con.execute("DELETE FROM meta WHERE key = 'embed_model'")
            embedder = make_embedder(con, *embed_args)
            if args.embed_url and embedder.identity.startswith("http:"):
                meta_set(con, "embed_url", args.embed_url)
            print(f"embed: {embed_pending(con, embedder, reindex=args.reindex)} passage(s) embedded with {embedder.identity}")
            return 0

        if args.command == "search":
            filters = {
                "source": args.source, "category": args.category, "role": args.role, "since": args.since,
                "docket": args.docket, "ld": args.ld, "kind": args.kind,
            }
            embedder = None if args.mode == "keyword" else make_embedder(con, *embed_args)
            results = search(con, embedder, args.query, args.mode, filters, args.limit)
            if args.json:
                print(json.dumps(results, indent=2, ensure_ascii=False))
            else:
                _print_results(results)
            return 0

        if args.command == "check":
            results = run_checks(
                con,
                {
                    "sync_warn_hours": args.sync_warn_hours, "sync_crit_hours": args.sync_crit_hours,
                    "build_warn_hours": args.build_warn_hours, "build_crit_hours": args.build_crit_hours,
                    "quiet_days": args.quiet_days,
                },
            )
            worst = max(r["level"] for r in results)
            if args.json:
                print(json.dumps({"status": LEVEL_NAMES[worst], "checks": results}, indent=2))
            else:
                for r in results:
                    if r["level"] or not args.quiet:
                        print(f"[{r['status']:<8}] {r['name']}: {r['detail']}")
                if worst or not args.quiet:
                    print(f"Overall: {LEVEL_NAMES[worst]}")
            return worst

        if args.command == "watch":
            import yaml

            config = yaml.safe_load(args.watchlist.read_text(encoding="utf-8")) or {}
            watches = config.get("watches") or []
            needs_vectors = any(w.get("query") for w in watches)
            embedder = make_embedder(con, *embed_args) if needs_vectors else None
            report = run_watchlist(con, embedder, watches, baseline=args.baseline)
            if args.json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                for block in report:
                    print(f"== {block['watch']}: {len(block['hits'])} new ==")
                    for hit in block["hits"]:
                        when = (hit["date_published"] or hit["first_seen"] or "")[:10]
                        print(f"  {when}  {hit['title']}\n              {hit['source_name']}  {hit['url']}")
            return 0

        if args.command == "stats":
            items = con.execute("SELECT count(*) FROM items").fetchone()[0]
            print(f"{items} item(s) from {con.execute('SELECT count(DISTINCT source_id) FROM items').fetchone()[0]} source(s)")
            print(f"embedding model: {meta_get(con, 'embed_model') or 'none yet'}; "
                  f"{con.execute('SELECT count(*) FROM vectors').fetchone()[0]} vector(s), "
                  f"{con.execute('SELECT count(*) FROM chunks').fetchone()[0]} passage(s)")
            for row in con.execute(
                "SELECT category, count(*) AS n, max(COALESCE(first_seen, date_published)) AS newest "
                "FROM items GROUP BY category ORDER BY n DESC"
            ):
                print(f"  {row['n']:>6}  {row['category'] or '(none)':<42} newest {(row['newest'] or '')[:10]}")
            return 0

        if args.command == "repair":
            con.execute("INSERT INTO items_fts(items_fts) VALUES ('rebuild')")
            con.commit()
            print("keyword index rebuilt")
            return 0
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
