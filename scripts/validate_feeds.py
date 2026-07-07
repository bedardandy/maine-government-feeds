#!/usr/bin/env python3
"""
Validate the generated feeds and write docs/status.html.

Exit code is nonzero only for build-breaking problems (malformed XML/JSON,
a missing feed referenced from the OPML, sources.yml itself being broken).
A government source being temporarily unreachable is recorded in the
status dashboard but is NOT a fatal validation error.
"""
from __future__ import annotations

import html
import json
import os
import sys
import xml.etree.ElementTree as ET

from dateutil import parser as dateutil_parser

import common
from common import (
    DOCS_DIR,
    FEEDS_DIR,
    OPML_DIR,
    enabled_sources,
    load_sources,
    load_state,
    now_utc,
    safe_filename,
)

# A source whose most recent successful fetch is older than this many days is
# treated as build-breaking: the feed is silently republishing stale last-good
# items and nobody has noticed. Override globally with FEED_MAX_AGE_DAYS, or
# per-source with a `max_age_days` key in sources.yml (e.g. rarely-updated
# annual pages). Sources intentionally left failing (bot/WAF blocks) should
# carry `health_ignore: true` in sources.yml, which exempts them here too.
DEFAULT_MAX_AGE_DAYS = 14


def _max_age_days_for(source: dict) -> float | None:
    """Effective max-age threshold (in days) for one source, or None to skip
    the check for it. Per-source `max_age_days` overrides the global default;
    `health_ignore: true` (or `max_age_days: 0`/null) disables the gate."""
    if source.get("health_ignore"):
        return None
    if "max_age_days" in source:
        raw = source.get("max_age_days")
        if raw is None:
            return None
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return None
        return val if val > 0 else None
    env = os.environ.get("FEED_MAX_AGE_DAYS")
    if env:
        try:
            val = float(env)
            return val if val > 0 else None
        except ValueError:
            pass
    return DEFAULT_MAX_AGE_DAYS


def check_staleness(sources: list[dict]) -> list[str]:
    """Flag sources whose last successful fetch is older than their max-age
    threshold (or that have never succeeded). Returns build-breaking messages.

    Without this gate a source can fail every fetch for weeks — its feed keeps
    regenerating from stale last-good items and CI stays green because the
    output files are still well-formed. This turns prolonged staleness into a
    real, actionable CI failure.
    """
    errors = []
    now = now_utc()
    for s in sources:
        max_age = _max_age_days_for(s)
        if max_age is None:
            continue
        state = load_state(s["id"])
        last_success = state.get("last_success")
        if not last_success:
            errors.append(
                f"Source '{s['id']}' has never successfully fetched "
                f"(no last_success; max age {max_age:g}d)."
            )
            continue
        try:
            last_dt = dateutil_parser.isoparse(last_success)
        except (ValueError, TypeError):
            errors.append(
                f"Source '{s['id']}' has an unparseable last_success "
                f"timestamp: {last_success!r}."
            )
            continue
        age_days = (now - last_dt).total_seconds() / 86400.0
        if age_days > max_age:
            failures = state.get("consecutive_failures", 0)
            errors.append(
                f"Source '{s['id']}' is stale: last verified {last_success} "
                f"({age_days:.1f} days ago > max {max_age:g}d; "
                f"{failures} consecutive failure(s))."
            )
    return errors


def check_xml_well_formed(path) -> str | None:
    try:
        ET.parse(path)
    except ET.ParseError as exc:
        return str(exc)
    except OSError as exc:
        return f"could not read file: {exc}"
    return None


