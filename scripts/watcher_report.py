#!/usr/bin/env python3
"""Build the diagnostic report consumed by the feeds-maintenance watcher.

Collects every enabled source that is currently failing, then — robots.txt
rules honored, descriptive UA sent, politeness delays applied — fetches each
failing URL once and saves alongside it:

    _watcher/report.md          overview table + per-source sections
    _watcher/<id>.page.html     raw response (truncated)
    _watcher/<id>.page.txt      visible text (truncated)
    _watcher/<id>.config.yml    the source's current entry in sources.yml

The Codex watcher (see .github/workflows/feeds-watcher.yml) reads ONLY these
local files — the agent itself never touches the network, which keeps it
sandboxable, cheap, and deterministic. Human review happens at the resulting
maintenance PR.

Exit status is always 0; the workflow decides what to do based on output.
"""
from __future__ import annotations

import argparse
import html as html_mod
import json
import re
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yaml  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

import common  # noqa: E402

REPORT_DIR = Path("_watcher")
MAX_HTML_BYTES = 60_000
MAX_TEXT_CHARS = 12_000
MAX_SOURCES_PER_RUN = 5


def visible_text(html: str, limit: int = MAX_TEXT_CHARS) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return ""
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)[:limit]


def build(out_dir: Path, client=None, max_sources: int = MAX_SOURCES_PER_RUN) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    own_client = False
    if client is None:
        client = common.make_client()
        own_client = True

    sources = {s["id"]: s for s in common.load_sources() if s.get("enabled", True)}
    failures = []
    for path in common.iter_source_state_paths():
        sid = path.stem
        src = sources.get(sid)
        if not src or src.get("health_ignore"):
            continue
        # Health fields live in the combined snapshot; load_state merges them.
        state = common.load_state(sid)
        fails = int(state.get("consecutive_failures") or 0)
        if fails > 0:
            failures.append((fails, sid, src, state))

    failures.sort(reverse=True)
    batch = failures[:max_sources]

    lines = [
        "# Feed maintenance report",
        "",
        f"Generated {common.iso(common.now_utc())}. "
        f"{len(failures)} failing source(s); diagnosing the worst {len(batch)}.",
        "",
    ]
    for fails, sid, src, state in batch:
        url = src["url"]
        result = common.fetch(url, client)
        snippet_html = (result.text or "")[:MAX_HTML_BYTES]
        (out_dir / f"{sid}.page.html").write_text(snippet_html, encoding="utf-8")
        (out_dir / f"{sid}.page.txt").write_text(visible_text(snippet_html), encoding="utf-8")
        cfg = {k: v for k, v in src.items()}
        (out_dir / f"{sid}.config.yml").write_text(
            yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        lines += [
            f"## {src['name']} (`{sid}`)",
            "",
            f"- Consecutive failures: **{fails}**",
            f"- Last HTTP/error: `{state.get('last_status') or state.get('last_error')}`",
            f"- URL: <{url}>",
            f"- Type: `{src.get('type', 'page_monitor')}`",
            "",
            "Local files saved for this source:",
            "",
            textwrap.dedent(
                f"""\
                - `_watcher/{sid}.page.html` — truncated raw response
                - `_watcher/{sid}.page.txt` — extracted visible text
                - `_watcher/{sid}.config.yml` — current sources.yml entry"""
            ),
            "",
            html_mod.escape(state.get("last_error") or "")[:300],
            "",
        ]

    (out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")
    if own_client:
        client.close()
    print(f"Wrote report for {len(batch)} failing source(s) to {out_dir}/report.md")
    return len(batch)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out-dir", type=Path, default=REPORT_DIR)
    ap.add_argument("--max-sources", type=int, default=MAX_SOURCES_PER_RUN)
    args = ap.parse_args()
    build(args.out_dir, max_sources=args.max_sources)
    return 0


if __name__ == "__main__":
    sys.exit(main())
