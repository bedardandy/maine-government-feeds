#!/usr/bin/env python3
"""Classify harvested items into practitioner roles (multi-label).

Reads the role taxonomy (roles.yml) and every per-source state file
(data/state/*.json), assigns each item a list of applicable role ids, and
writes the tags back onto the item as `roles` (list[str]) plus `roles_v` (the
taxonomy_version they were computed under). Those cached tags drive the
per-role curated feeds built by scripts/build_role_feeds.py.

Design notes
------------
* Incremental & cached: only items missing tags for the CURRENT
  taxonomy_version are (re)classified, so a normal run touches just the handful
  of items that are new since last time. Bump taxonomy_version in roles.yml to
  force a full re-classification.
* Fail-soft: if the LLM backend has no API key or errors out, this exits 0
  without modifying tags (existing cached tags are preserved and role feeds
  still build). Classification is advisory; the per-source feeds remain the
  source of truth.
* Pluggable backend behind classify_batch(): `openai` (real GPT-5.5-class
  model, via the OpenAI REST API using httpx — no extra dependency) or
  `heuristic` (offline keyword rules, deterministic; used for local demos and
  as the no-key fallback).

Usage
-----
    python scripts/classify_items.py                 # openai backend, new items only
    python scripts/classify_items.py --backend heuristic
    python scripts/classify_items.py --backfill      # re-tag every item
    python scripts/classify_items.py --dry-run       # report, don't write
    python scripts/classify_items.py --limit 40      # cap items this run
    python scripts/classify_items.py --sources ag-press-releases,fed-sec-press

Environment (openai backend)
    OPENAI_API_KEY   required
    OPENAI_MODEL     default "gpt-5.5"
    OPENAI_BASE_URL  default "https://api.openai.com/v1"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

import httpx
import yaml

from common import STATE_DIR, load_sources

ROOT_DIR = Path(__file__).resolve().parent.parent
ROLES_FILE = ROOT_DIR / "roles.yml"
BATCH_SIZE = 20
SUMMARY_TRUNC = 500


# --------------------------------------------------------------------------- #
# Taxonomy
# --------------------------------------------------------------------------- #
def load_taxonomy() -> tuple[int, list[dict]]:
    data = yaml.safe_load(ROLES_FILE.read_text(encoding="utf-8"))
    return int(data.get("taxonomy_version", 1)), data.get("roles", [])


def item_key(it: dict) -> str:
    """Stable identity for an item within its source state."""
    return f"{it.get('link','')}#{it.get('id','')}"


def is_role_eligible(it: dict) -> bool:
    """Whether an item should be considered for role feeds at all.

    Low-signal page-monitor markers are excluded so they don't pollute role
    feeds (they float to the top because their timestamps are fresh):
      * 'Monitoring started: ...' is a pure baseline — nothing happened.
      * 'Page updated: ...' is kept only when it carries a diff excerpt
        (build_feeds writes 'What changed (text excerpt):' into the summary
        when the page actually changed); a bare page-updated with no diff is
        navigational noise. Substantive diffs still go to the model, which
        reads the excerpt and decides relevance.
    """
    title = it.get("title") or ""
    if title.startswith("Monitoring started:"):
        return False
    if title.startswith(("Page updated:", "Page changed:")):
        return "What changed" in (it.get("summary") or "")
    return True


# --------------------------------------------------------------------------- #
# Backend: heuristic (offline, deterministic)
# --------------------------------------------------------------------------- #
def classify_heuristic(items: list[dict], roles: list[dict]) -> list[list[str]]:
    """Keyword/category scoring. Not as good as the LLM, but deterministic and
    dependency-free — used for demos and when no API key is present."""
    compiled = []
    for r in roles:
        kws = [re.escape(k.lower()) for k in (r.get("keywords") or [])]
        pat = re.compile(r"\b(" + "|".join(kws) + r")\b") if kws else None
        compiled.append((r["id"], set(r.get("categories") or []), pat))
    out = []
    for it in items:
        hay = f"{it.get('title','')} {it.get('summary','')}".lower()
        cat = it.get("_category", "")
        assigned = []
        for rid, cats, pat in compiled:
            hits = len(pat.findall(hay)) if pat else 0
            if cat and cat in cats:
                hits += 2  # category is a strong prior
            if hits >= 1:
                assigned.append((rid, hits))
        assigned.sort(key=lambda x: -x[1])
        out.append([rid for rid, _ in assigned])
    return out


# --------------------------------------------------------------------------- #
# Backend: OpenAI (real GPT-5.5-class model)
# --------------------------------------------------------------------------- #
def _openai_prompt(roles: list[dict]) -> str:
    lines = [
        "You classify U.S. (Maine-focused) legal/government notices into the "
        "legal-practitioner roles below. An item may belong to SEVERAL roles or "
        "NONE. Assign a role only when the item is genuinely useful to that "
        "practitioner.",
        "",
        "ROLES (id: description):",
    ]
    for r in roles:
        desc = " ".join((r.get("description") or "").split())
        lines.append(f"- {r['id']}: {desc}")
    lines += [
        "",
        "You will receive a JSON array of items, each {i, source, category, "
        "title, summary}. Respond with ONLY a JSON object of the form "
        '{"classifications": [{"i": <index>, "roles": [<role id>, ...]}, ...]}. '
        "Use only role ids from the list above. Return an entry for every item "
        "(empty roles array if none apply).",
    ]
    return "\n".join(lines)


def classify_openai(items: list[dict], roles: list[dict]) -> list[list[str]] | None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("  OPENAI_API_KEY not set — cannot use openai backend.", file=sys.stderr)
        return None
    model = os.environ.get("OPENAI_MODEL", "gpt-5.5")
    base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    valid = {r["id"] for r in roles}
    system = _openai_prompt(roles)
    results: list[list[str]] = [[] for _ in items]

    with httpx.Client(timeout=90) as client:
        for start in range(0, len(items), BATCH_SIZE):
            batch = items[start : start + BATCH_SIZE]
            payload = [
                {
                    "i": start + j,
                    "source": it.get("_source", ""),
                    "category": it.get("_category", ""),
                    "title": (it.get("title") or "")[:300],
                    "summary": (it.get("summary") or "")[:SUMMARY_TRUNC],
                }
                for j, it in enumerate(batch)
            ]
            body = {
                "model": model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            }
            try:
                resp = client.post(
                    f"{base}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as exc:
                print(f"  openai batch @{start} failed: {exc}", file=sys.stderr)
                return None
            for entry in parsed.get("classifications", []):
                idx = entry.get("i")
                if isinstance(idx, int) and 0 <= idx < len(items):
                    results[idx] = [r for r in entry.get("roles", []) if r in valid]
            print(f"  classified items {start}–{start + len(batch) - 1}")
    return results


BACKENDS = {"openai": classify_openai, "heuristic": classify_heuristic}


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", choices=BACKENDS, default="openai")
    ap.add_argument("--backfill", action="store_true", help="re-tag every item, ignoring cache")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="cap items classified this run (0 = no cap)")
    ap.add_argument("--sources", default="", help="comma-separated source ids to restrict to")
    args = ap.parse_args()

    version, roles = load_taxonomy()
    only = {s.strip() for s in args.sources.split(",") if s.strip()}
    categories = {s["id"]: s.get("category", "") for s in load_sources()}

    # Gather items needing classification, keyed by (source_id, item_key).
    states: dict[str, dict] = {}
    pending: list[dict] = []
    refs: list[tuple[str, int]] = []
    forced_empty: list[tuple[str, int]] = []  # ineligible items → cached as no-role
    for path in sorted(STATE_DIR.glob("*.json")):
        sid = path.stem
        if only and sid not in only:
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        states[sid] = state
        category = categories.get(sid, "")
        for idx, it in enumerate(state.get("items") or []):
            if not args.backfill and it.get("roles_v") == version:
                continue
            if not is_role_eligible(it):
                forced_empty.append((sid, idx))
                continue
            enriched = dict(it, _source=sid, _category=category)
            pending.append(enriched)
            refs.append((sid, idx))

    if not pending and not forced_empty:
        print(f"Nothing to classify (taxonomy v{version}; all items current).")
        return 0
    if args.limit and len(pending) > args.limit:
        pending = pending[: args.limit]
        refs = refs[: args.limit]

    tags: list[list[str]] = []
    if pending:
        print(f"Classifying {len(pending)} item(s) via '{args.backend}' backend (taxonomy v{version})...")
        result = BACKENDS[args.backend](pending, roles)
        if result is None:
            print("Backend produced no result; leaving existing tags untouched.", file=sys.stderr)
            return 0
        tags = result
    if forced_empty:
        print(f"Excluding {len(forced_empty)} page-change baseline marker(s) from role feeds.")

    changed_items = 0
    per_role: dict[str, int] = {}
    touched: set[str] = set()
    # Ineligible items are cached as explicitly no-role so they neither reach
    # the model nor appear in role feeds, and don't re-queue next run.
    for (sid, idx), assigned in list(zip(refs, tags)) + [(ref, []) for ref in forced_empty]:
        it = states[sid]["items"][idx]
        if it.get("roles") != assigned or it.get("roles_v") != version:
            it["roles"] = assigned
            it["roles_v"] = version
            changed_items += 1
            touched.add(sid)
        for r in assigned:
            per_role[r] = per_role.get(r, 0) + 1

    if not args.dry_run:
        for sid in sorted(touched):
            (STATE_DIR / f"{sid}.json").write_text(
                json.dumps(states[sid], indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )

    verb = "Would tag" if args.dry_run else "Tagged"
    print(f"\n{verb} {changed_items} item(s) across {len(touched)} file(s).")
    print("Per-role assignments this run:")
    for r in roles:
        print(f"  {per_role.get(r['id'], 0):>4}  {r['id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
