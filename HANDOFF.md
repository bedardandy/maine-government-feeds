# HANDOFF.md

Objective reference for an orchestrator or maintainer taking over this
repository. This file records factual state that is **not** captured in
`README.md` (end-user guide) or `AGENTS.md` (agent working conventions):
the on-disk data schema, the live-vs-inert status of each subsystem, branch
and CI configuration, and the set of intentionally-excluded sources.

No recommendations are made here — this is a status report, not a plan.

## Repository at a glance

- **Purpose:** static generator that fetches Maine government / court / legal
  sources and publishes RSS/Atom/JSON Feed/OPML/iCalendar to `docs/` (GitHub
  Pages). No server, database, or paid runtime dependency.
- **Published site:** `https://bedardandy.github.io/maine-government-feeds/`
- **Sources:** 103 total in `sources.yml`, by type:
  `page_monitor` 61, `html` 28, `native_rss` 13, `me_leg_hearings` 1.
- **Categories** (count): Registries of Deeds 16, Probate Courts 16,
  Federal Maine-Related 13, Maine Agencies 11, Maine Judicial Branch 11,
  Maine Legislature 6, Maine Secretary of State 6, Maine Attorney Regulation 5,
  Federal Practice Areas 4, Governor/Executive Branch 3, Maine Attorney
  General 3, Maine Human Rights Commission 3, Maine Revenue Services 3,
  Maine Workers' Compensation Board 3.

## Branch & CI configuration (facts that surprise newcomers)

- **The repository's default branch is `claude/maine-government-feeds-4p803k`**,
  not `main`. `git ls-remote --symref origin HEAD` confirms `HEAD ->
  claude/maine-government-feeds-4p803k`. All merged PRs in this project target
  that branch, and `.github/workflows/build.yml` has its `push:` trigger scoped
  to it (line ~9). If the default branch is ever renamed, that trigger and the
  comment above it must be updated by hand.
- **Feature branch used this session:** `claude/legal-feed-audit-kaebm9`.
- **Build schedule:** GitHub Actions `build.yml`, cron `15 */6 * * *`
  (every 6 h) plus `workflow_dispatch` and push-to-default. Concurrency group
  `build-feeds`, `cancel-in-progress: false`.
- **What CI commits:** the build job regenerates and commits `docs/` and
  `data/state/` on its schedule. Code/config PRs should **not** include that
  churn (see `AGENTS.md`).
- **Permissions granted to the workflow:** `contents: write`, `issues: write`.

## Subsystem status: live vs. inert

| Subsystem | Files | Status |
| --- | --- | --- |
| Core feed build + validate | `build_feeds.py`, `validate_feeds.py` | **Live.** Runs every build; CI gate is `validate_feeds.py`. |
| Source-health watcher | `health_watch.py` | **Live.** Runs each build (`continue-on-error`), maintains one self-healing GitHub Issue for sources failing ≥ `HEALTH_FAIL_THRESHOLD` (default 3). Uses built-in `GITHUB_TOKEN`. |
| Wayback archiving | inline in `build.yml` | **Live**, best-effort, only when a push occurred. |
| Role classification | `classify_items.py`, `roles.yml` | **Built but has produced no output yet.** See below. |
| Curated role feeds | `build_role_feeds.py` | **Built, not yet publishing** (depends on classification output). |

### Classification / role-feed status — objective findings

- **No state file currently carries role tags.** `grep -l '"roles"'
  data/state/*.json` returns 0 of 103 files. This means the classification
  step has not yet written any output into state.
- The CI classification step is **gated**: in `build.yml` it runs only
  `if: ${{ env.OPENAI_API_KEY != '' }}`. With no `roles`/`roles_v` fields
  present in any state file, the gate has evidently not been satisfied in a
  build (i.e. no successful `classify_items.py --backend openai` run has
  committed results). This repository checkout cannot read GitHub secret
  values, so the presence/absence of the secret itself is not asserted here —
  only the observable absence of role tags in committed state.
- Because no items are tagged, `build_role_feeds.py` would currently produce
  empty per-role feeds; role feeds are therefore **not yet meaningfully
  published**.
- Configuration knobs (in `build.yml` job `env`): `OPENAI_API_KEY`
  (repo secret), `OPENAI_MODEL` (repo variable, default `gpt-5.5`).
  Classifier also honors `OPENAI_BASE_URL` (default
  `https://api.openai.com/v1`).
- The classifier has two backends: `openai` (default, needs the key) and
  `heuristic` (offline keyword rules, deterministic, no key). The `heuristic`
  backend was used only for local prototype validation; the CI step always
  invokes `--backend openai`.

### Role taxonomy (`roles.yml`)

