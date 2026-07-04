#!/usr/bin/env python3
"""One-time migration: scrub stored item bodies in data/state/*.json.

Items captured by builds predating the body-cleaning work (commit
"Improve feed body quality...") still hold raw Drupal chrome / HTML in their
`summary` field and usually lack a `summary_html`. The published feeds are
already clean (write-time guards heal these on the fly), but the on-disk
state is not. This rewrites each item's `summary`/`summary_html` through the
same clean_entry_body() the parser now uses, so state matches output.

Idempotent: re-running on already-clean state is a no-op. Run once, review
the diff, commit.

    python scripts/migrate_scrub_state.py          # rewrite in place
    python scripts/migrate_scrub_state.py --dry-run # report only
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from build_feeds import clean_entry_body
from common import STATE_DIR


def migrate_item(it: dict) -> bool:
    """Re-clean one item's body fields. Returns True if anything changed."""
    title = it.get("title") or ""
    # Prefer the richest raw body available: an existing summary_html, else the
    # summary (which for stale items is raw HTML, for html/page_monitor items is
    # already plain and passes through untouched).
    raw = it.get("summary_html") or it.get("summary") or ""
    if not raw:
        return False
    plain, clean_html = clean_entry_body(raw, title)
    plain = plain[:2000]
    clean_html = clean_html[:8000]

    changed = False
    if plain and plain != it.get("summary"):
        it["summary"] = plain
        changed = True
    # Only store summary_html when it carries markup beyond the plain text;
    # for plain-text sources there's nothing to gain from duplicating it.
    if clean_html and "<" in clean_html and clean_html != it.get("summary_html"):
        it["summary_html"] = clean_html
        changed = True
    return changed


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    files = sorted(STATE_DIR.glob("*.json"))
    total_files = 0
    total_items = 0
    for path in files:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"skip {path.name}: {exc}")
            continue
        items = state.get("items") or []
        touched = sum(1 for it in items if migrate_item(it))
        if touched:
            total_files += 1
            total_items += touched
            print(f"{'would scrub' if dry_run else 'scrubbed'} {path.name}: {touched} item(s)")
            if not dry_run:
                path.write_text(
                    json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
    verb = "Would rewrite" if dry_run else "Rewrote"
    print(f"\n{verb} {total_items} item(s) across {total_files} state file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
