"""Shared helpers for the Maine Government Feeds build/validate scripts."""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml

REPO_URL = "https://github.com/bedardandy/maine-government-feeds"
USER_AGENT = (
    "MaineGovernmentFeedsBot/1.0 "
    f"(+{REPO_URL}; static GitHub Pages monitor; contact via GitHub Issues)"
)
SITE_BASE_URL = os.environ.get(
    "SITE_BASE_URL", "https://bedardandy.github.io/maine-government-feeds"
)

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT_DIR / "sources.yml"
STATE_DIR = ROOT_DIR / "data" / "state"
DOCS_DIR = ROOT_DIR / "docs"
FEEDS_DIR = DOCS_DIR / "feeds"
OPML_DIR = DOCS_DIR / "opml"

MAX_ITEMS_PER_FEED = 50
REQUEST_TIMEOUT = 25.0
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0

# Minimum spacing between requests to the same host. Several Maine county
# sites sit behind WAFs that started returning HTTP 403 to rapid sequential
# fetches; spacing same-host requests out is the polite fix. Override with
# FEED_HOST_DELAY (seconds; 0 disables).
HOST_DELAY_SECONDS = float(os.environ.get("FEED_HOST_DELAY", "2.0"))
# Never sleep longer than this when honoring a server's Retry-After, so a
# misbehaving server cannot stall an unattended build.
RETRY_AFTER_CAP_SECONDS = 60.0

_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
_last_request_at: dict[str, float] = {}


def polite_wait(url: str) -> None:
    """Sleep long enough to space consecutive requests to one host."""
    netloc = urlparse(url).netloc
    if not netloc or HOST_DELAY_SECONDS <= 0:
        return
    last = _last_request_at.get(netloc)
    now = time.monotonic()
    if last is not None:
        remaining = HOST_DELAY_SECONDS - (now - last)
        if remaining > 0:
            time.sleep(remaining + random.uniform(0, HOST_DELAY_SECONDS * 0.25))
    _last_request_at[netloc] = time.monotonic()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def load_sources() -> list[dict]:
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    sources = data.get("sources", [])
    seen_ids = set()
    for s in sources:
        if "id" not in s or "url" not in s or "name" not in s:
            raise ValueError(f"Source missing required field(s): {s}")
        if s["id"] in seen_ids:
            raise ValueError(f"Duplicate source id: {s['id']}")
        seen_ids.add(s["id"])
        s.setdefault("enabled", True)
        s.setdefault("type", "page_monitor")
        s.setdefault("notes", "")
    return sources


def enabled_sources(sources: list[dict]) -> list[dict]:
    return [s for s in sources if s.get("enabled", True)]


def state_path(source_id: str) -> Path:
    return STATE_DIR / f"{source_id}.json"


# Volatile per-run health bookkeeping. These fields change on EVERY build
# (last_checked especially), which used to rewrite all ~105 per-source state
# files each run and bury git history in timestamp churn. They now live in one
# combined snapshot file instead; load_state()/save_state() merge them back in
# so callers keep the same dict shape as before.
HEALTH_SNAPSHOT_NAME = "_health.json"
HEALTH_KEYS = (
    "last_checked",
    "last_success",
    "last_failure",
    "last_status",
    "last_error",
    "consecutive_failures",
)

_health_store: dict[str, dict] | None = None


def _load_health_store() -> dict[str, dict]:
    global _health_store
    if _health_store is None:
        p = STATE_DIR / HEALTH_SNAPSHOT_NAME
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                _health_store = {k: v for k, v in data.items() if isinstance(v, dict)}
            except (json.JSONDecodeError, OSError):
                _health_store = {}
        else:
            _health_store = {}
    return _health_store


def load_state(source_id: str) -> dict:
    p = state_path(source_id)
    state = {
        "id": source_id,
        "content_hash": None,
        "items": [],
    }
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                state.update(loaded)
        except (json.JSONDecodeError, OSError):
            pass
    # Health fields from the combined snapshot win over any legacy inline
    # copies still present in older per-source files.
    entry = _load_health_store().get(source_id)
    if entry:
        for k in HEALTH_KEYS:
            if k in entry:
                state[k] = entry[k]
    return state