`taxonomy_version: 1`. 14 roles; each has `id`, `label`, `description`
(injected verbatim as the classifier prompt), and optional `keywords` /
`categories` (used only by the heuristic backend). Bumping
`taxonomy_version` forces full re-classification on the next run.

Role ids: `real-estate-title`, `estate-probate`, `bankruptcy`,
`business-corporate`, `employment-labor`, `civil-litigation-appellate`,
`criminal-defense`, `family-law`, `consumer-protection`, `tax`,
`securities-financial`, `healthcare-human-services`,
`environmental-land-use`, `professional-responsibility`.

## Per-source state schema (`data/state/<source-id>.json`)

One file per source, keyed by source `id`. Not documented elsewhere.

Top-level fields (observed across all 103 files):

| Field | Meaning |
| --- | --- |
| `id` | Source id (matches the filename stem and the `sources.yml` entry). |
| `last_checked` | ISO timestamp of the most recent fetch attempt. |
| `last_status` | Outcome of the last attempt (e.g. HTTP status / status label). |
| `last_success` | ISO timestamp of the last successful fetch. |
| `last_failure` | ISO timestamp of the last failed fetch. |
| `last_error` | Error string from the last failure (truncated). |
| `consecutive_failures` | Integer; drives the health watcher (`≥ threshold` → alert). Reset to 0 on success. |
| `content_hash` | Fingerprint used by `page_monitor` sources to detect change. |
| `items` | List of harvested items (see below). |

Per-item fields (base):

| Field | Meaning |
| --- | --- |
| `id` | Stable per-item id within the source. |
| `link` | Item URL. |
| `title` | Item title. |
| `summary` | Cleaned plain-text body. |
| `published` | Item publish timestamp (source-provided or derived). |
| `first_seen` | ISO timestamp the build first recorded this item (drives feed ordering). |

Per-item fields **added by classification** (absent until a classify run
writes them):

| Field | Meaning |
| --- | --- |
| `summary_html` | Cleaned HTML body (some items; write-time guards heal legacy items). |
| `roles` | `list[str]` of assigned role ids (may be empty = evaluated, no role). |
| `roles_v` | Integer taxonomy_version the `roles` were computed under; items whose `roles_v` ≠ current version are re-queued. |

Note: `page_monitor` "Monitoring started: …" and bare "Page updated: …"
markers are deliberately excluded from role feeds by
`classify_items.py:is_role_eligible()` and cached with an empty `roles` list.

## Intentionally-excluded / non-alerting sources

Five sources carry `health_ignore: true` in `sources.yml`. They are expected
to keep failing because the host blocks automated access; the health watcher
skips them so they do not generate a permanent alert. Do **not** treat these
as bugs or attempt to bypass the block:

| id | Name |
| --- | --- |
| `rod-aroostook` | Aroostook County Registry of Deeds |
| `rod-penobscot` | Penobscot County Registry of Deeds |
| `rod-washington` | Washington County Registry of Deeds |
| `probate-aroostook` | Aroostook County Probate Court |
| `probate-washington` | Washington County Probate Court |

## Scripts inventory

| Script | Role |
| --- | --- |
| `build_feeds.py` | Fetch all sources, write `docs/` + update `data/state/`. Never raises on a single-source failure. |
| `validate_feeds.py` | CI gate. Asserts generated feeds are well-formed; writes `docs/status.html`. Non-zero exit only on malformed output / broken `sources.yml`. |
| `classify_items.py` | Multi-label role classifier (incremental, cached, fail-soft). Backends: `openai`, `heuristic`. |
| `build_role_feeds.py` | Pool tagged items across sources → per-role curated feeds + `docs/opml/curated-roles.opml`. |
| `health_watch.py` | Reconcile the single source-health tracking issue. |
| `discover_feeds.py` | Maintainer utility: probe a page for advertised feeds / selector hints. |
| `migrate_scrub_state.py` | One-time, idempotent migration to re-clean legacy stored item bodies. |
| `common.py` | Shared helpers (fetching, robots.txt handling, state I/O, `load_sources`, `STATE_DIR`). |

## Secrets & variables referenced by CI

| Name | Kind | Used by | Default if unset |
| --- | --- | --- | --- |
| `GITHUB_TOKEN` | built-in | build commit/push, `health_watch.py` | always present |
| `OPENAI_API_KEY` | repo secret | classification step (gates the whole step) | step skipped |
| `OPENAI_MODEL` | repo variable | classifier | `gpt-5.5` |
| `OPENAI_BASE_URL` | env (not set in CI) | classifier | `https://api.openai.com/v1` |

## Local build note

On some Debian/Ubuntu hosts a bare `pip install` fails on a setuptools
`install_layout` quirk. Use a virtualenv and its interpreter explicitly
(`.venv/bin/python`). CI uses a plain `pip install` on `ubuntu-latest`
without issue. (Also in `AGENTS.md`.)
