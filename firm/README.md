# Firm cache: local, searchable copy of the feeds

`firm_cache.py` keeps a copy of everything Maine Government Feeds publishes in
one SQLite file on a machine the firm controls, and makes it searchable two
ways: by keyword (SQLite FTS5) and by meaning (vector embeddings computed
locally). It also runs health checks you can wire into monitoring, and saved
searches that report only what is new.

- **Nothing confidential leaves the machine.** The only outbound traffic is
  HTTPS GETs of the public feed files, plus a one-time model download if you
  use the default embedding backend. Search queries and watchlist terms are
  never sent anywhere.
- **The cache only grows.** The published feeds keep the newest 50 items per
  source (500 in the archive feeds). Items that rotate out upstream stay in
  your cache.
- **Verified downloads.** Each feed file is checked against the SHA-256 the
  build recorded in `manifest.json`; a file that does not match is not
  ingested and `check` reports it as critical.

## Install

Python 3.10 or newer. On Linux:

```bash
sudo useradd --system --home /var/lib/maine-feeds --create-home mainefeeds
sudo mkdir -p /opt/maine-feeds && sudo cp firm_cache.py requirements.txt /opt/maine-feeds/
sudo python3 -m venv /opt/maine-feeds/venv
sudo /opt/maine-feeds/venv/bin/pip install -r /opt/maine-feeds/requirements.txt
```

On Windows Server, install Python from python.org, then in PowerShell:

```powershell
py -m venv C:\maine-feeds\venv
C:\maine-feeds\venv\Scripts\pip install -r requirements.txt
```

`fastembed` (in `requirements.txt`) provides the local embedding model. It
runs on CPU; no GPU is needed for a corpus this size. If you leave it out, the
tool still works with keyword search only.

## First run

```bash
export MGF_DB=/var/lib/maine-feeds/firm_cache.db
python firm_cache.py sync      # about 120 small files; embeds new text when done
python firm_cache.py check
python firm_cache.py search "shoreland zoning setback"
```

The first `sync` downloads the embedding model (about 70 MB for the default
`BAAI/bge-small-en-v1.5`) from Hugging Face and caches it; set
`MGF_MODEL_DIR` to choose where. Later syncs use conditional requests, so an
unchanged file costs one `304` and no transfer.

## Commands

| Command | What it does |
|---|---|
| `sync` | Pull the catalog and every source's archive feed, verify, upsert, then embed new or changed text. `--from-dir docs/` reads a local checkout or mirror instead of the network; `--corpus corpus.jsonl` also merges a CI corpus export; `--live-only` reads the 50-item live feeds. Exits 1 if any file failed. |
| `search "<query>"` | `--mode hybrid` (default), `vector` or `keyword`. Filters: `--source`, `--category`, `--role`, `--since 2026-10-01`, `--docket`, `--ld`, `--kind item\|page-change`. `--json` for scripts. |
| `check` | Health checks, below. Exit code 0 ok, 1 warning, 2 critical. `--json` for monitoring systems, `--quiet` to print only problems. |
| `watch <file.yml>` | Print items that match a saved search for the first time. Prints nothing when there is nothing new, so cron mails only real hits. `--baseline` records current matches silently. |
| `serve` | Search page and JSON API on `http://127.0.0.1:8765`. |
| `embed` | Embed anything not yet embedded. `--reindex` re-embeds everything with a different backend or model. |
| `stats` / `repair` | What the cache holds / rebuild the keyword index. |

### Search modes

- **keyword**: exact words, ranked by BM25. Best for docket numbers, statute
  cites, names.
- **vector**: nearest passages by embedding similarity. Finds "landlord" when
  the document says "lessor".
- **hybrid**: both lists merged by reciprocal rank fusion. The default.

Results show `sim` (cosine similarity, 0 to 1) when the vector leg matched.

### Checks

| Check | Warning | Critical |
|---|---|---|
| database integrity | | SQLite reports corruption |
| keyword index | | index and item counts differ |
| last sync | older than 13 h | older than 48 h, or never |
| last sync errors | a file failed to download | a file failed SHA-256 verification |
| upstream build | catalog older than 18 h | older than 72 h |
| upstream source health | some sources failing upstream | more than half failing |
| new items | nothing new in 7 days | |
| vector index | passages not embedded | |

"Upstream build" is the one to alert on: it turns critical when the public
feeds themselves have stopped being rebuilt, which no amount of local syncing
can fix. Thresholds are flags (`--build-warn-hours` and so on).