def check_json_well_formed(path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        return str(exc)
    return None


def check_ics_well_formed(path) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        return f"could not read file: {exc}"
    if not content.startswith("BEGIN:VCALENDAR"):
        return "does not start with BEGIN:VCALENDAR"
    if "END:VCALENDAR" not in content:
        return "missing END:VCALENDAR"
    if content.count("BEGIN:VEVENT") != content.count("END:VEVENT"):
        return "unbalanced BEGIN:VEVENT/END:VEVENT"
    return None


def validate_opml(sources: list[dict]) -> list[str]:
    errors = []
    opml_path = OPML_DIR / "maine-government-feeds.opml"
    if not opml_path.exists():
        return ["OPML file is missing"]
    err = check_xml_well_formed(opml_path)
    if err:
        return [f"OPML is not well-formed XML: {err}"]

    tree = ET.parse(opml_path)
    xml_urls = {
        outline.get("xmlUrl")
        for outline in tree.getroot().iter("outline")
        if outline.get("xmlUrl")
    }
    for s in sources:
        feed_slug = safe_filename(s["id"])
        expected_rss = FEEDS_DIR / "rss" / f"{feed_slug}.xml"
        if not expected_rss.exists():
            errors.append(f"OPML references a feed file that doesn't exist on disk: {expected_rss}")
        matching = [u for u in xml_urls if u.endswith(f"/{feed_slug}.xml")]
        if not matching:
            errors.append(f"Source '{s['id']}' has no corresponding entry in the OPML")
    return errors


def validate_generated_feeds(sources: list[dict]) -> list[str]:
    errors = []
    for s in sources:
        feed_slug = safe_filename(s["id"])
        rss_path = FEEDS_DIR / "rss" / f"{feed_slug}.xml"
        atom_path = FEEDS_DIR / "atom" / f"{feed_slug}.xml"
        json_path = FEEDS_DIR / "json" / f"{feed_slug}.json"

        for path, kind in ((rss_path, "RSS"), (atom_path, "Atom")):
            if not path.exists():
                errors.append(f"{kind} feed missing for source '{s['id']}': {path}")
                continue
            err = check_xml_well_formed(path)
            if err:
                errors.append(f"{kind} feed for '{s['id']}' is not well-formed XML: {err}")

        if not json_path.exists():
            errors.append(f"JSON feed missing for source '{s['id']}': {json_path}")
        else:
            err = check_json_well_formed(json_path)
            if err:
                errors.append(f"JSON feed for '{s['id']}' is not valid JSON: {err}")

        ics_path = DOCS_DIR / "calendar" / f"{feed_slug}.ics"
        if not ics_path.exists():
            errors.append(f"ICS calendar missing for source '{s['id']}': {ics_path}")
        else:
            err = check_ics_well_formed(ics_path)
            if err:
                errors.append(f"ICS calendar for '{s['id']}' is malformed: {err}")

    combined_ics = DOCS_DIR / "calendar" / "all-feeds.ics"
    if not combined_ics.exists():
        errors.append("all-feeds.ics combined calendar is missing")
    else:
        err = check_ics_well_formed(combined_ics)
        if err:
            errors.append(f"all-feeds.ics is malformed: {err}")

    catalog_path = FEEDS_DIR / "json" / "catalog.json"
    if not catalog_path.exists():
        errors.append("catalog.json is missing")
    else:
        err = check_json_well_formed(catalog_path)
        if err:
            errors.append(f"catalog.json is not valid JSON: {err}")

    return errors


def write_status_html(sources: list[dict]) -> None:
    rows = []
    ok_count = 0
    fail_count = 0
    for s in sorted(sources, key=lambda x: (x["category"], x["name"])):
        state = load_state(s["id"])
        last_success = state.get("last_success") or "never"
        last_failure = state.get("last_failure") or "never"
        last_status = state.get("last_status")
        item_count = len(state.get("items", []))
        consecutive_failures = state.get("consecutive_failures", 0)

        if consecutive_failures == 0 and state.get("last_success"):
            status_class, status_label = "status-ok", "OK"
            ok_count += 1
        elif consecutive_failures > 0:
            status_class, status_label = "status-fail", f"FAILING ({consecutive_failures}x)"
            fail_count += 1
        else:
            status_class, status_label = "status-unknown", "UNKNOWN"

        notes = html.escape(state.get("last_error") or s.get("notes", "") or "")
        rows.append(
            "        <tr>"
            f'<td>{html.escape(s["name"])}</td>'
            f'<td>{html.escape(s["category"])}</td>'
            f'<td class="{status_class}">{status_label}</td>'
            f'<td>{html.escape(str(last_status))}</td>'
            f'<td>{html.escape(last_success)}</td>'
            f'<td>{html.escape(last_failure)}</td>'
            f'<td>{item_count}</td>'
            f'<td>{notes}</td>'
            "</tr>"
        )

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feed Health Dashboard — Maine Government Feeds</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
  <header>
    <h1>Feed Health Dashboard</h1>
    <p><a href="index.html">&larr; Back to feed directory</a></p>
    <p>{ok_count} source(s) healthy &middot; {fail_count} source(s) currently failing.
       A "failing" source is usually a government site being temporarily unavailable;
       it does not mean the monitoring build itself is broken.</p>
  </header>
  <main>
    <table>
      <thead>
        <tr><th>Source</th><th>Category</th><th>Status</th><th>Last HTTP Status</th>
            <th>Last Success</th><th>Last Failure</th><th>Items</th><th>Notes</th></tr>
      </thead>
      <tbody>
{chr(10).join(rows)}
      </tbody>
    </table>
  </main>
  <footer>
    <p>Generated {html.escape(common.iso(now_utc()))} by GitHub Actions.</p>
  </footer>
</body>
</html>
"""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    with open(DOCS_DIR / "status.html", "w", encoding="utf-8") as f:
        f.write(html_doc)


def main() -> int:
    try:
        sources = enabled_sources(load_sources())
    except (ValueError, OSError) as exc:
        print(f"FATAL: could not load sources.yml: {exc}", file=sys.stderr)
        return 2

    errors = []
    errors.extend(validate_generated_feeds(sources))
    errors.extend(validate_opml(sources))
    errors.extend(check_staleness(sources))

    write_status_html(sources)

    if errors:
        print(f"VALIDATION FAILED with {len(errors)} build-breaking error(s):", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"Validation OK: {len(sources)} sources, all generated feeds well-formed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
