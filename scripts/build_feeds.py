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

import html
import json
import sys
from datetime import datetime
from urllib.parse import urljoin, urlparse

import re

import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateutil_parser
from feedgen.feed import FeedGenerator

import common
from common import (
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


def parse_native_rss(text: str, source_url: str) -> list[dict] | None:
    parsed = feedparser.parse(text)
    if parsed.bozo and not parsed.entries:
        return None
    items = []
    for entry in parsed.entries:
        link = entry.get("link") or source_url
        title = html.unescape((entry.get("title") or "Untitled").strip())
        summary = html.unescape((entry.get("summary") or entry.get("description") or "").strip())
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
                "published": iso(published_dt) if published_dt else None,
            }
        )
    return items


def parse_html_selectors(text: str, base_url: str, selectors: dict) -> list[dict] | None:
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

        link = urljoin(base_url, href)

        published_dt = None
        if date_sel:
            date_node = node.select_one(date_sel)
            if date_node:
                date_text = date_node.get_text(strip=True)
                try:
                    candidate = dateutil_parser.parse(date_text, fuzzy=True)
                    if 1776 <= candidate.year <= now_utc().year + 1:
                        published_dt = candidate
                except (ValueError, OverflowError):
                    published_dt = None

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


def page_monitor_item(source: dict, text: str, base_url: str) -> tuple[str, dict | None]:
    try:
        soup = BeautifulSoup(text, "lxml")
    except Exception:
        soup = BeautifulSoup(text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    visible_text = main.get_text(" ", strip=True)
    fingerprint = text_fingerprint(visible_text)
    return fingerprint, None


def build_source(source: dict, client) -> tuple[dict, list[dict], str | None]:
    """Returns (updated_state, current_items, status_note)."""
    sid = source["id"]
    state = load_state(sid)
    state["last_checked"] = iso(now_utc())

    fetch_url = source.get("rss_url") if source.get("type") == "native_rss" else source["url"]
    result = fetch(fetch_url, client)

    if not result.ok:
        state["last_failure"] = iso(now_utc())
        state["last_status"] = result.status_code
        state["last_error"] = result.error
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        save_state(sid, state)
        return state, state.get("items", []), result.error

    state["last_success"] = iso(now_utc())
    state["last_status"] = result.status_code
    state["last_error"] = None
    state["consecutive_failures"] = 0

    new_items = None
    source_type = source.get("type", "page_monitor")

    if source_type == "native_rss":
        new_items = parse_native_rss(result.text, source["url"])
    elif source_type == "html":
        selectors = source.get("selectors", {})
        new_items = parse_html_selectors(result.text, result.final_url or source["url"], selectors)

    note = None
    if source_type in ("native_rss", "html") and new_items:
        new_items = apply_filters(new_items, source)
    if source_type in ("native_rss", "html") and not new_items:
        note = "selectors/parser returned no items; falling back to page-change monitoring"
        source_type = "page_monitor"

    existing_items = state.get("items", [])

    if source_type == "page_monitor":
        fingerprint, _ = page_monitor_item(source, result.text, source["url"])
        previous_hash = state.get("content_hash")
        state["content_hash"] = fingerprint
        if previous_hash is not None and previous_hash != fingerprint:
            change_item = {
                "id": item_id(source["url"], f"page-changed-{state['last_success']}"),
                "first_seen": state["last_success"],
                "title": f"Page updated: {source['name']}",
                "link": source["url"],
                "summary": (
                    "This page's content changed since the last check. "
                    "No structured items could be extracted automatically; "
                    "visit the page directly to review the update."
                ),
                "published": state["last_success"],
            }
            merged = [change_item] + existing_items
        else:
            merged = existing_items
            if previous_hash is None:
                note = note or "baseline established; no prior content to compare against"
    else:
        # Merge new items with existing, de-duplicating by id, newest first.
        # `first_seen` (when this build first observed the item) is preserved
        # across runs and serves as a provenance marker independent of the
        # source page's own — often missing or unparseable — date.
        existing_by_id = {it["id"]: it for it in existing_items}
        merged_map = {}
        for it in new_items:
            prior = existing_by_id.get(it["id"])
            it["first_seen"] = (prior or {}).get("first_seen") or state["last_success"]
            if not it.get("published"):
                # Keep the timestamp assigned when the item was first seen;
                # re-stamping every run would make undated items float to
                # "now" forever and destroy their value as a publication marker.
                it["published"] = (prior or {}).get("published") or it["first_seen"]
            merged_map[it["id"]] = it
        for it in existing_items:
            merged_map.setdefault(it["id"], it)
        merged = sorted(
            merged_map.values(),
            key=lambda x: x.get("published") or "",
            reverse=True,
        )
        # Re-apply filters to the merged list so items retained in state from
        # before a filter was added (or tightened) are purged, not kept forever.
        merged = apply_filters(merged, source)

    merged = merged[:MAX_ITEMS_PER_FEED]
    state["items"] = merged
    save_state(sid, state)
    return state, merged, note


def write_rss_atom(source: dict, items: list[dict]) -> None:
    fg = FeedGenerator()
    fg.id(source["url"])
    fg.title(source["name"])
    fg.link(href=source["url"], rel="alternate")
    feed_slug = safe_filename(source["id"])
    fg.link(href=f"{SITE_BASE_URL}/feeds/rss/{feed_slug}.xml", rel="self")
    fg.description(source.get("notes") or f"Monitored updates for {source['name']} ({source['category']}).")
    fg.language("en-US")
    fg.generator("maine-government-feeds (static GitHub Actions build)")

    for it in items[:MAX_ITEMS_PER_FEED]:
        fe = fg.add_entry()
        fe.id(it["link"] + "#" + it["id"])
        fe.title(it["title"])
        fe.link(href=it["link"])
        if it.get("summary"):
            fe.description(it["summary"])
        if it.get("published"):
            try:
                fe.pubDate(it["published"])
            except Exception:
                pass

    (FEEDS_DIR / "rss").mkdir(parents=True, exist_ok=True)
    (FEEDS_DIR / "atom").mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(FEEDS_DIR / "rss" / f"{feed_slug}.xml"))
    fg.atom_file(str(FEEDS_DIR / "atom" / f"{feed_slug}.xml"))


