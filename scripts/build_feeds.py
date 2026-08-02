#!/usr/bin/env python3
"""
Build RSS, Atom, and JSON feeds (plus OPML and the docs/ site) for all
sources configured in sources.yml.

This script is designed to run unattended in GitHub Actions every few
hours. It never raises on an individual source failure (a government site
being briefly down is normal); it records the failure in that source's
state file and moves on. See validate_feeds.py for the post-build checks
that decide whether the overall build should fail CI.
"""
from __future__ import annotations

import csv
import difflib
import hashlib
import html
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import re

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from feedgen.feed import FeedGenerator

import common
from common import (
    fetch_post,
    DOCS_DIR,
    FEEDS_DIR,
    MAX_ITEMS_PER_FEED,
    OPML_DIR,
    SITE_BASE_URL,
    enabled_sources,
    fetch,
    iso,
    item_id,
    load_sources,
    load_state,
    make_client,
    now_utc,
    resolve_url,
    safe_filename,
    save_state,
    text_fingerprint,
)


# A leading feed-body line is "chrome" (template junk maine.gov's Drupal
# feeds prepend before the real content) if it's a bare date, a
# "Day, MM/DD/YYYY - HH:MM" byline timestamp, or an author username/email
# fragment. We drop such lines only while they lead the body; once real
# prose starts we stop, so mid-article dates are never touched.
_CHROME_LINE_RES = [
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$"),
    re.compile(r"^[A-Za-z]{3,9},?\s+\d{1,2}/\d{1,2}/\d{2,4}\s*-\s*\d{1,2}:\d{2}$"),
    re.compile(r"^\S+@\S*$"),           # jules.m.olson@… , foo@maine.gov
    re.compile(r"^[a-z0-9._-]+…$"),      # truncated username like ag-timswan…
    re.compile(r"^[a-z0-9]+-[a-z0-9]+$"),  # bare Drupal author slug e.g. ag-timswan
]


def _is_chrome_line(line: str, title: str) -> bool:
    if not line:
        return True
    if title and line == title:
        return True
    return any(rx.match(line) for rx in _CHROME_LINE_RES)


def clean_entry_body(raw_html: str, title: str) -> tuple[str, str]:
    """Return (plain_text, clean_html) for a feed entry body.

    Strips the Drupal template chrome maine.gov feeds prepend (a repeated
    title, a bare date, an author username/email, a byline timestamp)
    before the real content. Feeds that are already clean pass through
    essentially unchanged.
    """
    if not raw_html:
        return "", ""
    try:
        soup = BeautifulSoup(raw_html, "lxml")
    except Exception:
        soup = BeautifulSoup(raw_html, "html.parser")

    # <time> elements are pure date chrome (the item already carries a real
    # pubDate); remove them so they can't lead the HTML body.
    for t in soup.find_all("time"):
        t.decompose()
    # Author <span> wrappers whose whole text is a username/email fragment.
    for sp in soup.find_all("span"):
        txt = sp.get_text(" ", strip=True)
        if txt and any(
            rx.match(txt) for rx in _CHROME_LINE_RES[2:]
        ) and not sp.find(["p", "div", "a", "strong"]):
            sp.decompose()
    # Drop wrapper tags left empty after removing the chrome above (e.g. the
    # <p> and <span> that had only held a <time> or author name).
    for tag in soup.find_all(["p", "span", "div"]):
        if not tag.get_text(strip=True) and not tag.find(["img", "a", "br"]):
            tag.decompose()

    # Plain text: drop leading chrome lines until real prose begins.
    lines = [ln.strip() for ln in soup.get_text("\n").split("\n")]
    i = 0
    while i < len(lines) and _is_chrome_line(lines[i], title):
        i += 1
    plain = "\n".join(ln for ln in lines[i:] if ln).strip()

    # HTML: drop a leading title-only block, then serialize the body.
    body = soup.body or soup
    for child in list(body.children):
        text = child.get_text(strip=True) if hasattr(child, "get_text") else str(child).strip()
        if not text:
            if hasattr(child, "extract"):
                child.extract()
            continue
        if title and text == title:
            child.extract()
        break
    clean_html = (body.decode_contents() if hasattr(body, "decode_contents") else str(body)).strip()
    return plain, clean_html


def parse_native_rss(text: str, source_url: str) -> list[dict] | None:
    parsed = feedparser.parse(text)
    if parsed.bozo and not parsed.entries:
        return None
    items = []
    for entry in parsed.entries:
        link = entry.get("link") or source_url
        title = html.unescape((entry.get("title") or "Untitled").strip())
        raw_body = html.unescape((entry.get("summary") or entry.get("description") or "").strip())
        summary, summary_html = clean_entry_body(raw_body, title)
        published_dt = None
        for key in ("published_parsed", "updated_parsed"):
            if entry.get(key):
                try:
                    published_dt = datetime(*entry[key][:6], tzinfo=None)
                except (TypeError, ValueError):
                    published_dt = None
                break
        items.append(
            {
                "id": item_id(link, title),
                "title": title,
                "link": link,
                "summary": summary[:2000],
                "summary_html": summary_html[:8000],
                "published": iso(published_dt) if published_dt else None,
            }
        )
    return items


# Leading date prefix in a title, e.g. "7/24/2026: eFiling rollout..." or
# "July 24, 2026 - Notice...". Used only when a source opts in with
# `date_from_title: true` and has no (or a failed) date selector.
_TITLE_DATE_RE = re.compile(
    r"^\s*((?:\d{1,2}/\d{1,2}/\d{2,4})|(?:\d{4}-\d{2}-\d{2})|"
    r"(?:[A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4}))\s*[:\-–—]?\s*"
)


def _parse_item_date(date_text: str) -> datetime | None:
    """Fuzzy-parse a scraped date string, rejecting implausible results.

    Future dates beyond a small grace window are rejected: pages like the
    MRS rulemaking table put comment deadlines / effective dates in the
    date column, and stamping an item weeks into the future pins it to the
    top of the feed until that date passes."""
    try:
        candidate = dateutil_parser.parse(date_text, fuzzy=True)
    except (ValueError, OverflowError):
        return None
    if not (1776 <= candidate.year <= now_utc().year + 1):
        return None
    if candidate.replace(tzinfo=timezone.utc) > now_utc() + timedelta(days=2):
        return None
    return candidate


def parse_html_selectors(
    text: str, base_url: str, selectors: dict, date_from_title: bool = False
) -> list[dict] | None:
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:
        soup = BeautifulSoup(text, "html.parser")

    item_sel = selectors.get("item")
    if not item_sel:
        return None
    nodes = soup.select(item_sel)
    if not nodes:
        return None

    title_sel = selectors.get("title")
    link_sel = selectors.get("link")
    date_sel = selectors.get("date")

    items = []
    for node in nodes:
        title_node = node.select_one(title_sel) if title_sel else node
        link_node = node.select_one(link_sel) if link_sel else node

        title = title_node.get_text(" ", strip=True) if title_node else ""
        href = None
        if link_node is not None:
            href = link_node.get("href")
            if not href and link_node.name != "a":
                a = link_node.find("a")
                if a:
                    href = a.get("href")
        if not href and node.name == "a":
            href = node.get("href")
        if not href:
            a = node.find("a")
            if a:
                href = a.get("href")

        if not title or not href:
            continue

        # Normalize before hashing into the item id: raw spaces in an href
        # are invalid in a URL, and upstream sites flipping between " " and
        # "%20" variants would otherwise mint duplicate items (observed on
        # gov-boards-commissions).
        link = urljoin(base_url, href.strip()).replace(" ", "%20")

        published_dt = None
        if date_sel:
            date_node = node.select_one(date_sel)
            if date_node:
                published_dt = _parse_item_date(date_node.get_text(strip=True))
        if published_dt is None and date_from_title:
            m = _TITLE_DATE_RE.match(title)
            if m:
                published_dt = _parse_item_date(m.group(1))

        items.append(
            {
                "id": item_id(link, title),
                "title": title,
                "link": link,
                "summary": "",
                "published": iso(published_dt) if published_dt else None,
            }
        )

    # De-duplicate while preserving order (some selectors match nested nodes twice).
    seen = set()
    deduped = []
    for it in items:
        if it["id"] in seen:
            continue
        seen.add(it["id"])
        deduped.append(it)
    return deduped


ME_LEG_HEARINGS_ENDPOINT = "https://legislature.maine.gov/bills/getPHWSForDate.asp"
# How far back/forward the hearings window reaches from today. Looking back
# keeps just-held events in the feed/calendar; looking ahead captures
# newly scheduled ones.
ME_LEG_HEARINGS_LOOKBACK_DAYS = 7
ME_LEG_HEARINGS_WINDOW_DAYS = 45


