"""Shared helpers for the Maine Government Feeds build/validate scripts."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}


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


def load_state(source_id: str) -> dict:
    p = state_path(source_id)
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "id": source_id,
        "last_checked": None,
        "last_success": None,
        "last_failure": None,
        "last_status": None,
        "last_error": None,
        "content_hash": None,
        "consecutive_failures": 0,
        "items": [],
    }


def save_state(source_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(state_path(source_id), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


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


def fetch(url: str, client: httpx.Client) -> FetchResult:
    if not robots_allowed(url, client):
        return FetchResult(ok=False, error="blocked by robots.txt")

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = client.get(url, timeout=REQUEST_TIMEOUT, follow_redirects=True)
            if resp.status_code < 400:
                return FetchResult(
                    ok=True,
                    status_code=resp.status_code,
                    text=resp.text,
                    final_url=str(resp.url),
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
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF_SECONDS * attempt)
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
