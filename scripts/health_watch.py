#!/usr/bin/env python3
"""Source-health watcher: surface persistently failing sources as a GitHub Issue.

Reads every data/state/<id>.json after a build, finds sources that have failed
for at least THRESHOLD consecutive runs (ignoring sources explicitly marked
`health_ignore: true` in sources.yml — e.g. hosts that intentionally block
bots), and maintains ONE tracking issue:

  * failing set non-empty, no open tracker  -> open a new issue
  * failing set non-empty, tracker already open -> update its body
  * failing set empty, tracker open -> close it with an "all recovered" comment

The tracker issue is identified by a hidden marker in its body, so no labels
need to pre-exist. Uses the built-in GITHUB_TOKEN (needs `issues: write`); makes
no external-service calls beyond api.github.com. Fail-soft: any API error prints
a warning and exits 0 so the build is never broken.

    python scripts/health_watch.py --dry-run   # print, no API calls
    python scripts/health_watch.py             # reconcile the tracker issue

Env: GITHUB_TOKEN, GITHUB_REPOSITORY (owner/repo), HEALTH_FAIL_THRESHOLD (=3).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import httpx

from common import STATE_DIR, load_sources

MARKER = "<!-- source-health-tracker -->"
API = "https://api.github.com"


def failing_sources(threshold: int) -> list[dict]:
    meta = {s["id"]: s for s in load_sources()}
    rows = []
    for path in sorted(STATE_DIR.glob("*.json")):
        sid = path.stem
        src = meta.get(sid)
        if src is None or src.get("health_ignore"):
            continue
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        fails = int(state.get("consecutive_failures") or 0)
        if fails >= threshold:
            rows.append(
                {
                    "id": sid,
                    "name": src.get("name", sid),
                    "category": src.get("category", ""),
                    "fails": fails,
                    "status": state.get("last_status"),
                    "error": (state.get("last_error") or "")[:200],
                    "last_success": state.get("last_success"),
                    "url": src.get("url", ""),
                }
            )
    rows.sort(key=lambda r: (-r["fails"], r["category"], r["id"]))
    return rows


def issue_body(rows: list[dict], threshold: int) -> str:
    lines = [
        MARKER,
        "## Source-health watcher",
        "",
        f"The following **{len(rows)}** source(s) have failed **≥ {threshold} "
        "consecutive** builds. This issue is maintained automatically and will "
        "close itself when all listed sources recover.",
        "",
        "| Source | Category | Fails | Last status | Last success |",
        "| --- | --- | --: | --- | --- |",
    ]
    for r in rows:
        lines.append(
            f"| [{r['name']}]({r['url']}) (`{r['id']}`) | {r['category']} | "
            f"{r['fails']} | {r['status']} | {r['last_success'] or '—'} |"
        )
    lines += [
        "",
        "<sub>Sources that intentionally block bots (WAF/robots.txt) are marked "
        "`health_ignore: true` in `sources.yml` and excluded. A source failing "
        "here usually means a site redesign broke its selectors, or an outage — "
        "check the linked URL and update `sources.yml` if needed.</sub>",
    ]
    return "\n".join(lines)


class GH:
    def __init__(self, token: str, repo: str):
        self.repo = repo
        self.client = httpx.Client(
            base_url=API,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    def find_tracker(self) -> dict | None:
        r = self.client.get(f"/repos/{self.repo}/issues", params={"state": "open", "per_page": 100})
        r.raise_for_status()
        for issue in r.json():
            if "pull_request" in issue:
                continue
            if MARKER in (issue.get("body") or ""):
                return issue
        return None

    def create(self, title: str, body: str) -> None:
        self.client.post(f"/repos/{self.repo}/issues", json={"title": title, "body": body}).raise_for_status()

    def update(self, number: int, body: str, title: str | None = None) -> None:
        payload = {"body": body}
        if title:
            payload["title"] = title
        self.client.patch(f"/repos/{self.repo}/issues/{number}", json=payload).raise_for_status()

    def comment(self, number: int, body: str) -> None:
        self.client.post(f"/repos/{self.repo}/issues/{number}/comments", json={"body": body}).raise_for_status()

    def close(self, number: int) -> None:
        self.client.patch(f"/repos/{self.repo}/issues/{number}", json={"state": "closed"}).raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    threshold = int(os.environ.get("HEALTH_FAIL_THRESHOLD", "3"))

    rows = failing_sources(threshold)
    print(f"{len(rows)} source(s) failing ≥ {threshold} consecutive builds.")
    for r in rows:
        print(f"  {r['fails']:>3}x  {r['id']}  ({r['status']})")

    if args.dry_run:
        print("[dry-run] no GitHub API calls made.")
        return 0

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        print("GITHUB_TOKEN/GITHUB_REPOSITORY not set — skipping issue reconciliation.", file=sys.stderr)
        return 0

    title = f"⚠️ Source health: {len(rows)} source(s) failing" if rows else "Source health"
    try:
        gh = GH(token, repo)
        tracker = gh.find_tracker()
        if rows:
            body = issue_body(rows, threshold)
            if tracker:
                gh.update(tracker["number"], body, title)
                print(f"Updated tracker issue #{tracker['number']}.")
            else:
                gh.create(title, body)
                print("Opened new tracker issue.")
        elif tracker:
            gh.comment(tracker["number"], f"{MARKER}\nAll previously-failing sources have recovered. Closing automatically. ✅")
            gh.close(tracker["number"])
            print(f"Closed tracker issue #{tracker['number']} (all recovered).")
        else:
            print("No failing sources and no open tracker — nothing to do.")
    except httpx.HTTPError as exc:
        # Fail-soft: never break the build over the watcher.
        print(f"Health watcher API error (ignored): {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