def fetch_me_leg_hearings(source: dict, client) -> tuple[list[dict] | None, str | None]:
    """Fetch Maine Legislature public hearings / work sessions.

    The Legislature's schedule pages are client-rendered; the underlying
    endpoint only answers structured queries via POST (a GET returns a
    stale dump of 1990s sessions). Returns (items, error). An empty item
    list is normal between sessions, so it is NOT treated as a parse
    failure by the caller.
    """
    start = now_utc() - timedelta(days=ME_LEG_HEARINGS_LOOKBACK_DAYS)
    result = fetch_post(
        ME_LEG_HEARINGS_ENDPOINT,
        {
            "yr": str(start.year),
            "mo": f"{start.month:02d}",
            "dy": f"{start.day:02d}",
            "days": str(ME_LEG_HEARINGS_WINDOW_DAYS),
            "code": "ALL",
            "mode": "D",
        },
        client,
    )
    if not result.ok:
        return None, result.error
    try:
        rows = json.loads(result.text)
    except json.JSONDecodeError as exc:
        return None, f"hearings endpoint returned non-JSON: {exc}"

    items = []
    for r in rows:
        paper = (r.get("paperNumber") or "").strip()
        bill_title = (r.get("title") or "").strip()
        committee = (r.get("committeeName") or "").strip()
        location = (r.get("publicHearingLocation") or "").strip()
        session = r.get("sessionNumber")
        # The page's own JS treats this field as a boolean: -1 (truthy) is a
        # public hearing, 0 a work session.
        kind = "Public Hearing" if r.get("publicHearing") else "Work Session"

        event_dt = None
        raw_when = (r.get("hearingDate") or "").strip()
        if raw_when:
            try:
                # Format: 'Tue Mar 17 13:00:00 EDT 2026'. dateutil ignores the
                # unknown tz abbreviation; times are Maine-local wall clock.
                event_dt = dateutil_parser.parse(raw_when, ignoretz=True)
            except (ValueError, OverflowError):
                event_dt = None
        if event_dt is None or event_dt.year < 2000:
            continue  # skip the endpoint's 1900-01-01 placeholder rows

        link = (
            f"https://legislature.maine.gov/bills/display_ps.asp?paper={paper}&snum={session}"
            if paper and session
            else source["url"]
        )
        when_text = event_dt.strftime("%a %b %d, %Y %I:%M %p").replace(" 0", " ")
        title = f"{kind}: LD {r.get('ld')} ({paper}) — {committee} — {when_text}"
        summary_bits = [bill_title]
        if location:
            summary_bits.append(f"Location: {location}")
        summary_bits.append(f"Committee: {committee}")
        sponsor = " ".join(
            str(r.get(k) or "").strip()
            for k in ("sponsorPosition", "sponsorFirstName", "sponsorLastName", "sponsorFrom")
        ).strip()
        if sponsor:
            summary_bits.append(f"Sponsor: {sponsor}")

        items.append(
            {
                # One event per bill+kind+datetime, so reschedules appear as new items.
                "id": item_id(link, f"{kind}|{event_dt.isoformat()}"),
                "title": title,
                "link": link,
                "summary": "\n".join(b for b in summary_bits if b)[:2000],
                "published": iso(event_dt),
                "event_start": iso(event_dt),
                "event_location": location,
            }
        )
    return items, None


# --------------------------------------------------------------------------- #
# Item content enrichment: fetch the page (or PDF) a NEW item links to, once,
# and store its main text as the item body — so subscribers see the substance
# of an opinion/notice instead of a bare title+link. Politeness constraints:
# robots.txt is honored (same check as all fetching), the descriptive
# User-Agent is sent, each item is fetched at most once ever (the extracted
# body persists in state), fetches are capped per source per run, and a
# failure just leaves the item body empty — it never marks the source failing.
# --------------------------------------------------------------------------- #
MAX_ENRICH_FETCHES_PER_SOURCE = 10
MAX_ENRICH_PDF_BYTES = 4 * 1024 * 1024
MAX_ENRICH_PDF_PAGES = 4
ENRICH_SUMMARY_CHARS = 2000
ENRICH_HTML_CHARS = 8000

# File extensions we know how to extract text from. Anything else (docx,
# xlsx, images, ...) is left as a bare link.
_PDF_RE = re.compile(r"\.pdf($|\?)", re.IGNORECASE)
_SKIP_EXT_RE = re.compile(r"\.(docx?|xlsx?|pptx?|zip|jpe?g|png|gif|ics|mp3|mp4)($|\?)", re.IGNORECASE)


def _extract_html_body(text: str) -> tuple[str, str]:
    """(plain_text, main_html) of an article page's main content region."""
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:
        soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    plain = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True)).strip()
    html_body = main.decode_contents().strip() if hasattr(main, "decode_contents") else ""
    return plain, html_body


def _extract_pdf_body(data: bytes) -> str:
    try:
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages[:MAX_ENRICH_PDF_PAGES]:
            pages.append(page.extract_text() or "")
        return re.sub(r"\n{3,}", "\n\n", "\n".join(pages)).strip()
    except Exception:
        return ""


def fetch_item_body(url: str, client) -> tuple[str, str] | None:
    """Fetch one linked document and return (plain_text, html_body), or None.

    Single attempt, no retries — enrichment is best-effort garnish and must
    not slow the build down when a document host is struggling. Split out as
    a module-level function so tests can stub it.
    """
    if client is None:
        return None
    if not common.robots_allowed(url, client):
        return None
    try:
        resp = client.get(url, timeout=common.REQUEST_TIMEOUT, follow_redirects=True)
    except Exception:
        return None
    if resp.status_code >= 400:
        return None
    if _PDF_RE.search(url) or "application/pdf" in resp.headers.get("content-type", ""):
        if len(resp.content) > MAX_ENRICH_PDF_BYTES:
            return None
        plain = _extract_pdf_body(resp.content)
        return (plain, "") if plain else None
    if looks_like_challenge_page(resp.text or ""):
        return None
    plain, html_body = _extract_html_body(resp.text)
    return (plain, html_body) if plain else None


def enrich_new_items(source: dict, new_ids: set[str], items: list[dict], client) -> int:
    """Fill empty bodies of newly observed items by fetching what they link
    to. Returns the number of items enriched."""
    if source.get("enrich") is False or os.environ.get("FEED_ENRICH") == "0":
        return 0
    budget = MAX_ENRICH_FETCHES_PER_SOURCE
    enriched = 0
    for it in items:
        if budget <= 0:
            break
        if it["id"] not in new_ids or it.get("summary"):
            continue
        link = it.get("link") or ""
        if not link.startswith(("http://", "https://")) or _SKIP_EXT_RE.search(link):
            continue
        budget -= 1
        body = fetch_item_body(link, client)
        if not body:
            continue
        plain, html_body = body
        it["summary"] = plain[:ENRICH_SUMMARY_CHARS]
        if html_body and "<" in html_body:
            it["summary_html"] = html_body[:ENRICH_HTML_CHARS]
        enriched += 1
    return enriched


def apply_filters(items: list[dict], source: dict) -> list[dict]:
    """Filter parsed items by the source's optional `filters` block.

    Supported keys (all case-insensitive regexes, searched not matched):
    include_title / exclude_title / include_link / exclude_link.
    This lets several sources share one broad upstream feed or table while
    each publishes only its own slice (e.g. splitting the Governor's
    sitewide RSS into executive orders vs. press releases).
    """
    filters = source.get("filters") or {}
    if not filters:
        return items

    compiled = {}
    for key in ("include_title", "exclude_title", "include_link", "exclude_link"):
        pattern = filters.get(key)
        if pattern:
            compiled[key] = re.compile(pattern, re.IGNORECASE)

    kept = []
    for it in items:
        title, link = it.get("title") or "", it.get("link") or ""
        if "include_title" in compiled and not compiled["include_title"].search(title):
            continue
        if "exclude_title" in compiled and compiled["exclude_title"].search(title):
            continue
        if "include_link" in compiled and not compiled["include_link"].search(link):
            continue
        if "exclude_link" in compiled and compiled["exclude_link"].search(link):
            continue
        kept.append(it)
    return kept


# Bounds for the visible-text snapshot kept in state for diffing, and for
# the diff excerpt embedded in a "page changed" feed item.
MAX_PAGE_LINES_STORED = 3000
MAX_DIFF_EXCERPT_CHARS = 1500

