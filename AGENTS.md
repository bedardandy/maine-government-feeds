# AGENTS.md

Guidance for automated coding agents (OpenAI Codex, Claude Code, and similar)
working in this repository. Humans: see `README.md`. For a factual,
maintainer/orchestrator-level status reference (data schema, live-vs-inert
subsystems, CI secrets, intentionally-excluded sources), see `HANDOFF.md`.

## What this repo is

A **static** feed generator. `scripts/build_feeds.py` fetches ~100 Maine
government / court / legal sources listed in `sources.yml`, detects new or
changed content, and writes RSS/Atom/JSON/iCalendar/OPML into `docs/`, which is
published via GitHub Pages. There is no server, database, or paid API. GitHub
Actions (`.github/workflows/build.yml`) rebuilds every 6 hours.

Per-source memory (dedup, timestamps, classification tags) lives in
`data/state/<source-id>.json`.

## Setup

Python 3.11+. Install dependencies:

```bash
pip install -r requirements.txt
```

Local note: on some Debian/Ubuntu boxes a bare `pip install` fails on a
setuptools `install_layout` quirk; if so, use a virtualenv and call its
interpreter explicitly (`python -m venv .venv && .venv/bin/pip install -r
requirements.txt`, then run scripts with `.venv/bin/python`). CI uses a plain
`pip install` on `ubuntu-latest` and works fine.

## Key commands

```bash
python scripts/build_feeds.py        # fetch all sources, write docs/ + update data/state/
python scripts/validate_feeds.py     # assert all generated feeds are well-formed; exit 1 if not
python scripts/classify_items.py --backend openai   # tag new items into roles (needs OPENAI_API_KEY)
python scripts/classify_items.py --backend heuristic # offline classifier (no key)
python scripts/build_role_feeds.py   # build per-role curated feeds from tagged items
python scripts/discover_feeds.py     # helper: probe a page for advertised feeds/selectors
```

`validate_feeds.py` is the CI gate — it is the check to run before proposing any
change. Individual source fetch failures are expected (government sites go
down) and never fail the build; validation only fails on malformed output.

## Conventions (important)

- **Do NOT commit generated artifacts in a code/config change.** Running
  `build_feeds.py` locally rewrites all of `docs/` and refreshes every
  `data/state/*.json` timestamp (hundreds of files). Before committing a code
  or `sources.yml`/`roles.yml` change, revert that churn so the commit contains
  only intended edits:
  ```bash
  git checkout -- data docs && git clean -fdq data docs
  ```
  CI regenerates and commits `docs/` + `data/state/` on its own schedule.
- **Adding a source:** append an entry to `sources.yml` (see existing entries
  for the `native_rss` / `html` / `page_monitor` / `me_leg_hearings` shapes and
  the optional `filters` block), then run build + validate to confirm it fetches
  200 and produces well-formed feeds. Prefer a real `native_rss` feed; fall back
  to `html` selectors; use `page_monitor` only when there is no repeating list.
- **Respect robots.txt and bot policies.** Some sources intentionally stay
  "failing" because the host blocks bots (WAF 403) or disallows crawling; do not
  work around those. Mark such sources `health_ignore: true` so the health
  watcher does not alert on them.
- **Roles / classification:** the taxonomy is `roles.yml`; each role
  `description` is the classifier prompt. Bump `taxonomy_version` to force
  re-classification. Classification is optional and fail-soft — never let it
  break the core build.

## Verifying a change

1. `python scripts/build_feeds.py` (or, for a targeted check, confirm the
   affected source(s) fetch and parse).
2. `python scripts/validate_feeds.py` → must print
   `Validation OK: <N> sources, all generated feeds well-formed.`
3. Revert generated churn (see conventions) so the diff is only your edit.

## Repository norms

- Branch, commit with a descriptive message, open a PR against the default
  branch. Keep code/config PRs free of generated `docs/`/`data/` churn.
- Secrets: role classification uses `OPENAI_API_KEY` (repo secret) and optional
  `OPENAI_MODEL` (repo variable, default `gpt-5.5`). No other secrets are needed;
  the build itself uses only the built-in `GITHUB_TOKEN`.
