#!/usr/bin/env python3
"""Build per-role curated feeds from classified items.

After scripts/classify_items.py has tagged items (data/state/*.json) with
`roles`, this pools every item across all sources and emits one curated feed
per role — a cross-source view of just the items relevant to that practitioner.

Each role becomes a synthetic "source" so it can reuse build_feeds.py's
existing writers (RSS/Atom, JSON Feed, iCalendar), then we add a role-only OPML
bundle. Output lands under docs/feeds/{rss,atom,json}/role-<id>.* and
docs/calendar/role-<id>.ics, with docs/opml/curated-roles.opml tying them
together.

    python scripts/build_role_feeds.py
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import yaml

from common import STATE_DIR, OPML_DIR, SITE_BASE_URL, MAX_ITEMS_PER_FEED
from build_feeds import (
    write_rss_atom,
    write_json_feed,
    write_ics,
    safe_filename,
)

ROOT_DIR = Path(__file__).resolve().parent.parent
ROLES_FILE = ROOT_DIR / "roles.yml"


def load_taxonomy() -> tuple[int, list[dict]]:
    data = yaml.safe_load(ROLES_FILE.read_text(encoding="utf-8"))
    return int(data.get("taxonomy_version", 1)), data.get("roles", [])


def pool_items() -> list[dict]:
    """Every tagged item across all sources, newest first."""
    pooled = []
    for path in sorted(STATE_DIR.glob("*.json")):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for it in state.get("items") or []:
            if it.get("roles"):
                pooled.append(it)
    pooled.sort(key=lambda it: (it.get("first_seen") or it.get("published") or ""), reverse=True)
    return pooled


def role_source(role: dict) -> dict:
    slug = f"role-{role['id']}"
    return {
        "id": slug,
        "name": f"Curated — {role['label']}",
        "category": "Curated Role Feeds",
        "url": f"{SITE_BASE_URL}/feeds/rss/{slug}.xml",
        "notes": " ".join((role.get("description") or "").split()),
    }


def write_roles_opml(roles: list[dict]) -> None:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<opml version="2.0">',
        "  <head><title>Maine Legal Feeds — Curated Role Feeds</title></head>",
        "  <body>",
        '    <outline text="Curated Role Feeds" title="Curated Role Feeds">',
    ]
    for r in roles:
        slug = safe_filename(f"role-{r['id']}")
        title = html.escape(f"Curated — {r['label']}")
        xml_url = html.escape(f"{SITE_BASE_URL}/feeds/rss/{slug}.xml")
        html_url = html.escape(f"{SITE_BASE_URL}/feeds/rss/{slug}.xml")
        lines.append(
            f'      <outline type="rss" text="{title}" title="{title}" '
            f'xmlUrl="{xml_url}" htmlUrl="{html_url}"/>'
        )
    lines += ["    </outline>", "  </body>", "</opml>", ""]
    OPML_DIR.mkdir(parents=True, exist_ok=True)
    (OPML_DIR / "curated-roles.opml").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    version, roles = load_taxonomy()
    pooled = pool_items()
    print(f"Pooled {len(pooled)} tagged item(s); building {len(roles)} role feed(s) (taxonomy v{version}).")

    for role in roles:
        rid = role["id"]
        items = [it for it in pooled if rid in (it.get("roles") or [])][:MAX_ITEMS_PER_FEED]
        src = role_source(role)
        write_rss_atom(src, items)
        write_json_feed(src, items)
        write_ics(src, items)  # per-role .ics; the main build owns all-feeds.ics
        print(f"  {len(items):>4}  {src['id']}")

    write_roles_opml(roles)
    print("Wrote role feeds + docs/opml/curated-roles.opml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