# How many evicted-item provenance records to remember per source (see the
# `seen` map in build_source). Sized to comfortably exceed the largest
# upstream listing observed (~200 rows) so items cycling in and out of the
# 50-item window keep their original first_seen/published stamps.
MAX_SEEN_ITEMS = 1000

# Anti-bot / WAF interstitials are served with HTTP 200 and would otherwise
# be fingerprinted or parsed as page content, producing a guaranteed junk
# "Page updated" item now and another when the real page returns (observed
# on rod-franklin, probate-franklin, mbe-announcements).
_CHALLENGE_PAGE_RE = re.compile(
    r"(please wait while your request is being verified"
    r"|checking your browser before accessing"
    r"|verifying you are human"
    r"|enable javascript and cookies to continue"
    r"|<title>\s*just a moment)",
    re.IGNORECASE,
)


def looks_like_challenge_page(text: str) -> bool:
    return bool(_CHALLENGE_PAGE_RE.search(text[:40000]))


def page_monitor_item(source: dict, text: str, base_url: str) -> tuple[str, list[str]]:
    """Returns (fingerprint, visible_text_lines).

    The fingerprint must stay byte-identical to what earlier versions
    computed (whitespace-normalized full visible text), or every
    page-monitor source would emit a spurious "page changed" item on the
    first run after an algorithm change. The line list is kept separately,
    only for diff excerpts.
    """
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:
        soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    visible_text = main.get_text(" ", strip=True)
    fingerprint = text_fingerprint(visible_text)

    lines = []
    for raw in main.get_text("\n", strip=True).split("\n"):
        line = " ".join(raw.split())
        if line:
            lines.append(line)
    return fingerprint, lines[:MAX_PAGE_LINES_STORED]


def diff_excerpt(old_lines: list[str], new_lines: list[str]) -> str:
    """Human-readable added/removed summary between two page snapshots."""
    added, removed = [], []
    for line in difflib.unified_diff(old_lines, new_lines, n=0, lineterm=""):
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:].strip())
        elif line.startswith("-") and not line.startswith("---"):
            removed.append(line[1:].strip())
    parts = []
    if added:
        parts.append("Added:\n" + "\n".join(f"  + {l}" for l in added))
    if removed:
        parts.append("Removed:\n" + "\n".join(f"  - {l}" for l in removed))
    excerpt = "\n".join(parts)
    if len(excerpt) > MAX_DIFF_EXCERPT_CHARS:
        excerpt = excerpt[:MAX_DIFF_EXCERPT_CHARS] + "\n[diff truncated]"
    return excerpt


def build_source(source: dict, client) -> tuple[dict, list[dict], str | None]:
    """Returns (updated_state, current_items, status_note)."""
    sid = source["id"]
    state = load_state(sid)
    state["last_checked"] = iso(now_utc())

    source_type = source.get("type", "page_monitor")
    note = None

    if source_type == "me_leg_hearings":
        new_items, error = fetch_me_leg_hearings(source, client)
        if new_items is None:
            state["last_failure"] = iso(now_utc())
            state["last_status"] = None
            state["last_error"] = error
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(sid, state)
            return state, state.get("items", []), error
        state["last_success"] = iso(now_utc())
        state["last_status"] = 200
        state["last_error"] = None
        state["consecutive_failures"] = 0
        if not new_items:
            # A successful but empty response means nothing is scheduled in the
            # look-ahead window (normal between sessions). Treat hearings as a
            # live schedule snapshot and clear stored items, so the feed reports
            # zero rather than republishing past or cancelled sessions
            # indefinitely. Provenance of past hearings lives in the git history
            # and Wayback snapshots, not the live calendar.
            note = "no hearings/work sessions scheduled in the current window"
            state["items"] = []
            save_state(sid, state)
            return state, [], note
        result = None
    else:
        fetch_url = source.get("rss_url") if source_type == "native_rss" else source["url"]
        result = fetch(fetch_url, client)

        if not result.ok:
            state["last_failure"] = iso(now_utc())
            state["last_status"] = result.status_code
            state["last_error"] = result.error
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(sid, state)
            return state, state.get("items", []), result.error

        if source_type in ("html", "page_monitor") and looks_like_challenge_page(result.text or ""):
            error = "anti-bot challenge/interstitial page served instead of content"
            state["last_failure"] = iso(now_utc())
            state["last_status"] = result.status_code
            state["last_error"] = error
            state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
            save_state(sid, state)
            return state, state.get("items", []), error

        state["last_success"] = iso(now_utc())
        state["last_status"] = result.status_code
        state["last_error"] = None
        state["consecutive_failures"] = 0

        new_items = None
        if source_type == "native_rss":
            new_items = parse_native_rss(result.text, source["url"])
        elif source_type == "html":
            selectors = source.get("selectors", {})
            new_items = parse_html_selectors(
                result.text,
                result.final_url or source["url"],
                selectors,
                date_from_title=bool(source.get("date_from_title")),
            )

        # Whether the parser itself found any items, measured BEFORE filtering.
        # A filtered source that parsed fine but matched nothing this run is a
        # valid (currently-empty) filtered feed, not a broken one — it must go
        # through the merge/filter path below so stale out-of-scope items are
        # purged, rather than falling back to page-change monitoring.
        parser_found_items = bool(new_items)
        if source_type in ("native_rss", "html") and new_items:
            new_items = apply_filters(new_items, source)
        if source_type in ("native_rss", "html") and not parser_found_items:
            note = "selectors/parser returned no items; falling back to page-change monitoring"
            source_type = "page_monitor"

    existing_items = state.get("items", [])

    if source_type == "page_monitor":
        # Drop any prior synthetic baseline placeholder; real items supersede it.
        baseline_id = item_id(source["url"], "baseline-monitoring")
        existing_items = [it for it in existing_items if it.get("id") != baseline_id]
        fingerprint, page_lines = page_monitor_item(source, result.text, source["url"])
        previous_hash = state.get("content_hash")
        previous_lines = state.get("page_lines") or []
        merged = existing_items
        if previous_hash is None:
            # First observation: establish the baseline silently.
            state["content_hash"] = fingerprint
            state["page_lines"] = page_lines
            state.pop("pending_hash", None)
            note = note or "baseline established; no prior content to compare against"
        elif fingerprint == previous_hash:
            # Unchanged. Clear any unconfirmed change — the page flapped back
            # to its known content (rotating page variants, transient widget
            # states) and no item should be emitted for that.
            if state.pop("pending_hash", None) is not None:
                note = note or "unconfirmed page change reverted; no item emitted"
        elif fingerprint == state.get("pending_hash"):
            # The same new content was seen on two consecutive runs: a real,
            # stable change. Emit the item and advance the baseline.
            state["content_hash"] = fingerprint
            state["page_lines"] = page_lines
            state.pop("pending_hash", None)
            summary = (
                "This page's content changed since the last check. "
                "Visit the page directly to review the update."
            )
            excerpt = diff_excerpt(previous_lines, page_lines) if previous_lines else ""
            if excerpt:
                summary += "\n\nWhat changed (text excerpt):\n" + excerpt
            change_item = {
                "id": item_id(source["url"], f"page-changed-{state['last_success']}"),
                "first_seen": state["last_success"],
                "title": f"Page updated: {source['name']}",
                "link": source["url"],
                "summary": summary,
                "published": state["last_success"],
            }
            merged = [change_item] + existing_items
        else:
            # New content seen for the first time: hold it for confirmation on
            # the next run instead of emitting immediately. Alternating page
            # variants (server-side A/B chrome, bot checks that slipped past
            # detection) never confirm, so they never emit; a genuine change
            # is delayed by one build cycle, which the 6-hour cadence absorbs.
            state["pending_hash"] = fingerprint
            note = note or "page change observed; awaiting confirmation on next run"
        if not merged:
            # A page-monitor feed with no change item yet would be an empty
            # subscription in a reader. Emit one stable baseline item so the
            # feed reads as "monitoring, nothing changed yet" rather than
            # broken. Keyed on the URL alone, so it's added once and never
            # duplicated on later runs.
            since = state.get("first_monitored") or state["last_success"]
            state["first_monitored"] = since
            merged = [
                {
                    "id": item_id(source["url"], "baseline-monitoring"),
                    "first_seen": since,
                    "title": f"Monitoring started: {source['name']}",
                    "link": source["url"],
                    "summary": (
                        "This source is being monitored for changes. No update has been "
                        "detected yet; a new item will appear here the next time the page "
                        "changes. This placeholder confirms the feed is live."
                    ),
                    "published": since,
                }
            ]
    else:
        # This source produced real per-item entries this run: any leftover
        # page-monitor artifacts (a "Monitoring started" baseline or "Page
        # updated" change items from an earlier page_monitor life or a past
        # selector-failure fallback) are superseded — purge them and the
        # page-monitor bookkeeping keys so they don't linger in an item feed.
        existing_items = [
            it
            for it in existing_items
            if not (it.get("title") or "").startswith(("Page updated:", "Monitoring started:"))
        ]
        for key in ("content_hash", "page_lines", "pending_hash"):
            state.pop(key, None)

        # Merge new items with existing, de-duplicating by id, newest first.
        # `first_seen` (when this build first observed the item) is preserved
        # across runs and serves as a provenance marker independent of the
        # source page's own — often missing or unparseable — date.
        #
        # `seen` remembers the provenance stamps of every item id this source
        # has EVER emitted — including items evicted from the 50-item window.
        # Without it, an undated upstream listing longer than the window
        # re-stamps evicted rows with "now" whenever they rotate back in,
        # making the whole feed republish as new every run (observed on
        # jb-news and agency-bureau-insurance-bulletins).
        seen = state.get("seen") or {}
        existing_by_id = {it["id"]: it for it in existing_items}
        merged_map = {}
        new_ids: set[str] = set()
        for it in new_items:
            prior = existing_by_id.get(it["id"]) or seen.get(it["id"])
            if prior is None:
                new_ids.add(it["id"])
            # A re-parsed listing row has no body; keep the enriched body
            # fetched when the item was first observed instead of wiping it.
            if not it.get("summary") and prior and prior.get("summary"):
                it["summary"] = prior["summary"]
                if prior.get("summary_html"):
                    it["summary_html"] = prior["summary_html"]
            it["first_seen"] = (prior or {}).get("first_seen") or state["last_success"]
            if not it.get("published"):
                # Keep the timestamp assigned when the item was first seen;
                # re-stamping every run would make undated items float to
                # "now" forever and destroy their value as a publication marker.
                it["published"] = (prior or {}).get("published") or it["first_seen"]
            merged_map[it["id"]] = it
        for it in existing_items:
            merged_map.setdefault(it["id"], it)

        def _published_key(it: dict) -> float:
            try:
                return dateutil_parser.isoparse(it.get("published") or "").timestamp()
            except (ValueError, TypeError, OverflowError):
                return 0.0

        merged = sorted(merged_map.values(), key=_published_key, reverse=True)
        # Re-apply filters to the merged list so items retained in state from
        # before a filter was added (or tightened) are purged, not kept forever.
        merged = apply_filters(merged, source)

        # Record provenance for everything observed this run (pre-truncation),
        # newest insertions last; trim oldest entries beyond the cap.
        for it in merged:
            seen[it["id"]] = {
                "first_seen": it.get("first_seen"),
                "published": it.get("published"),
            }
        if len(seen) > MAX_SEEN_ITEMS:
            for stale_id in list(seen)[: len(seen) - MAX_SEEN_ITEMS]:
                del seen[stale_id]
        state["seen"] = seen

        # Fetch bodies for the newly observed items that made the published
        # window, so subscribers get the substance, not just a link.
        merged = merged[:MAX_ITEMS_PER_FEED]
        enriched = enrich_new_items(source, new_ids, merged, client)
        if enriched:
            note = note or f"fetched body text for {enriched} new item(s)"

    merged = merged[:MAX_ITEMS_PER_FEED]
    state["items"] = merged
    save_state(sid, state)
    return state, merged, note


