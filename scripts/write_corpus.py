#!/usr/bin/env python3
"""Export the observed-item corpus as newline-delimited JSON for a firm's
knowledge/search pipeline (RAG ingestion, analytics).

One JSON object per line, newest first, drawn from every per-source state
file (the live observation window). Fields:

    id, url, title, content_text, date_published, _first_seen,
    source_id, source_name, category, subcategory, roles,
    pdf_link, _meta (dockets / LD number / effective date),
    _minor_change

The file is written to the repository root as `corpus.jsonl`, which is
gitignored — it is published as a CI artifact (see build.yml) rather than
committed, so multi-megabyte regenerations never bloat the repo or Pages.

Usage:
    python scripts/write_corpus.py [--output PATH] [--max-items N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_feeds  # noqa: E402
from common import iter_source_state_paths, load_sources  # noqa: E402

DEFAULT_OUTPUT = Path("corpus.jsonl")
MAX_CORPUS_ITEMS = 3000
MAX_BODY_CHARS = 2500


def build_corpus_rows(max_items: int = MAX_CORPUS_ITEMS, max_body: int = MAX_BODY_CHARS):
    sources = {s["id"]: s for s in load_sources()}
    rows = []
    for path in iter_source_state_paths():
        sid = path.stem
        src = sources.get(sid)
        if not src:
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for it in state.get("items") or []:
            if (it.get("title") or "").startswith(("Monitoring started:",)):
                continue
            rows.append(
                {
                    "id": it["link"] + "#" + it["id"],
                    "url": it.get("link"),
                    "title": it.get("title"),
                    "content_text": build_feeds._plain_text(it.get("summary") or "")[:max_body],
                    "date_published": it.get("published"),
                    "_first_seen": it.get("first_seen"),
                    "source_id": sid,
                    "source_name": src.get("name"),
                    "category": src.get("category"),
                    "subcategory": src.get("subcategory"),
                    "roles": it.get("roles") or [],
                    "pdf_link": bool(
                        (it.get("link") or "").lower().split("?")[0].endswith(".pdf")
                    ),
                    "_meta": build_feeds.extract_item_meta(it) or None,
                    "_minor_change": bool(it.get("_minor_change")) or None,
                }
            )

    def _key(r):
        return r["_first_seen"] or r["date_published"] or ""

    rows.sort(key=_key, reverse=True)
    return rows[:max_items]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    ap.add_argument("--max-items", type=int, default=MAX_CORPUS_ITEMS)
    args = ap.parse_args()

    rows = build_corpus_rows(max_items=args.max_items)
    with open(args.output, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Checksum sidecar so consumers can verify an artifact download before
    # ingesting it (the artifact is fetched over the network and unzipped).
    digest = hashlib.sha256(args.output.read_bytes()).hexdigest()
    sidecar = args.output.with_name(args.output.name + ".sha256")
    sidecar.write_text(f"{digest}  {args.output.name}\n", encoding="utf-8")
    print(f"Wrote {len(rows)} corpus rows to {args.output} ({digest[:12]}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