def write_json_feed(source: dict, items: list[dict]) -> None:
    feed_slug = safe_filename(source["id"])
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": source["name"],
        "home_page_url": source["url"],
        "feed_url": f"{SITE_BASE_URL}/feeds/json/{feed_slug}.json",
        "description": source.get("notes") or f"Monitored updates for {source['name']} ({source['category']}).",
        "items": [
            {
                "id": it["link"] + "#" + it["id"],
                "url": it["link"],
                "title": it["title"],
                "content_text": it.get("summary") or it["title"],
                "date_published": it.get("published"),
                "_first_seen": it.get("first_seen"),
            }
            for it in items[:MAX_ITEMS_PER_FEED]
        ],
    }
    (FEEDS_DIR / "json").mkdir(parents=True, exist_ok=True)
    with open(FEEDS_DIR / "json" / f"{feed_slug}.json", "w", encoding="utf-8") as f:
        json.dump(feed, f, indent=2, ensure_ascii=False)
        f.write("\n")


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
        lines += [
            "BEGIN:VEVENT",
            f"UID:{it['id']}@{feed_slug}.maine-government-feeds",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{day}",
            f"SUMMARY:{_ics_escape(summary)}",
            f"DESCRIPTION:{_ics_escape(description)}",
            f"URL:{it['link']}",
            f"CATEGORIES:{_ics_escape(source['category'])}",
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


def write_catalog(sources: list[dict]) -> None:
    catalog = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Maine Government Feeds Catalog",
        "home_page_url": SITE_BASE_URL,
        "feed_url": f"{SITE_BASE_URL}/feeds/json/catalog.json",
        "description": "Catalog of all monitored Maine government, court, and legal-agency sources.",
        "items": [],
    }
    for s in sources:
        feed_slug = safe_filename(s["id"])
        catalog["items"].append(
            {
                "id": s["id"],
                "title": s["name"],
                "url": s["url"],
                "content_text": f"{s['category']} / {s.get('subcategory', '')}",
                "_category": s["category"],
                "_subcategory": s.get("subcategory", ""),
                "_rss_url": f"{SITE_BASE_URL}/feeds/rss/{feed_slug}.xml",
                "_atom_url": f"{SITE_BASE_URL}/feeds/atom/{feed_slug}.xml",
                "_json_url": f"{SITE_BASE_URL}/feeds/json/{feed_slug}.json",
            }
        )
    (FEEDS_DIR / "json").mkdir(parents=True, exist_ok=True)
    with open(FEEDS_DIR / "json" / "catalog.json", "w", encoding="utf-8") as f:
        json.dump(catalog, f, indent=2, ensure_ascii=False)
        f.write("\n")


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
    lines.append("  </body>")
    lines.append("</opml>")

    OPML_DIR.mkdir(parents=True, exist_ok=True)
    with open(OPML_DIR / "maine-government-feeds.opml", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_index_html(sources: list[dict]) -> None:
    categories: dict[str, list[dict]] = {}
    for s in sources:
        categories.setdefault(s["category"], []).append(s)

    sections = []
    for category in sorted(categories):
        rows = []
        for s in sorted(categories[category], key=lambda x: x["name"]):
            feed_slug = safe_filename(s["id"])
            rows.append(
                "        <tr>"
                f'<td>{html.escape(s["name"])}</td>'
                f'<td>{html.escape(s.get("subcategory", ""))}</td>'
                f'<td><a href="{html.escape(s["url"])}">source</a></td>'
                f'<td><a href="feeds/rss/{feed_slug}.xml">RSS</a></td>'
                f'<td><a href="feeds/atom/{feed_slug}.xml">Atom</a></td>'
                f'<td><a href="feeds/json/{feed_slug}.json">JSON</a></td>'
                f'<td><a href="calendar/{feed_slug}.ics">ICS</a></td>'
                "</tr>"
            )
        sections.append(
            f"      <h2>{html.escape(category)}</h2>\n"
            "      <table>\n"
            "        <thead><tr><th>Source</th><th>Subcategory</th><th>Page</th>"
            "<th>RSS</th><th>Atom</th><th>JSON</th><th>Calendar</th></tr></thead>\n"
            "        <tbody>\n" + "\n".join(rows) + "\n        </tbody>\n      </table>"
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Maine Government Feeds</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <h1>Maine Government Feeds</h1>
    <p>Unofficial RSS, Atom, and JSON feeds for Maine government, court, registry, legislative,
       and legal-agency webpages. Built and published automatically from
       <a href="https://github.com/bedardandy/maine-government-feeds">github.com/bedardandy/maine-government-feeds</a>.</p>
    <p>
      <a href="opml/maine-government-feeds.opml">Download OPML (Outlook / FreshRSS / Feedly)</a>
      &middot; <a href="status.html">Feed health dashboard</a>
      &middot; <a href="feeds/json/catalog.json">JSON catalog</a>
      &middot; <a href="calendar/all-feeds.ics">Combined iCalendar (.ics)</a>
    </p>
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


def main() -> int:
    sources = enabled_sources(load_sources())
    if not sources:
        print("No enabled sources found in sources.yml", file=sys.stderr)
        return 1
    for s in sources:
        s["url"] = resolve_url(s["url"])

    client = make_client()
    failures = []
    try:
        combined_events = []
        for source in sources:
            state, items, note = build_source(source, client)
            write_rss_atom(source, items)
            write_json_feed(source, items)
            combined_events.extend(write_ics(source, items))
            status_bits = [source["id"], str(state.get("last_status"))]
            if note:
                status_bits.append(note)
            if not state.get("last_success") or state.get("consecutive_failures", 0) > 0:
                failures.append(source["id"])
            print(" | ".join(status_bits))
    finally:
        client.close()

    write_combined_ics(combined_events)
    write_opml(sources)
    write_catalog(sources)
    write_index_html(sources)
    write_style_css()
    write_nojekyll()

    print(f"\nBuilt {len(sources)} feeds. {len(failures)} source(s) currently failing/degraded.")
    # Individual source failures are expected (government sites go down); never
    # fail the build for that. validate_feeds.py decides what's CI-breaking.
    return 0


if __name__ == "__main__":
    sys.exit(main())