def _base_description(source: dict) -> str:
    return source.get("notes") or f"Monitored updates for {source['name']} ({source['category']})."


def staleness_suffix(state: dict | None) -> str:
    """Return a human-readable staleness marker to append to a feed's
    description, or "" when the source is healthy.

    A subscriber reads only the feed itself — the separate status.html
    dashboard is invisible to them. When the source has been failing to
    fetch (``consecutive_failures > 0``), the feed is being regenerated from
    the last-good items with no fresh content; stamp that fact IN the feed so
    a reader can tell the source has gone stale rather than silently trusting
    weeks-old items as current.
    """
    if not state:
        return ""
    failures = state.get("consecutive_failures", 0) or 0
    if failures <= 0:
        return ""
    last_success = state.get("last_success")
    verified = last_success or "an unknown date (never successfully fetched)"
    plural = "s" if failures != 1 else ""
    return (
        f" [STALE: source last verified {verified}; "
        f"{failures} consecutive fetch failure{plural} since — "
        "items below may be out of date.]"
    )


def feed_description(source: dict, state: dict | None = None) -> str:
    """Feed channel/description, with an appended staleness marker when the
    source's most recent fetches have been failing."""
    return _base_description(source) + staleness_suffix(state)


# Public WebSub hub advertised in the Atom feeds; the build workflow pings it
# for changed feeds after each publish so push-capable readers (Inoreader,
# FreshRSS with the WebSub plugin) get new items in seconds rather than on
# their next poll.
WEBSUB_HUB = "https://pubsubhubbub.superfeedr.com/"

# Matches the 6-hour build cron: tells readers (notably classic Outlook) not
# to poll more often than the data can change.
FEED_TTL_MINUTES = 360


def _entry_pubdate(it: dict) -> datetime | None:
    """The pubDate an RSS/Atom entry ships with.

    Falls back to `_first_seen` when no publication date was parseable, so
    every entry carries a date (Power Automate's RSS trigger and RSS-to-email
    services key on pubDate and skip or misorder dateless items). Date-only
    timestamps (midnight) are deterministically offset by a per-item value so
    a twelve-opinion day doesn't produce twelve identical pubDates — Power
    Automate treats equal pubDates as already-seen and may deliver only one."""
    raw = it.get("published") or it.get("first_seen")
    if not raw:
        return None
    try:
        dt = dateutil_parser.isoparse(raw)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        try:
            dt += timedelta(seconds=int(it["id"], 16) % 43200)
        except (KeyError, ValueError, TypeError):
            pass
    return dt


def _item_tags(source: dict, it: dict) -> list[str]:
    """Category/tag terms for one item: source category, subcategory, and any
    practitioner-role tags the classifier assigned. These let consumers route
    items (Outlook rules, Power Automate conditions, DMS intake mapping)
    without regexing titles."""
    # Aggregate feeds carry each item's ORIGIN category/subcategory (stamped
    # as _agg_tags on the pooled copy) rather than the synthetic feed's own.
    if it.get("_agg_tags"):
        tags = list(it["_agg_tags"])
    else:
        tags = [source.get("category") or ""]
        if source.get("subcategory"):
            tags.append(source["subcategory"])
    tags.extend(f"role:{r}" for r in it.get("roles") or [])
    return [t for t in tags if t]


def write_rss_atom(
    source: dict, items: list[dict], state: dict | None = None, max_items: int = MAX_ITEMS_PER_FEED
) -> None:
    fg = FeedGenerator()
    fg.id(source["url"])
    fg.title(source["name"])
    feed_slug = safe_filename(source["id"])
    fg.link(href=f"{SITE_BASE_URL}/feeds/rss/{feed_slug}.xml", rel="self")
    fg.link(href=WEBSUB_HUB, rel="hub")
    # Set the alternate link LAST: feedgen's RSS <link> takes the most recent
    # link() call, and the channel link must point at the source page, not at
    # the feed itself.
    fg.link(href=source["url"], rel="alternate")
    fg.description(feed_description(source, state))
    fg.language("en-US")
    fg.ttl(FEED_TTL_MINUTES)
    if source.get("category"):
        fg.category([{"term": source["category"]}])
    fg.generator("maine-government-feeds (static GitHub Actions build)")

    for it in items[:max_items]:
        fe = fg.add_entry()
        fe.id(it["link"] + "#" + it["id"])
        fe.title(it["title"])
        fe.link(href=it["link"])
        tags = _item_tags(source, it)
        if tags:
            fe.category([{"term": t} for t in tags])
        summary_text = _plain_text(it.get("summary") or "")
        if summary_text:
            # description is plain-text; the richer HTML body (when we have a
            # cleaned one) goes in content:encoded so readers can render it.
            fe.description(summary_text)
            if it.get("summary_html"):
                fe.content(content=it["summary_html"], type="CDATA")
        if it["link"].lower().split("?")[0].endswith(".pdf"):
            # Length is unknown (we never fetch the documents themselves);
            # "0" is the accepted convention for unknown enclosure length.
            fe.enclosure(url=it["link"], length="0", type="application/pdf")
        pub = _entry_pubdate(it)
        if pub:
            try:
                fe.pubDate(pub)
            except Exception:
                pass

    (FEEDS_DIR / "rss").mkdir(parents=True, exist_ok=True)
    (FEEDS_DIR / "atom").mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(FEEDS_DIR / "rss" / f"{feed_slug}.xml"))
    fg.atom_file(str(FEEDS_DIR / "atom" / f"{feed_slug}.xml"))


