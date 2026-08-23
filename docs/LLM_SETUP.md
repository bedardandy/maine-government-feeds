# LLM setup: classification and the Codex maintenance watcher

Two optional LLM features plug into this repo. Both fail soft — everything
publishes without them — but both get smarter once a key exists.

---

## 1) Practice-area classification (the role feeds)

Every item is tagged into practitioner roles (`roles.yml`) by
`scripts/classify_items.py`. Backend selection, first available wins:

| Backend | Needs | Notes |
|---|---|---|
| Anthropic | secret `ANTHROPIC_API_KEY` (+ optional variable `ANTHROPIC_MODEL`) | |
| OpenAI | secret `OPENAI_API_KEY` (+ optional variable `OPENAI_MODEL`) | |
| Heuristic | nothing | offline keyword fallback; always present |

### Setup (no code changes required)

1. **Settings → Secrets and variables → Actions → Secrets**: add
   `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`).
2. **Variables** tab: optionally set `OPENAI_MODEL` to pin the model.
   Any model id works — point it at a small inexpensive one (e.g. your
   preferred mini/luna-tier model); classification is high-volume,
   short-output work where a small model does well.
3. That's it. The next scheduled build classifies with the LLM; tags are
   cached in `data/state/`, so only new items are billed each run.

Cost shape: ~20-item batches, truncated inputs (500-char summaries),
one call per batch per build — pennies per day on a small model.

---

## 2) The Codex maintenance watcher

`.github/workflows/feeds-watcher.yml` runs on weekday mornings **only when
sources are actually failing**, and:

1. Pre-fetches the worst-failing source pages into `_watcher/`
   (`scripts/watcher_report.py` — robots-honoring, descriptive UA).
2. Runs the Codex CLI **offline** against that report with a small model
   and asks it to propose selector/URL fixes in `sources.yml`.
3. Opens ONE pull request for human review. The watcher can never push to
   `main`, never touches state files or generated docs, and cannot invent
   workarounds for bot-blocked pages (the prompt forbids it).

Hard rails: agent sandbox is `read-only` (it edits via its response
channel only), diff must be within `sources.yml`, CI validates any
proposal before merge, and PRs are labeled for review.

### Setup

1. Add secret `OPENAI_API_KEY` (same key as classification — one secret
   powers both).
2. Optional: set repo variable `WATCHER_MODEL` to your small-model id
   (defaults to `gpt-4o-mini`; point it at whatever luna/mini-tier model
   you prefer). Keep it cheap — this job is bounded (≤5 sources/run) but
   runs up to five days a week.
3. Nothing else. Silence means healthy feeds; a `maintenance`-labeled PR
   means the watcher found something worth reviewing.

### Why not let an external Codex cloud task drive this repo?

It works too (assign recurring tasks from ChatGPT/Codex to the repo), but
the repo-native workflow keeps scheduling, credentials, sandboxing, and
the PR path inside version control — no external service can commit
state-file churn onto the 6-hour build's critical path, and every change
arrives as an ordinary reviewed PR. Use the cloud-agent route for ad-hoc
investigations; keep this workflow for the routine loop.

### Local dry-run

```bash
python scripts/watcher_report.py --out-dir _watcher   # inspect report.md
# then simulate the agent step yourself against _watcher/
```

---

## Removing either feature

Delete the secret(s): classification falls back to the heuristic backend
and the watcher exits early with "nothing failing" / no-key notices. No
code changes needed.
