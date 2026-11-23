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
import os
import sys

import httpx

from common import enabled_sources, load_sources, load_state

MARKER = "<!-- source-health-tracker -->"
API = "https://api.github.com"


def failing_sources(threshold: int) -> list[dict]:
    rows = []
    for src in enabled_sources(load_sources()):
        if src.get("health_ignore"):
            continue
        sid = src["id"]
        # Health fields now live in _health.json. Merge through the shared
        # loader so snapshot-only failures cannot look like a recovery.
        state = load_state(sid)
        fails = int(state.get("consecutive_failures") or 0)
        degraded = state.get("selector_fallback_since")
        if fails >= threshold or degraded:
            rows.append(
                {
                    "id": sid,
                    "name": src.get("name", sid),
                    "category": src.get("category", ""),
                    "fails": fails,
                    "status": (
                        f"selectors match nothing since {degraded[:10]}"
                        if degraded and fails < threshold
                        else state.get("last_status")
                    ),
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

    def find_tracker(self, marker: str = MARKER) -> dict | None:
        r = self.client.get(f"/repos/{self.repo}/issues", params={"state": "open", "per_page": 100})
        r.raise_for_status()
        for issue in r.json():
            if "pull_request" in issue:
                continue
            if marker in (issue.get("body") or ""):
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


BUILD_MARKER = "<!-- build-failure-tracker -->"


def reconcile_build_issue(gh: "GH", outcome: str, run_url: str) -> str:
    """Keep ONE issue open while scheduled builds are failing.

    A failing build publishes nothing, and nothing else announced it: the
    feeds simply stopped updating (25 days, once). This opens an issue on the
    first failure, adds one comment per later failing run, and closes the
    issue on the next success."""
    tracker = gh.find_tracker(BUILD_MARKER)
    if outcome == "failure":
        if tracker:
            gh.comment(tracker["number"], f"Still failing: {run_url}")
            return f"commented on build-failure issue #{tracker['number']}"
        gh.create(
            "🚨 Feed build is failing: nothing is being published",
            f"{BUILD_MARKER}\n"
            "The scheduled build failed, so the published feeds, the status dashboard and "
            "`data/state/` were **not** updated. Subscribers see stale feeds until this is fixed.\n\n"
            f"First failing run: {run_url}\n\n"
            "This issue is maintained automatically: each further failing run adds a comment, "
            "and the next successful build closes it.",
        )
        return "opened build-failure issue"
    if tracker:
        gh.comment(tracker["number"], f"{BUILD_MARKER}\nBuild succeeded again: {run_url}. Closing automatically. ✅")
        gh.close(tracker["number"])
        return f"closed build-failure issue #{tracker['number']}"
    return "build ok; no build-failure issue open"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--build-outcome", choices=("success", "failure"),
        help="reconcile the build-failure issue instead of the source-health issue",
    )
    args = ap.parse_args()
    threshold = int(os.environ.get("HEALTH_FAIL_THRESHOLD", "3"))

    if args.build_outcome:
        token = os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")
        run_url = (
            f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
            f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"
        )
        if args.dry_run or not token or not repo:
            print(f"[dry-run] build outcome {args.build_outcome}; no GitHub API calls made.")
            return 0
        try:
            print(reconcile_build_issue(GH(token, repo), args.build_outcome, run_url))
        except httpx.HTTPError as exc:
            print(f"Build-failure tracker API error (ignored): {exc}", file=sys.stderr)
        return 0

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