def _plain_text(s: str) -> str:
    """Guarantee plain text: strip any residual HTML tags. A no-op for
    already-clean strings, this also heals items captured by older builds
    (before body-cleaning) that still carry raw HTML in state."""
    if not s or "<" not in s:
        return s
    try:
        return BeautifulSoup(s, "lxml").get_text(" ", strip=True)
    except Exception:
        return s


def _json_feed_item(it: dict, source: dict | None = None) -> dict:
    """One JSON Feed 1.1 item. Per the spec, content_text is plain text and
    HTML belongs in content_html; we emit whichever we have (never raw HTML
    in content_text)."""
    entry = {
        "id": it["link"] + "#" + it["id"],
        "url": it["link"],
        "title": it["title"],
        "content_text": _plain_text(it.get("summary") or "") or it["title"],
        "date_published": it.get("published") or it.get("first_seen"),
        "_first_seen": it.get("first_seen"),
    }
    if it.get("summary_html"):
        entry["content_html"] = it["summary_html"]
    if source is not None:
        tags = _item_tags(source, it)
        if tags:
            entry["tags"] = tags
    if it["link"].lower().split("?")[0].endswith(".pdf"):
        entry["attachments"] = [{"url": it["link"], "mime_type": "application/pdf"}]
    return entry


def write_json_feed(
    source: dict, items: list[dict], state: dict | None = None, max_items: int = MAX_ITEMS_PER_FEED
) -> None:
    feed_slug = safe_filename(source["id"])
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": source["name"],
        "home_page_url": source["url"],
        "feed_url": f"{SITE_BASE_URL}/feeds/json/{feed_slug}.json",
        "description": feed_description(source, state),
        "items": [
            _json_feed_item(it, source)
            for it in items[:max_items]
        ],
    }
    (FEEDS_DIR / "json").mkdir(parents=True, exist_ok=True)
    with open(FEEDS_DIR / "json" / f"{feed_slug}.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2, ensure_ascii=False)
        f.write("\n")


# --------------------------------------------------------------------------- #
# Archive feeds: the live feeds keep only the newest MAX_ITEMS_PER_FEED items
# so readers poll something small, but each source also publishes an
# append-only archive (JSON Feed) that this run's items merge into and never
# rotate out of — so a consumer can backfill history it wasn't subscribed
# for. Bounded per source; the git history remains the unabridged record.
# --------------------------------------------------------------------------- #
MAX_ARCHIVE_ITEMS = 500


def _entry_sort_key(entry: dict) -> float:
    try:
        return dateutil_parser.isoparse(
            entry.get("date_published") or entry.get("_first_seen") or ""
        ).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0.0


def write_archive_feed(source: dict, items: list[dict], state: dict | None = None) -> None:
    feed_slug = safe_filename(source["id"])
    archive_dir = FEEDS_DIR / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"{feed_slug}.json"

    merged: dict[str, dict] = {}
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                for entry in json.load(f).get("items") or []:
                    if entry.get("id"):
                        merged[entry["id"]] = entry
        except (json.JSONDecodeError, OSError):
            merged = {}

    for it in items:
        # Baseline placeholders say "nothing happened"; keep them out of the
        # permanent record.
        if (it.get("title") or "").startswith("Monitoring started:"):
            continue
        entry = _json_feed_item(it, source)
        prev = merged.get(entry["id"])
        # An item can be re-observed without its enriched body (e.g. after
        # rotating back in through the seen map). Never let a body-less
        # re-observation overwrite an archived entry that has real content.
        if (
            prev is not None
            and entry.get("content_text") == entry.get("title")
            and prev.get("content_text") not in (None, "", prev.get("title"))
        ):
            continue
        merged[entry["id"]] = entry

    entries = sorted(merged.values(), key=_entry_sort_key, reverse=True)[:MAX_ARCHIVE_ITEMS]
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": f"{source['name']} — Archive",
        "home_page_url": source["url"],
        "feed_url": f"{SITE_BASE_URL}/feeds/archive/{feed_slug}.json",
        "description": (
            f"Append-only archive of every item this monitor has observed for "
            f"{source['name']} (newest first, capped at {MAX_ARCHIVE_ITEMS}). "
            "The live feed carries only the most recent items; use this to backfill."
        ),
        "items": entries,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2, ensure_ascii=False)
        f.write("\n")


# --------------------------------------------------------------------------- #
# Aggregate feeds: one "everything" feed, one feed per category, and a daily
# digest. A firm's IT department deploys ONE of these (an Outlook folder, a
# Power Automate flow, an RSS-to-email digest) instead of maintaining 100+
# per-source subscriptions.
# --------------------------------------------------------------------------- #
MAX_ALL_FEED_ITEMS = 200
MAX_CATEGORY_FEED_ITEMS = 100
DIGEST_DAYS = 14


def category_slug(category: str) -> str:
    return re.sub(r"-+", "-", safe_filename(category.lower().replace(" ", "-"))).strip("-")


def _aggregate_pool(per_source: list[tuple[dict, list[dict]]]) -> list[dict]:
    """All real items across sources, newest first, each stamped with its
    origin source's name and category tags. Baseline placeholders are
    excluded (they say "nothing happened yet")."""
    pooled = []
    seen_guids = set()
    for source, items in per_source:
        for it in items:
            if (it.get("title") or "").startswith("Monitoring started:"):
                continue
            # Some sources deliberately overlap (filtered slices of one
            # upstream feed/table, CourtListener vs. court-site opinion
            # listings) — keep one copy per underlying item so combined
            # feeds don't carry duplicate guids.
            guid = it["link"] + "#" + it["id"]
            if guid in seen_guids:
                continue
            seen_guids.add(guid)
            agg = dict(it)
            agg["title"] = f"{it['title']} — {source['name']}"
            agg["_agg_tags"] = [
                t for t in (source.get("category"), source.get("subcategory")) if t
            ]
            pooled.append(agg)

    def _key(it: dict) -> float:
        try:
            return dateutil_parser.isoparse(it.get("published") or "").timestamp()
        except (ValueError, TypeError, OverflowError):
            return 0.0

    pooled.sort(key=_key, reverse=True)
    return pooled


def _digest_items(pooled: list[dict]) -> list[dict]:
    """One item per COMPLETED day (yesterday and earlier, UTC), summarizing
    everything first seen that day. Only completed days are emitted so each
    digest item's content and pubDate never change after publication —
    consumers that key on pubDate (Power Automate, Mailchimp) would otherwise
    see the same GUID mutate all day long."""
    today = now_utc().date()
    by_day: dict[str, list[dict]] = {}
    for it in pooled:
        first_seen = it.get("first_seen") or it.get("published")
        if not first_seen:
            continue
        try:
            day = dateutil_parser.isoparse(first_seen).date()
        except (ValueError, TypeError):
            continue
        if day >= today or (today - day).days > DIGEST_DAYS:
            continue
        by_day.setdefault(day.isoformat(), []).append(it)

    digest_items = []
    for day, day_items in sorted(by_day.items(), reverse=True):
        by_category: dict[str, list[dict]] = {}
        for it in day_items:
            cat = (it.get("_agg_tags") or ["Other"])[0]
            by_category.setdefault(cat, []).append(it)
        text_parts, html_parts = [], []
        for cat in sorted(by_category):
            text_parts.append(cat + ":")
            html_parts.append(f"<h3>{html.escape(cat)}</h3><ul>")
            for it in by_category[cat]:
                text_parts.append(f"  - {it['title']}\n    {it['link']}")
                html_parts.append(
                    f'<li><a href="{html.escape(it["link"])}">{html.escape(it["title"])}</a></li>'
                )
            html_parts.append("</ul>")
        n = len(day_items)
        digest_items.append(
            {
                "id": f"digest-{day}",
                "title": f"Maine Government Feeds daily digest — {n} new item{'s' if n != 1 else ''} — {day}",
                "link": f"{SITE_BASE_URL}/#digest-{day}",
                "summary": "\n".join(text_parts)[:8000],
                "summary_html": "".join(html_parts)[:16000],
                "published": f"{day}T23:59:59+00:00",
                "first_seen": f"{day}T23:59:59+00:00",
            }
        )
    return digest_items