def save_state(source_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    # Split out volatile health fields into the combined snapshot; only write
    # it when an entry actually changed so uneventful runs leave git alone.
    store = _load_health_store()
    entry = store.get(source_id) or {}
    health_changed = False
    for k in HEALTH_KEYS:
        val = state.get(k)
        if entry.get(k) != val:
            health_changed = True
        entry[k] = val
    store[source_id] = entry
    snapshot_path = STATE_DIR / HEALTH_SNAPSHOT_NAME
    if health_changed or not snapshot_path.exists():
        with open(snapshot_path, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")

    # The per-source file carries only durable content (items, fingerprints,
    # validators, classification tags). Skip the write when the serialized
    # payload is byte-identical to what's already on disk — that keeps an
    # uneventful run from producing 100+ no-op diffs in the repo.
    payload = {k: v for k, v in state.items() if k not in HEALTH_KEYS}
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    p = state_path(source_id)
    try:
        current = p.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != serialized:
        with open(p, "w", encoding="utf-8") as f:
            f.write(serialized)


def iter_source_state_paths():
    """Yield per-source state file paths, excluding internal bookkeeping files
    whose names begin with '_' (e.g. the combined health snapshot)."""
    for path in sorted(STATE_DIR.glob("*.json")):
        if not path.name.startswith("_"):
            yield path


def robots_allowed(url: str, client: httpx.Client) -> bool:
    parsed = urlparse(url)
    domain = f"{parsed.scheme}://{parsed.netloc}"
    if domain not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        robots_url = urljoin(domain, "/robots.txt")
        try:
            resp = client.get(robots_url, timeout=10.0)
            if resp.status_code == 200:
                rp.parse(resp.text.splitlines())
            else:
                # No robots.txt or inaccessible: treat as allow-all.
                rp.parse([])
        except httpx.HTTPError:
            rp.parse([])
        _robots_cache[domain] = rp
    rp = _robots_cache[domain]
    try:
        return rp.can_fetch(USER_AGENT, url)
    except Exception:
        return True


@dataclass
class FetchResult:
    ok: bool
    status_code: int | None = None
    text: str | None = None
    error: str | None = None
    final_url: str | None = None
    # True when the server answered a conditional request with 304 Not
    # Modified: the caller should reuse its previously stored body.
    not_modified: bool = False
    # HTTP validators from the response, to be persisted in state and passed
    # back into fetch() on the next run.
    etag: str | None = None
    last_modified: str | None = None


def _conditional_headers(etag: str | None, last_modified: str | None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    return headers


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Server-advertised wait in seconds (delta-seconds or HTTP-date form),
    capped so a misbehaving server can't stall the build."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, min(float(raw), RETRY_AFTER_CAP_SECONDS))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        delta = dt.timestamp() - time.time()
        return max(0.0, min(delta, RETRY_AFTER_CAP_SECONDS))
    except (TypeError, ValueError, OverflowError):
        return None


def fetch(
    url: str,
    client: httpx.Client,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResult:
    """GET a URL politely.

    Honors robots.txt, spaces same-host requests (polite_wait), sends HTTP
    conditional validators when given (a 304 comes back as ok=True with
    not_modified=True and no body), and retries transient failures with
    server-advertised Retry-After support."""
    if not robots_allowed(url, client):
        return FetchResult(ok=False, error="blocked by robots.txt")

    headers = _conditional_headers(etag, last_modified)
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        polite_wait(url)
        try:
            resp = client.get(
                url, timeout=REQUEST_TIMEOUT, follow_redirects=True, headers=headers
            )
            if resp.status_code == 304 and headers:
                return FetchResult(ok=True, status_code=304, not_modified=True, final_url=url)
            if resp.status_code < 400:
                return FetchResult(
                    ok=True,
                    status_code=resp.status_code,
                    text=resp.text,
                    final_url=str(resp.url),
                    etag=resp.headers.get("ETag"),
                    last_modified=resp.headers.get("Last-Modified"),
                )
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code in (403, 404, 410):
                # Not worth retrying on these.
                return FetchResult(
                    ok=False,
                    status_code=resp.status_code,
                    error=last_error,
                    final_url=str(resp.url),
                )
            retry_after = (
                _retry_after_seconds(resp) if resp.status_code in (429, 503) else None
            )
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            retry_after = None
        if attempt < MAX_RETRIES:
            time.sleep(retry_after if retry_after is not None else RETRY_BACKOFF_SECONDS * attempt)
    return FetchResult(ok=False, error=last_error)


def fetch_post(url: str, data: dict, client: httpx.Client) -> FetchResult:
    """POST a form to an endpoint (used for JSON APIs that ignore GET
    parameters, like the Legislature's hearings schedule). Same politeness,
    robots.txt check and retry policy as fetch()."""
    if not robots_allowed(url, client):
        return FetchResult(ok=False, error="blocked by robots.txt")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        polite_wait(url)
        try:
            resp = client.post(url, data=data, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            if resp.status_code < 400:
                return FetchResult(
                    ok=True,
                    status_code=resp.status_code,
                    text=resp.text,
                    final_url=str(resp.url),
                )
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code in (403, 404, 410):
                return FetchResult(
                    ok=False,
                    status_code=resp.status_code,
                    error=last_error,
                    final_url=str(resp.url),
                )
            retry_after = (
                _retry_after_seconds(resp) if resp.status_code in (429, 503) else None
            )
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            retry_after = None
        if attempt < MAX_RETRIES:
            time.sleep(retry_after if retry_after is not None else RETRY_BACKOFF_SECONDS * attempt)
    return FetchResult(ok=False, error=last_error)


def make_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT})


def item_id(link: str, title: str) -> str:
    h = hashlib.sha256(f"{link}|{title}".encode("utf-8")).hexdigest()
    return h[:16]


def text_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def safe_filename(source_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", source_id)


def scotus_ot_term() -> int:
    """Return the two-digit U.S. Supreme Court October Term year.

    "October Term N" (URLs use e.g. /opinions/slipopinion/25 for OT2025)
    begins the first Monday in October of calendar year 2000+N and runs
    until the next term's first Monday in October, so the two-digit term
    number stays the same across the new-year rollover.
    """
    today = now_utc().date()
    oct1 = datetime(today.year, 10, 1, tzinfo=timezone.utc).date()
    first_monday = oct1 + timedelta(days=(7 - oct1.weekday()) % 7)
    term_year = today.year if today >= first_monday else today.year - 1
    return term_year - 2000


def wcb_decision_year() -> int:
    """Current calendar year, used for the WCB Appellate Division's yearly
    decisions page (e.g. .../appellate/2026decisions.html). The Board opens
    a new year's page once its first decision of the year is issued, so
    there's a brief window early in January where this may 404 until then."""
    return now_utc().year


def resolve_url(url: str) -> str:
    """Substitute date-computed placeholders (see scotus_ot_term, wcb_decision_year) in a source URL."""
    if "{scotus_ot_term}" in url:
        url = url.replace("{scotus_ot_term}", str(scotus_ot_term()))
    if "{wcb_decision_year}" in url:
        url = url.replace("{wcb_decision_year}", str(wcb_decision_year()))
    return url