### Watchlist

See `watchlist.example.yml`. A watch can combine exact `keywords`, a
plain-language `query` matched by meaning (with a `min_similarity` floor), a
`docket`, an `ld` number, and source/category/role filters. Each item is
reported once per watch.

A real watchlist can identify clients and matters. Keep it outside the
repository, readable only by the service account.

## Scheduling

Linux: copy the units in `deploy/` to `/etc/systemd/system/`, then

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now maine-feeds-sync.timer maine-feeds-serve.service
```

The timer runs `sync` then `check` shortly after each upstream build (every
six hours). The unit fails only on a critical check, so
`systemctl --failed`, an `OnFailure=` hook or your monitoring agent sees it.

Windows: one scheduled task every six hours running
`C:\maine-feeds\venv\Scripts\python firm_cache.py sync`, a second running
`check`, with "if the task fails" alerting on a non-zero exit.

Plain cron works too:

```cron
5 1,7,13,19 * * *  cd /opt/maine-feeds && venv/bin/python firm_cache.py sync >/dev/null; venv/bin/python firm_cache.py check --quiet
20 7 * * 1-5       cd /opt/maine-feeds && venv/bin/python firm_cache.py watch /etc/maine-feeds/watchlist.yml
```

Both lines print only when there is something to act on (`check --quiet`
prints just the checks that are not OK), so cron's mail is the alert.

## Web page and API

`serve` binds to localhost. It has no authentication and no TLS, so to offer
it on the office network put it behind the firm's reverse proxy or SSO rather
than binding it to a public interface. Access logs record the path only, not
the search terms.

| Endpoint | Returns |
|---|---|
| `GET /` | search page |
| `GET /api/search?q=…&mode=&source=&category=&role=&since=&docket=&ld=&kind=&limit=` | `{"results": [...]}` |
| `GET /api/health` | the checks as JSON; HTTP 503 when critical |
| `GET /api/sources` | source list, item count, last sync time |

## Embedding backends

Set with `--embed-backend` / `MGF_EMBED_BACKEND` the first time; after that
`auto` keeps using whatever the cache was built with, so a query is never
compared against vectors from a different model.

| Backend | Use when |
|---|---|
| `fastembed` (default when installed) | You want it self-contained. `--embed-model` picks any model fastembed supports. |
| `http` | You already run an embedding server: Ollama, llama.cpp, LM Studio, text-embeddings-inference. Any OpenAI-compatible `/v1/embeddings` URL via `--embed-url` (default is Ollama on localhost), model name via `--embed-model`. |
| `hashing` | Air-gapped box with no model. Lexical only: it matches shared words, not meaning, and hybrid search falls back to keyword ranking. |

To switch: `firm_cache.py --embed-backend http --embed-model nomic-embed-text embed --reindex`.

Pointing the `http` backend at a hosted embedding API would send feed text and
your search queries to that provider. The feed text is public; the queries are
not.

## Data model

One SQLite file (WAL mode). Back it up with `sqlite3 firm_cache.db ".backup
copy.db"` or by copying it while the service is stopped.

| Table | Contents |
|---|---|
| `items` | one row per published item: `guid`, `url`, `title`, `content_text`, `date_published`, `first_seen`, `source_id`, `category`, `roles` (JSON), `dockets`, `ld`, `effective_date`, `kind`, `first_ingested`, `last_changed` |
| `items_fts` | FTS5 index over title and text |
| `chunks`, `vectors` | passages of about 1,200 characters and their embeddings (float32), keyed by model |
| `sources` | upstream catalog: name, category, health, last success |
| `sync_runs` | history of syncs, with upstream build time and errors |
| `watch_hits` | which item matched which watch, and when |

Query it directly when that is easier:

```sql
SELECT title, url FROM items
WHERE source_id = 'jb-sjc-opinions' AND first_seen > date('now', '-7 days');
```

Vector search is an exact scan held in memory. At this corpus's size (a few
thousand passages, growing by tens per day) that takes milliseconds; a
dedicated vector index becomes worth it somewhere past a few hundred thousand
passages.

## Limits

- Item text is what the feeds publish: the first few thousand characters of
  an opinion or notice, not the whole PDF. Search finds the document; read
  the source for the full text.
- This is a monitoring aid. It is not a substitute for official notice or
  for checking the source itself. A source that fails upstream stops
  producing items, which `check` reports under "upstream source health".