def write_aggregate_feeds(per_source: list[tuple[dict, list[dict]]]) -> list[dict]:
    """Write the all-sources feed, per-category feeds, and the daily digest.
    Returns the synthetic source entries so the catalog/index can list them."""
    pooled = _aggregate_pool(per_source)
    synthetic: list[dict] = []

    all_src = {
        "id": "all",
        "name": "Maine Government Feeds — Everything",
        "category": "Combined Feeds",
        "subcategory": "All sources combined",
        "url": SITE_BASE_URL,
        "notes": (
            "Every item from every monitored source in one feed, newest first. "
            "Items carry category tags for filtering/routing."
        ),
    }
    write_rss_atom(all_src, pooled, max_items=MAX_ALL_FEED_ITEMS)
    write_json_feed(all_src, pooled, max_items=MAX_ALL_FEED_ITEMS)
    synthetic.append(all_src)

    categories: dict[str, list[dict]] = {}
    for source, _ in per_source:
        categories.setdefault(source["category"], [])
    for it in pooled:
        cat = (it.get("_agg_tags") or [None])[0]
        if cat in categories:
            categories[cat].append(it)
    for category, cat_items in sorted(categories.items()):
        slug = category_slug(category)
        cat_src = {
            "id": f"category-{slug}",
            "name": f"Maine Government Feeds — {category}",
            "category": "Combined Feeds",
            "subcategory": f"Everything in {category}",
            "url": SITE_BASE_URL,
            "notes": f"All items from every source in the '{category}' category, newest first.",
        }
        write_rss_atom(cat_src, cat_items, max_items=MAX_CATEGORY_FEED_ITEMS)
        write_json_feed(cat_src, cat_items, max_items=MAX_CATEGORY_FEED_ITEMS)
        synthetic.append(cat_src)

    digest_src = {
        "id": "daily-digest",
        "name": "Maine Government Feeds — Daily Digest",
        "category": "Combined Feeds",
        "subcategory": "One item per day",
        "url": SITE_BASE_URL,
        "notes": (
            "One feed item per day (completed days only, UTC) summarizing every new "
            "item observed that day, grouped by category. The lowest-noise way to "
            "follow everything: route this to a distribution list, a Teams channel, "
            "or an RSS-to-email service."
        ),
    }
    digest_items = _digest_items(pooled)
    write_rss_atom(digest_src, digest_items, max_items=DIGEST_DAYS)
    write_json_feed(digest_src, digest_items, max_items=DIGEST_DAYS)
    synthetic.append(digest_src)
    return synthetic


CALENDAR_DIR = DOCS_DIR / "calendar"


def _ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _ics_fold(line: str) -> str:
    """Fold lines longer than 75 octets per RFC 5545 §3.1."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    parts = []
    while raw:
        chunk = raw[:74]
        # Don't split inside a multi-byte UTF-8 sequence.
        while chunk and (raw[len(chunk):len(chunk) + 1] and (raw[len(chunk)] & 0xC0) == 0x80):
            chunk = chunk[:-1]
        parts.append(chunk.decode("utf-8"))
        raw = raw[len(chunk):]
    return ("\r\n ").join(parts)


def _ics_events(source: dict, items: list[dict]) -> list[str]:
    """One all-day VEVENT per dated feed item.

    The event date is the item's published date (the source page's own date
    when parseable, otherwise the date this build first observed the item),
    so a subscribed calendar doubles as a timeline of when each opinion,
    order, bulletin, or page change appeared.
    """
    lines = []
    feed_slug = safe_filename(source["id"])
    for it in items[:MAX_ITEMS_PER_FEED]:
        published = it.get("published")
        if not published:
            continue
        try:
            dt = dateutil_parser.isoparse(published)
        except (ValueError, TypeError):
            continue
        day = dt.strftime("%Y%m%d")
        stamp_src = it.get("first_seen") or published
        try:
            stamp = dateutil_parser.isoparse(stamp_src).strftime("%Y%m%dT%H%M%SZ")
        except (ValueError, TypeError):
            stamp = f"{day}T000000Z"
        description = f"{it.get('summary') or ''}\n{it['link']}".strip()
        summary = "[{}] {}".format(source["category"], it["title"])

        # Items carrying a real event time (e.g. legislative hearings) become
        # timed one-hour events in Maine local time; everything else is an
        # all-day event on its publication date.
        start_lines = [f"DTSTART;VALUE=DATE:{day}"]
        if it.get("event_start"):
            try:
                event_dt = dateutil_parser.isoparse(it["event_start"])
                local = event_dt.strftime("%Y%m%dT%H%M%S")
                end_local = (event_dt + timedelta(hours=1)).strftime("%Y%m%dT%H%M%S")
                start_lines = [
                    f"DTSTART;TZID=America/New_York:{local}",
                    f"DTEND;TZID=America/New_York:{end_local}",
                ]
            except (ValueError, TypeError):
                pass

        lines += [
            "BEGIN:VEVENT",
            f"UID:{it['id']}@{feed_slug}.maine-government-feeds",
            f"DTSTAMP:{stamp}",
            *start_lines,
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(description)}",
            f"URL:{it['link']}",
            f"CATEGORIES:{_ics_escape(source['category'])}",
        ]
        if it.get("event_location"):
            lines.append(f"LOCATION:{_ics_escape(it['event_location'])}")
        lines += [
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    return lines


def _write_ics_file(path, name: str, event_lines: list[str]) -> None:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//maine-government-feeds//github.com/bedardandy/maine-government-feeds//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_ics_escape(name)}",
        "X-WR-TIMEZONE:America/New_York",
        # Static US Eastern VTIMEZONE so DTSTART;TZID=America/New_York
        # resolves in strict clients (RFC 5545 requires the definition).
        "BEGIN:VTIMEZONE",
        "TZID:America/New_York",
        "BEGIN:DAYLIGHT",
        "TZOFFSETFROM:-0500",
        "TZOFFSETTO:-0400",
        "TZNAME:EDT",
        "DTSTART:19700308T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
        "END:DAYLIGHT",
        "BEGIN:STANDARD",
        "TZOFFSETFROM:-0400",
        "TZOFFSETTO:-0500",
        "TZNAME:EST",
        "DTSTART:19701101T020000",
        "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
        "END:STANDARD",
        "END:VTIMEZONE",
        *event_lines,
        "END:VCALENDAR",
    ]
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(_ics_fold(line) for line in lines) + "\r\n")


def write_ics(source: dict, items: list[dict]) -> list[str]:
    """Write a per-source .ics calendar; returns the source's event lines
    so the caller can also build the combined all-sources calendar."""
    CALENDAR_DIR.mkdir(parents=True, exist_ok=True)
    events = _ics_events(source, items)
    _write_ics_file(
        CALENDAR_DIR / f"{safe_filename(source['id'])}.ics",
        f"{source['name']} (Maine Government Feeds)",
        events,
    )
    return events


def write_combined_ics(all_events: list[str]) -> None:
    CALENDAR_DIR.mkdir(parents=True, exist_ok=True)
    _write_ics_file(
        CALENDAR_DIR / "all-feeds.ics",
        "Maine Government Feeds — All Sources",
        all_events,
    )


def _source_health(state: dict | None) -> str:
    if not state or not state.get("last_success"):
        return "never"
    if state.get("consecutive_failures", 0) > 0:
        return "failing"
    return "ok"


def write_catalog(
    sources: list[dict],
    states: dict[str, dict] | None = None,
    synthetic_sources: list[dict] | None = None,
) -> None:
    """Machine-readable catalog (JSON Feed shell + `_`-prefixed extensions)
    plus a CSV twin, so an IT department can programmatically provision
    subscriptions, skip unhealthy sources, and detect renames. The `_schema`
    fields are additive; consumers of the v1 fields are unaffected."""
    states = states or {}
    catalog = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Maine Government Feeds Catalog",
        "home_page_url": SITE_BASE_URL,
        "feed_url": f"{SITE_BASE_URL}/feeds/json/catalog.json",
        "description": "Catalog of all monitored Maine government, court, and legal-agency sources.",
        "_schema_version": 2,
        "_generated_at": iso(now_utc()),
        "_build_commit": os.environ.get("GITHUB_SHA", ""),
        "_update_interval_minutes": FEED_TTL_MINUTES,
        "_site": {
            "opml_url": f"{SITE_BASE_URL}/opml/maine-government-feeds.opml",
            "roles_opml_url": f"{SITE_BASE_URL}/opml/curated-roles.opml",
            "all_feed_url": f"{SITE_BASE_URL}/feeds/rss/all.xml",
            "digest_feed_url": f"{SITE_BASE_URL}/feeds/rss/daily-digest.xml",
            "status_url": f"{SITE_BASE_URL}/status.html",
            "combined_ics_url": f"{SITE_BASE_URL}/calendar/all-feeds.ics",
        },
        "_categories": sorted({s["category"] for s in sources}),
        "items": [],
    }
    for s in sources + (synthetic_sources or []):
        feed_slug = safe_filename(s["id"])
        state = states.get(s["id"])
        entry = {
            "id": s["id"],
            "title": s["name"],
            "url": s["url"],
            "content_text": f"{s['category']} / {s.get('subcategory', '')}",
            "_category": s["category"],
            "_subcategory": s.get("subcategory", ""),
            "_source_type": s.get("type", "aggregate"),
            "_rss_url": f"{SITE_BASE_URL}/feeds/rss/{feed_slug}.xml",
            "_atom_url": f"{SITE_BASE_URL}/feeds/atom/{feed_slug}.xml",
            "_json_url": f"{SITE_BASE_URL}/feeds/json/{feed_slug}.json",
            "_ics_url": f"{SITE_BASE_URL}/calendar/{feed_slug}.ics",
            "_notes": s.get("notes", ""),
        }
        if s.get("type"):
            # Real monitored sources publish an append-only archive;
            # synthetic aggregates don't (their items live in the archives
            # of their origin sources).
            entry["_archive_url"] = f"{SITE_BASE_URL}/feeds/archive/{feed_slug}.json"
        if state is not None:
            entry["_health"] = _source_health(state)
            entry["_last_success"] = state.get("last_success")
            entry["_item_count"] = len(state.get("items") or [])
            entry["_health_ignore"] = bool(s.get("health_ignore"))
        catalog["items"].append(entry)
    for r in _roles_for_index():
        slug = safe_filename(f"role-{r['id']}")
        catalog["items"].append(
            {
                "id": slug,
                "title": f"Curated — {r.get('label', r['id'])}",
                "url": f"{SITE_BASE_URL}/feeds/rss/{slug}.xml",
                "content_text": f"Curated Practice-Area Feeds / {r.get('label', r['id'])}",
                "_category": "Curated Practice-Area Feeds",
                "_subcategory": r.get("label", r["id"]),
                "_source_type": "curated-role",
                "_rss_url": f"{SITE_BASE_URL}/feeds/rss/{slug}.xml",
                "_atom_url": f"{SITE_BASE_URL}/feeds/atom/{slug}.xml",
                "_json_url": f"{SITE_BASE_URL}/feeds/json/{slug}.json",
                "_ics_url": f"{SITE_BASE_URL}/calendar/{slug}.ics",
                "_notes": " ".join((r.get("description") or "").split()),
                "_roles": [r["id"]],
            }
        )
    (FEEDS_DIR / "json").mkdir(parents=True, exist_ok=True)
    with open(FEEDS_DIR / "json" / "catalog.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # CSV twin: in practice half of IT will open this in Excel or feed it to
    # an import wizard rather than parse JSON.
    csv_cols = [
        "id", "title", "category", "subcategory", "source_type", "health",
        "item_count", "last_success", "url", "rss_url", "atom_url", "json_url", "ics_url",
    ]
    with open(FEEDS_DIR / "json" / "catalog.csv", "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_cols)
        writer.writeheader()
        for entry in catalog["items"]:
            writer.writerow(
                {
                    "id": entry["id"],
                    "title": entry["title"],
                    "category": entry["_category"],
                    "subcategory": entry["_subcategory"],
                    "source_type": entry["_source_type"],
                    "health": entry.get("_health", ""),
                    "item_count": entry.get("_item_count", ""),
                    "last_success": entry.get("_last_success", ""),
                    "url": entry["url"],
                    "rss_url": entry["_rss_url"],
                    "atom_url": entry["_atom_url"],
                    "json_url": entry["_json_url"],
                    "ics_url": entry["_ics_url"],
                }
            )


def write_opml(sources: list[dict]) -> None:
    categories: dict[str, list[dict]] = {}
    for s in sources:
        categories.setdefault(s["category"], []).append(s)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<opml version="2.0">')
    lines.append("  <head>")
    lines.append("    <title>Maine Government Feeds</title>")
    lines.append(f"    <dateCreated>{html.escape(iso(now_utc()))}</dateCreated>")
    lines.append("    <ownerName>Maine Government Feeds</ownerName>")
    lines.append(f"    <ownerId>{html.escape(SITE_BASE_URL)}</ownerId>")
    lines.append("  </head>")
    lines.append("  <body>")
    for category, items in categories.items():
        lines.append(f'    <outline text="{html.escape(category)}" title="{html.escape(category)}">')
        for s in items:
            feed_slug = safe_filename(s["id"])
            rss_url = f"{SITE_BASE_URL}/feeds/rss/{feed_slug}.xml"
            lines.append(
                "      <outline "
                f'type="rss" text="{html.escape(s["name"])}" title="{html.escape(s["name"])}" '
                f'xmlUrl="{html.escape(rss_url)}" htmlUrl="{html.escape(s["url"])}"/>'
            )
        lines.append("    </outline>")
    # Curated practice-area feeds (built by build_role_feeds.py later in the
    # same workflow run) get their own OPML group so one import covers both
    # the per-source and the curated views.
    roles = _roles_for_index()
    if roles:
        group = "Curated Practice-Area Feeds"
        lines.append(f'    <outline text="{group}" title="{group}">')
        for r in roles:
            slug = safe_filename(f"role-{r['id']}")
            name = f"Curated — {r.get('label', r['id'])}"
            rss_url = f"{SITE_BASE_URL}/feeds/rss/{slug}.xml"
            lines.append(
                "      <outline "
                f'type="rss" text="{html.escape(name)}" title="{html.escape(name)}" '
                f'xmlUrl="{html.escape(rss_url)}" htmlUrl="{html.escape(rss_url)}"/>'
            )
        lines.append("    </outline>")
    lines.append("  </body>")
    lines.append("</opml>")

    OPML_DIR.mkdir(parents=True, exist_ok=True)
    with open(OPML_DIR / "maine-government-feeds.opml", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def _roles_for_index() -> list[dict]:
    """Role definitions from roles.yml (if present) for the index's curated
    feeds section. The role feeds themselves are built by
    scripts/build_role_feeds.py in the same workflow run."""
    roles_file = common.ROOT_DIR / "roles.yml"
    if not roles_file.exists():
        return []
    try:
        import yaml

        data = yaml.safe_load(roles_file.read_text(encoding="utf-8"))
        return data.get("roles", [])
    except Exception:
        return []


def write_index_html(sources: list[dict], synthetic_sources: list[dict] | None = None) -> None:
    categories: dict[str, list[dict]] = {}
    for s in sources + (synthetic_sources or []):
        categories.setdefault(s["category"], []).append(s)

    sections = []
    # List the Combined Feeds section first: it's what most subscribers want.
    ordered = sorted(categories, key=lambda c: (c != "Combined Feeds", c))
    for category in ordered:
        rows = []
        for s in sorted(categories[category], key=lambda x: x["name"]):
            feed_slug = safe_filename(s["id"])
            # Synthetic aggregate feeds have no per-source calendar/archive.
            ics_cell = (
                f'<td><a href="calendar/{feed_slug}.ics">ICS</a></td>'
                if s.get("type")
                else "<td>—</td>"
            )
            archive_cell = (
                f'<td><a href="feeds/archive/{feed_slug}.json">Archive</a></td>'
                if s.get("type")
                else "<td>—</td>"
            )
            rows.append(
                "        <tr>"
                f'<td>{html.escape(s["name"])}</td>'
                f'<td>{html.escape(s.get("subcategory", ""))}</td>'
                f'<td><a href="{html.escape(s["url"])}">source</a></td>'
                f'<td><a href="feeds/rss/{feed_slug}.xml">RSS</a></td>'
                f'<td><a href="feeds/atom/{feed_slug}.xml">Atom</a></td>'
                f'<td><a href="feeds/json/{feed_slug}.json">JSON</a></td>'
                f"{ics_cell}"
                f"{archive_cell}"
                "</tr>"
            )
        sections.append(
            f"      <h2>{html.escape(category)}</h2>\n"
            "      <table>\n"
            "        <thead><tr><th>Source</th><th>Subcategory</th><th>Page</th>"
            "<th>RSS</th><th>Atom</th><th>JSON</th><th>Calendar</th><th>Archive</th></tr></thead>\n"
            "        <tbody>\n" + "\n".join(rows) + "\n        </tbody>\n      </table>"
        )

    roles = _roles_for_index()
    if roles:
        rows = []
        for r in roles:
            slug = safe_filename(f"role-{r['id']}")
            rows.append(
                "        <tr>"
                f'<td>{html.escape("Curated — " + r.get("label", r["id"]))}</td>'
                f'<td>{html.escape(" ".join((r.get("description") or "").split())[:160])}</td>'
                f'<td><a href="feeds/rss/{slug}.xml">RSS</a></td>'
                f'<td><a href="feeds/atom/{slug}.xml">Atom</a></td>'
                f'<td><a href="feeds/json/{slug}.json">JSON</a></td>'
                f'<td><a href="calendar/{slug}.ics">ICS</a></td>'
                "</tr>"
            )
        sections.insert(
            1,
            "      <h2>Curated Practice-Area Feeds</h2>\n"
            "      <p>Cross-source feeds filtered to one practitioner role "
            '(see <a href="opml/curated-roles.opml">curated-roles.opml</a> to import all of them at once).</p>\n'
            "      <table>\n"
            "        <thead><tr><th>Feed</th><th>Scope</th>"
            "<th>RSS</th><th>Atom</th><th>JSON</th><th>Calendar</th></tr></thead>\n"
            "        <tbody>\n" + "\n".join(rows) + "\n        </tbody>\n      </table>",
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maine Government Feeds</title>
<link rel="stylesheet" href="style.css">
<link rel="alternate" type="application/rss+xml" title="Maine Government Feeds — Everything" href="feeds/rss/all.xml">
<link rel="alternate" type="application/rss+xml" title="Maine Government Feeds — Daily Digest" href="feeds/rss/daily-digest.xml">
<link rel="alternate" type="application/feed+json" title="Maine Government Feeds — Catalog (JSON)" href="feeds/json/catalog.json">
<link rel="alternate" type="text/calendar" title="Maine Government Feeds — Combined Calendar" href="calendar/all-feeds.ics">
</head>
<body>
  <header>
    <h1>Maine Government Feeds</h1>
    <p>Unofficial RSS, Atom, and JSON feeds for Maine government, court, registry, legislative,
       and legal-agency webpages. Built and published automatically from
       <a href="https://github.com/bedardandy/maine-government-feeds">github.com/bedardandy/maine-government-feeds</a>.</p>
    <p>
      <a href="opml/maine-government-feeds.opml">Download OPML (Outlook / FreshRSS / Feedly)</a>
      &middot; <a href="opml/curated-roles.opml">Practice-area OPML</a>
      &middot; <a href="status.html">Feed health dashboard</a>
      &middot; <a href="feeds/json/catalog.json">JSON catalog</a>
      &middot; <a href="feeds/json/catalog.csv">CSV catalog</a>
      &middot; <a href="calendar/all-feeds.ics">Combined iCalendar (.ics)</a>
    </p>
    <p><strong>New here?</strong> Subscribe to the
      <a href="feeds/rss/daily-digest.xml">Daily Digest</a> (one item per day covering everything),
      the <a href="feeds/rss/all.xml">Everything feed</a>, a per-category combined feed, or a
      curated practice-area feed below — instead of importing all individual sources.</p>
    <p>Every source also publishes an iCalendar file (one all-day event per dated item),
       so you can subscribe in Outlook / Google Calendar / Apple Calendar and see when
       each opinion, order, or notice appeared. Subscribe to the combined calendar URL
       (<code>{html.escape(SITE_BASE_URL)}/calendar/all-feeds.ics</code>) or any
       per-source ICS link below.</p>
    <p class="disclaimer">This is not legal advice and not an official government publication.
      Always verify against the official source linked in each row.</p>
  </header>
  <main>
{chr(10).join(sections)}
  </main>
  <footer>
    <p>Generated {html.escape(iso(now_utc()))} by GitHub Actions. Last updated automatically every 6 hours.</p>
  </footer>
</body>
</html>
"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    with open(DOCS_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(html_doc)


def write_style_css() -> None:
    css = """
body { font-family: -apple-system, Segoe UI, Helvetica, Arial, sans-serif; margin: 0; padding: 0 1.5rem 3rem; color: #1a1a1a; background: #fafafa; }
header { max-width: 60rem; margin: 0 auto; padding-top: 2rem; }
main { max-width: 60rem; margin: 0 auto; }
h1 { margin-bottom: 0.25rem; }
h2 { margin-top: 2.5rem; border-bottom: 2px solid #14213d; padding-bottom: 0.25rem; }
table { width: 100%; border-collapse: collapse; margin-bottom: 1rem; font-size: 0.92rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #ddd; }
th { background: #14213d; color: #fff; }
tr:nth-child(even) { background: #f1f1f1; }
a { color: #003087; }
.disclaimer { font-size: 0.85rem; color: #555; background: #fff3cd; padding: 0.6rem 0.8rem; border-radius: 4px; border: 1px solid #ffe69c; }
.status-ok { color: #1a7f37; font-weight: 600; }
.status-fail { color: #b91c1c; font-weight: 600; }
.status-unknown { color: #888; }
footer { max-width: 60rem; margin: 2rem auto 0; font-size: 0.8rem; color: #666; }
"""
    with open(DOCS_DIR / "style.css", "w", encoding="utf-8") as f:
        f.write(css)


def write_nojekyll() -> None:
    # Tells GitHub Pages to serve docs/ as-is instead of running it through Jekyll,
    # which otherwise ignores/mishandles some non-HTML files and nested folders.
    (DOCS_DIR / ".nojekyll").touch()


def write_robots_and_sitemap() -> None:
    (DOCS_DIR / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {SITE_BASE_URL}/sitemap.xml\n",
        encoding="utf-8",
    )
    urls = [
        f"{SITE_BASE_URL}/",
        f"{SITE_BASE_URL}/status.html",
        f"{SITE_BASE_URL}/feeds/json/catalog.json",
        f"{SITE_BASE_URL}/opml/maine-government-feeds.opml",
    ]
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for u in urls:
        lines.append(f"  <url><loc>{html.escape(u)}</loc></url>")
    lines.append("</urlset>")
    (DOCS_DIR / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_manifest() -> None:
    """docs/feeds/json/manifest.json: sha256 + size of every published file.

    Combined with the git history, the `_first_seen` stamps, and the Wayback
    snapshots, this gives a verifiable record of exactly what was published —
    part of the provenance chain for demonstrating when a notice appeared."""
    entries = []
    for path in sorted(DOCS_DIR.rglob("*")):
        if not path.is_file() or path.name in ("manifest.json", ".nojekyll"):
            continue
        data = path.read_bytes()
        entries.append(
            {
                "path": str(path.relative_to(DOCS_DIR)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    manifest = {
        "generated_at": iso(now_utc()),
        "build_commit": os.environ.get("GITHUB_SHA", ""),
        "files": entries,
    }
    with open(FEEDS_DIR / "json" / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main() -> int:
    sources = enabled_sources(load_sources())
    if not sources:
        print("No enabled sources found in sources.yml", file=sys.stderr)
        return 1
    for s in sources:
        s["url"] = resolve_url(s["url"])

    client = make_client()
    failures = []
    states: dict[str, dict] = {}
    per_source: list[tuple[dict, list[dict]]] = []
    try:
        combined_events = []
        for source in sources:
            state, items, note = build_source(source, client)
            states[source["id"]] = state
            per_source.append((source, items))
            write_rss_atom(source, items, state)
            write_json_feed(source, items, state)
            write_archive_feed(source, items, state)
            combined_events.extend(write_ics(source, items))
            status_bits = [source["id"], str(state.get("last_status"))]
            if note:
                status_bits.append(note)
            if not state.get("last_success") or state.get("consecutive_failures", 0) > 0:
                failures.append(source["id"])
            print(" | ".join(status_bits))
    finally:
        client.close()

    synthetic_sources = write_aggregate_feeds(per_source)
    write_combined_ics(combined_events)
    write_opml(sources)
    write_catalog(sources, states, synthetic_sources)
    write_index_html(sources, synthetic_sources)
    write_style_css()
    write_nojekyll()
    write_robots_and_sitemap()
    # NOTE: the provenance manifest (manifest.json) is written by
    # validate_feeds.py, which runs last in the workflow — after the role
    # feeds are built — so it covers every published file.

    print(f"\nBuilt {len(sources)} feeds. {len(failures)} source(s) currently failing/degraded.")
    # Individual source failures are expected (government sites go down); never
    # fail the build for that. validate_feeds.py decides what's CI-breaking.
    return 0


if __name__ == "__main__":
    sys.exit(main())
