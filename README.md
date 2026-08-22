# Maine Government Feeds

Unofficial, free, static RSS/Atom/JSON feeds that monitor publicly available
Maine government, court, registry, legislative, and legal-agency webpages —
built for a Maine law practice that wants a single feed reader view of
Maine Judicial Branch opinions, AG/SoS/Governor notices, agency bulletins,
county Registry of Deeds pages, and Maine-related federal court activity.

Everything here is generated automatically by GitHub Actions and published
on GitHub Pages from this repository's `docs/` folder. There is no server,
no database, no paid API, and no login — it is a static site rebuilt every
six hours.

**Live site:** https://bedardandy.github.io/maine-government-feeds/
**Feed directory:** [`docs/index.html`](docs/index.html)
**Health dashboard:** [`docs/status.html`](docs/status.html)
**OPML (import this into a feed reader):** [`docs/opml/maine-government-feeds.opml`](docs/opml/maine-government-feeds.opml)

## Disclaimer

**This is not legal advice and not an official government publication.**
These feeds are an unofficial convenience layer over public Maine and
federal government webpages. Sources may lag the live page, may
misparse a page after a website redesign, or may be temporarily
unavailable. **Always verify anything important against the official
source URL** linked in each feed item before relying on it for a filing
deadline, a court date, a legal opinion, or any other consequential
decision. This project has no affiliation with the State of Maine, the
Maine Judicial Branch, or any federal court.

## What this is

- `sources.yml` lists every monitored page: its category, its URL, and
  either its native RSS/Atom feed URL or the CSS selectors used to extract
  "what's new" from the HTML page.
- `scripts/build_feeds.py` fetches every source, detects new/changed
  content, and writes:
  - RSS 2.0 feeds to `docs/feeds/rss/<source-id>.xml`
  - Atom feeds to `docs/feeds/atom/<source-id>.xml`
  - JSON Feed (jsonfeed.org v1.1) files to `docs/feeds/json/<source-id>.json`
  - iCalendar files to `docs/calendar/<source-id>.ics` plus a combined
    `docs/calendar/all-feeds.ics` (one all-day event per dated item — see
    "Subscribing to the calendars" below)
  - a combined catalog at `docs/feeds/json/catalog.json`
  - a master OPML file grouped by category at
    `docs/opml/maine-government-feeds.opml`
  - a human-browsable directory at `docs/index.html`
- `scripts/validate_feeds.py` checks that every generated file is
  well-formed and writes `docs/status.html`, a dashboard showing each
  source's last success/failure, HTTP status, item count, and notes.
- A GitHub Actions workflow (`.github/workflows/build.yml`) runs the build
  and validation every 6 hours and on manual dispatch, and commits the
  regenerated `docs/` and `data/state/` files back to the repository, which
  GitHub Pages then serves.

## Start here: combined and curated feeds

Most subscribers should NOT import all 100+ per-source feeds. Pick one of
these instead (all URLs relative to the live site above):

- **Daily digest** — `feeds/rss/daily-digest.xml`: one feed item per
  completed day summarizing everything new that day, grouped by category.
  The lowest-noise option; route it to a distribution list or Teams channel.
- **Everything** — `feeds/rss/all.xml`: every item from every source in one
  feed (newest first, capped at 200). Items carry `<category>` tags with the
  source category/subcategory, so rules and flows can filter on them.
- **Per-category combined feeds** — `feeds/rss/category-<slug>.xml`: one
  feed per category (e.g. `category-maine-judicial-branch.xml`,
  `category-registries-of-deeds.xml`). Listed on the site index.
- **Curated practice-area feeds** — `feeds/rss/role-<id>.xml`: cross-source
  feeds filtered to one practitioner role (real-estate-title,
  estate-probate, bankruptcy, employment-labor, …) defined in `roles.yml`.
  Items are classified by an LLM when an API key is configured
  (`ANTHROPIC_API_KEY`, preferred, or `OPENAI_API_KEY`), otherwise by an
  offline keyword heuristic — the feeds always publish either way, and
  `docs/opml/curated-roles.opml` imports all of them at once.

## Item bodies (full text) and history beyond 50 items

**Item bodies.** Items carry as much of the underlying document's text as
can be extracted responsibly, in three tiers:

1. Sources with a native RSS feed pass through the upstream summary/body.
2. HTML-scraped items (most court/agency listings) used to carry only a
   title and link; the build now fetches each **newly observed** item's
   linked page once and stores the main content region's text (and, for
   PDF links like slip opinions, text extracted from the first few pages
   via `pypdf`). Extraction is best-effort: robots.txt is honored, the
   descriptive User-Agent is sent, at most 10 documents are fetched per
   source per run, each item is fetched only once ever, and a failed or
   blocked fetch simply leaves that item as title+link. Word/Excel and
   other binary links are not fetched. Disable per source with
   `enrich: false` in `sources.yml` or globally with `FEED_ENRICH=0`.
3. Page-monitor items carry the added/removed text diff excerpt.

Always verify against the linked official document — extracted text is a
convenience copy, not the authoritative version.

**History.** Live feeds intentionally stay small (the 50 newest items) so
readers poll something light. Two complementary answers for "I want more
than 50":

- **Archive endpoint (publisher side):** every source also publishes an
  append-only JSON Feed at `feeds/archive/<source-id>.json` — everything
  the monitor has ever observed for that source (newest first, capped at
  500, listed in the catalog as `_archive_url`). New subscribers can
  backfill from it; it never rotates items out. The git history of this
  repository remains the unabridged record beyond the cap.
- **Retention (consumer side):** most feed readers keep items after they
  scroll off the feed — FreshRSS retains per its configured purge policy
  (set it to "keep forever"), classic Outlook keeps RSS messages like any
  mail, and a Power Automate flow writing items to a SharePoint list gives
  a permanent, searchable archive owned by the firm. If long-term
  retention matters to you, configure it in the consumer too — the archive
  endpoint is a backfill mechanism, not a substitute for your own records.

## For IT departments and automation

- **Machine-readable catalog**: `feeds/json/catalog.json` (schema v2) lists
  every feed with category, source type, health (`ok`/`failing`/`never`),
  item count, last-success timestamp, and all format URLs — enough to
  auto-provision subscriptions and skip unhealthy sources. A CSV twin lives
  at `feeds/json/catalog.csv` for Excel/import-wizard use.
- **Item metadata**: every RSS/Atom item carries `<category>` tags (source
  category, subcategory, and `role:<id>` practice-area tags); JSON Feed
  items carry the same in `tags`. Items linking directly to a PDF carry an
  `<enclosure type="application/pdf">` / JSON `attachments` entry.
- **Every item has a `pubDate`** (falling back to the first-observed time
  when the source page's date is unparseable), and same-day items get
  deterministically distinct timestamps — this matters for Power Automate's
  RSS trigger and RSS-to-email services, which skip items with missing or
  duplicate dates.
- **Power Automate recipe** (works in every M365 tenant, including New
  Outlook which dropped RSS support): create a flow with trigger *"When a
  feed item is published"* pointed at the digest (or any feed above), then
  add *"Send an email (V2)"* to a distribution list, *"Post message in a
  chat or channel"* (Teams), or *"Create item"* (SharePoint list).
- **RSS-to-email**: point Buttondown's or Mailchimp's RSS-to-email at the
  daily-digest feed. This project deliberately does not send email itself —
  a public static repo should not hold SMTP credentials or subscriber
  lists.
- **Push (WebSub)**: the Atom feeds advertise a public WebSub hub and the
  build pings it for changed feeds after each publish, so push-capable
  readers (Inoreader, FreshRSS with the WebSub plugin) see new items in
  seconds rather than on their next poll.
- **Provenance manifest**: `feeds/json/manifest.json` lists the SHA-256 and
  size of every published file per build — combined with git history,
  `_first_seen` stamps, and the Wayback snapshots, a verifiable record of
  when each item appeared.
- **Reliability envelope**: GitHub Pages serves with `Cache-Control:
  max-age=600` (not configurable) and no SLA, so subscribers can lag a push
  by up to ~10 minutes on top of the 6-hour build cadence. Poll no more
  often than the feeds' advertised `<ttl>360</ttl>`.

## Importing the OPML into Outlook

Classic Outlook (Windows) supports RSS feeds as mail folders:

1. Download [`docs/opml/maine-government-feeds.opml`](docs/opml/maine-government-feeds.opml)
   (or use the live link: `https://bedardandy.github.io/maine-government-feeds/opml/maine-government-feeds.opml`).
2. In Outlook: **File → Open & Export → Import/Export → Import RSS Feeds
   from an OPML file** (the exact menu path varies by Outlook version —
   in some versions it's **File → Account Settings → RSS Feeds → Import**).
3. Select the downloaded `.opml` file. Outlook will create an "RSS Feeds"
   folder tree grouped by the categories in this project (Maine Judicial
   Branch, Maine Legislature, Registries of Deeds, etc.).
4. New items will arrive as messages in those folders on Outlook's normal
   RSS sync schedule.

New Outlook and Outlook on the web do not support RSS subscriptions
directly; for those, subscribe to individual feed URLs from
`docs/index.html` in a separate reader (or use FreshRSS below and read it
in a browser).

## Subscribing with FreshRSS

1. In FreshRSS, go to **Subscription management → Import/export**.
2. Upload `maine-government-feeds.opml`, or paste the live URL:
   `https://bedardandy.github.io/maine-government-feeds/opml/maine-government-feeds.opml`
3. FreshRSS will create one folder per category and one feed per source.
4. FreshRSS polls each feed on its own schedule; since this project
   rebuilds every 6 hours, there's no benefit to polling more often than
   that.

Any other OPML-aware reader (Feedly, Inoreader, NetNewsWire, etc.) works
the same way — import the OPML URL above.

## Subscribing to the calendars

Every source is also published as an iCalendar (`.ics`) file, and all
sources are merged into one combined calendar:

- Combined: `https://bedardandy.github.io/maine-government-feeds/calendar/all-feeds.ics`
- Per source: `https://bedardandy.github.io/maine-government-feeds/calendar/<source-id>.ics`

Each dated feed item becomes an all-day event on its publication date (the
date scraped from the source page when parseable, otherwise the date this
build first observed the item). Subscribed in Outlook ("Add calendar →
Subscribe from web"), Google Calendar ("Other calendars → From URL"), or
Apple Calendar ("File → New Calendar Subscription"), this gives a
day-by-day timeline of when each opinion, order, bulletin, fee change, or
page update appeared — useful both for staying current and as an
approximate record of *when* something was published or changed.

**Legislative hearings are real timed events.** The
`leg-hearings-schedule` source pulls structured data from the
Legislature's own schedule endpoint (the same call its schedule page
makes in the browser — it only answers POST requests, which is why simple
scraping missed it). Each public hearing and work session becomes a feed
item and a *timed* calendar event with the committee, bill (LD/paper
number), room, and start time in Maine local time — so subscribing to
`calendar/leg-hearings-schedule.ics` puts upcoming hearings directly on
your calendar. An empty feed outside legislative sessions is normal.

**Page-change items include a diff excerpt.** When a page-monitored
source (fee schedules, registry pages, rule pages, etc.) changes, the
"Page updated" item now includes an added/removed text excerpt showing
what actually changed, not just that something did.

**Page changes are debounced across two runs.** A page-monitored source
emits a "Page updated" item only after the same new content is observed on
two consecutive builds. Several government sites alternate between page
variants (rotating navigation chrome, transient search-widget states) or
intermittently serve anti-bot challenge pages; single-run fingerprinting
turned that into a junk item every six hours. Debouncing suppresses all of
it, at the cost of real changes appearing one build cycle (~6 hours) later.
Anti-bot interstitials ("please wait while your request is being
verified...") are additionally detected and treated as failed fetches
rather than content.

**Third-party timestamping.** After each build that publishes changes,
the workflow asks the Internet Archive's Save Page Now to snapshot the
feed directory, catalog, and combined calendar (best-effort; never fails
the build). Together with the git commit history, this gives an
externally verifiable record of when each item appeared.

Two provenance timestamps are kept for every item and exposed in the JSON
feeds: `date_published` (best-effort publication date) and `_first_seen`
(when this monitor first observed the item, never rewritten afterward).
Because every build is committed to git, the repository history itself is
a tamper-evident record of when each item first appeared.

## Item filters (granular feeds from broad sources)

Some upstream feeds/pages are broader than the topic a source represents
(e.g. the Governor's office publishes one sitewide RSS feed). A source may
declare a `filters` block of case-insensitive regexes applied to each
parsed item:

```yaml
  filters:
    include_title: standing order     # keep only matching titles
    exclude_title: ...
    include_link: /official_documents/  # keep only matching URLs
    exclude_link: ...
```

This is how `jb-standing-orders` extracts only Standing Orders from the
Administrative Orders table, and how the Governor's sitewide feed is split
into executive orders/proclamations vs. press releases. If a filter
removes every item on a run, the source falls back to page-change
monitoring for that run (same as when selectors stop matching).

## Adding or editing a source

Open `sources.yml` and add an entry. Each source looks like one of these
two shapes:

**A page with a real RSS/Atom feed** (always prefer this when one exists):

```yaml
- id: "ca1-opinions"
  name: "U.S. Court of Appeals for the First Circuit — Opinions"
  category: "Federal Maine-Related"
  subcategory: "Circuit Court Opinions"
  url: "https://www.ca1.uscourts.gov/opn"
  type: "native_rss"
  rss_url: "https://www.ca1.uscourts.gov/opn/feed"
  notes: "Maine cases appeal to the First Circuit."
```

**A page with no feed, parsed with CSS selectors:**

```yaml
- id: "sjc-opinions"
  name: "Maine Supreme Judicial Court (Law Court) — Published Opinions"
  category: "Maine Judicial Branch"
  subcategory: "Supreme Judicial Court Opinions"
  url: "https://www.courts.maine.gov/courts/sjc/opinions.html"
  type: "html"
  selectors:
    item: "table tr"      # one CSS selector match per list item
    title: "td:nth-of-type(2) a"
    link: "td:nth-of-type(2) a"
    date: "td:nth-of-type(3)"
  notes: "Table of opinions, newest first."
```

If a page has no clean repeating list (e.g. a single prose paragraph that
occasionally changes), omit `selectors` and set `type: "page_monitor"`.
The build script will hash the page's visible text and emit a single
"page changed" item whenever that hash changes — useful as a fallback so
nothing is silently un-monitored, even if it can't produce rich,
per-item entries.

```yaml
- id: "rules-proposed"
  name: "Maine Judicial Branch — Proposed Rule Amendments"
  category: "Maine Judicial Branch"
  subcategory: "Rules amendments"
  url: "https://www.courts.maine.gov/rules/proposed.html"
  type: "page_monitor"
  notes: "No repeating list structure; page is prose that changes infrequently."
```

After editing `sources.yml`, run the build locally (see below) to confirm
your new source produces a valid feed before committing.

### How selectors work

- `item`: a CSS selector matching each "row" of the listing (e.g. a table
  row, or a list item). The build script iterates over every match.
- `title` / `link`: CSS selectors **relative to each item** for the title
  text and the link's `href`. If the item itself is the `<a>` tag, you can
  point `title`/`link` at the same selector, or omit them to fall back to
  the item's own text/href.
- `date` (optional): a CSS selector relative to each item whose text is
  parsed as a date (flexible/fuzzy parsing via `python-dateutil`). If
  omitted, or if parsing fails, the item is timestamped with the time it
  was first observed by the build.
- Links are normalized to absolute URLs automatically.
- If selectors stop matching anything (e.g. after a site redesign), the
  build automatically falls back to page-fingerprint monitoring for that
  run and logs a note — it does not fail the whole build.

Use `scripts/discover_feeds.py <url>` to check whether a page already has
a native feed before writing selectors, and to get a quick structural hint
(which repeating `<table>`/`<ul>` containers exist) for writing them.

## Running locally

```bash
git clone https://github.com/bedardandy/maine-government-feeds.git
cd maine-government-feeds
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python scripts/build_feeds.py      # fetches sources, writes docs/feeds, docs/opml, docs/index.html
python scripts/validate_feeds.py   # checks well-formedness, writes docs/status.html
```

Generated output lands in `docs/` and per-source state lands in
`data/state/*.json`. Both are checked into the repository so the build is
incremental (only new/changed items get added, and the last 50 items per
feed are kept) and so the health dashboard has history to report.

To check a single candidate URL before adding it to `sources.yml`:

```bash
python scripts/discover_feeds.py "https://www.courts.maine.gov/courts/sjc/opinions.html"
```

## How GitHub Actions publishes the feeds

`.github/workflows/build.yml` runs on a `workflow_dispatch` trigger (Actions
tab → "Run workflow") and on a `15 */6 * * *` cron schedule (every 6 hours).
Each run:

1. Installs the pinned Python dependencies.
2. Runs `scripts/build_feeds.py` against every enabled source in
   `sources.yml`, updating `data/state/*.json` and regenerating everything
   under `docs/`.
3. Runs `scripts/classify_items.py` (LLM backend if an API key is
   configured, offline keyword heuristic otherwise) and
   `scripts/build_role_feeds.py` for the curated practice-area feeds.
4. Exports `corpus.jsonl` (`scripts/write_corpus.py`) and uploads it as a
   90-day workflow artifact for firm knowledge pipelines — see "Firm
   knowledge-pipeline exports" below.
5. Runs `scripts/validate_feeds.py`, which writes `docs/status.html` and
   fails the job only for build-breaking problems (malformed XML/JSON, a
   broken `sources.yml`, an OPML entry with no matching feed file) — not
   for an individual government site being temporarily down.
6. Commits and pushes any changed files in `docs/` and `data/state/` back
   to the repository, then pings the WebSub hub and snapshots key URLs to
   the Wayback Machine (both best-effort).

GitHub Pages is configured (Settings → Pages) to publish from the `main`
branch, `/docs` folder, so every push from the workflow republishes the
site automatically — no separate deploy step is needed.

## Limitations

- **Few native feeds for Maine state sites.** Some maine.gov properties do
  publish RSS (`/sos/rss.xml`, `/governor/mills/rss.xml`, `/ag/newsrss`
  are used here; DEP publishes an RSS index page), but most do not, so
  most sources are HTML-scraped using CSS selectors, with page-fingerprint
  monitoring as a fallback. Selectors can break silently when a government
  site is redesigned; check `docs/status.html` periodically and watch for
  sources stuck on "selectors/parser returned no items" notes in the build
  log. See "Candidate sources to verify" below for native feeds worth
  probing.
- **No corporate-filings or UCC data feed.** Maine SoS bulk corporate and
  UCC data is only available through a paid InforME subscription; the
  public search apps are interactive-only. There is nothing this project
  can legitimately poll, so no such feed exists here.
- **Polling delay.** Updates are only as fresh as the last scheduled run
  (every 6 hours), not real-time.
- **Best-effort date parsing.** Dates scraped from HTML are parsed
  heuristically; when parsing fails, an item is timestamped with the time
  it was first observed instead of its true publication date.
- **Not exhaustive.** This covers a curated set of pages useful to a law
  practice, not every Maine government webpage.
- **No invented feeds.** Every entry in `sources.yml` is a real, verified,
  public URL. Where a native RSS feed doesn't exist, that's stated
  explicitly rather than guessed at.

## Candidate sources to verify

Researched but not yet added because the exact feed URL could not be
verified end-to-end (per the "no invented feeds" rule, nothing goes in
`sources.yml` without a confirmed 200 + parseable items — check each with
`scripts/discover_feeds.py`):

- **Maine.gov Public Meeting Calendar "Next 7 Days" RSS** (feed URL listed
  on maine.gov's RSS subscriptions page) — statewide board/commission
  meetings; would flow into the ICS calendars as timed events.
- **DOJ U.S. Attorney, District of Maine press releases** — no working RSS
  was found at the documented per-district paths (`justice.gov/usao-me/rss`
  returns 404 and the news page is client-rendered); revisit if DOJ
  restores district feeds.
- **Maine Commission on Public Defense Services rulemaking pages** — page
  URLs not yet confirmed stable.
- **Maine.gov GovDelivery topic feeds** — many agencies distribute notices
  via GovDelivery (`public.govdelivery.com/accounts/megov`); per-topic
  bulletin RSS may exist and could replace fragile HTML scrapes, but the
  topic catalog needs interactive probing.

Researched and ruled out (do not re-add without new information):

- **PACER CM/ECF public RSS for D. Me. / Bankr. D. Me.** — ADDED 2026-08
  (`fed-dme-cmecf-orders`, `fed-meb-cmecf-entries`); both verified live.
- **First Circuit oral-argument feed** — ADDED 2026-08
  (`fed-ca1-oral-arguments`).
- **Federal Register type-filtered feeds + public inspection** — ADDED
  2026-08 (`fed-register-maine-rules`, `-proposed`,
  `fed-register-public-inspection`).
- **DigitalMaine (bepress) collection feeds** — ADDED 2026-08
  (`digitalmaine-ag-docs`, `-puc-docs`, `-buc-docs`).
- **Maine DEP native RSS** — the feeds advertised on maine.gov/dep/social/rss.html
  all live under `/tools/whatsnew/`, which maine.gov's robots.txt
  disallows; the existing HTML scrape of the DEP news page remains the
  compliant path.

## Firm knowledge-pipeline exports

For firms that process these feeds programmatically (search/RAG intake,
analytics, alert routing) rather than reading them in an RSS client:

- **Structured item metadata** — every JSON Feed item carries a `_meta`
  extension when anything is extractable from its text:
  - `_meta.docket`: court docket numbers (state trial format
    `ANDSC-RE-2026-00123`, federal district `2:25-cv-00264`, bankruptcy/
    appellate short forms when the item reads as a case entry),
  - `_meta.ld`: Maine Legislative Document number,
  - `_meta.effective_date`: any stated effective date.
- **Cross-source deduplication** — combined/category feeds collapse the
  same document arriving via redundant publishers (CourtListener vs.
  govinfo vs. the court's own site) to one occurrence, keyed on docket +
  normalized title; the newest copy wins.
- **Minor-change tagging** — page-monitor change items whose entire diff
  excerpt is under ~120 characters carry `_minor_change: true` in JSON so
  pipelines can drop trivial chrome churn while keeping real changes.
- **Corpus artifact** — each build publishes `corpus.jsonl` (newline-
  delimited JSON of every live observed item: text, source, category,
  role tags, dates, extracted metadata) as a workflow artifact named
  `corpus` (90-day retention). Download via
  `gh run download --name corpus` or the Actions API; it is deliberately
  NOT committed, so multi-MB regenerations never bloat the repo or Pages.
  Cap: newest 3,000 items, bodies truncated to 2,500 characters — tune in
  `scripts/write_corpus.py`.

## Reporting a broken feed

If a feed has stopped updating, is misparsing items (e.g. duplicate or
garbled titles), or a source URL has moved, please
[open a GitHub Issue](https://github.com/bedardandy/maine-government-feeds/issues)
with:

- the source `id` from `sources.yml` (or the feed URL),
- what you expected vs. what you saw, and
- a link to the current state of the official source page, if it has
  moved.

Check `docs/status.html` first — if a source shows as failing there, the
next scheduled run may resolve it automatically once the government site
is back up. The dashboard also flags sources idle for more than ~6 months
as pruning candidates and reports per-source body-enrichment coverage.

## Maintenance notes

- State files in `data/state/` are the only "memory" the build has between
  runs; deleting one resets that source's de-duplication history (it will
  re-emit recent items as if new on the next run). Volatile health fields
  (last checked/failed, consecutive-failure counts) live in the combined
  snapshot `data/state/_health.json`, so an uneventful run no longer
  rewrites every per-source state file.
- Every fetch sends HTTP conditional validators (`If-None-Match` /
  `If-Modified-Since`) persisted from the previous response; a 304 answer
  counts as a successful verification with no transfer. Consecutive
  requests to the same host are spaced by `FEED_HOST_DELAY` seconds
  (default 2.0; set 0 to disable locally), and servers' `Retry-After`
  headers are honored on 429/503 — this spacing has un-blocked county WAFs
  that previously returned 403.
- `MAX_ITEMS_PER_FEED` (50) and request timeout/retry settings live in
  `scripts/common.py`.
- The site base URL used inside generated feeds is controlled by the
  `SITE_BASE_URL` environment variable (defaults to the GitHub Pages URL
  for this repo) — set it locally if testing under a different domain.
- This project sends a descriptive `User-Agent` identifying itself and
  this repository on every request, and checks `robots.txt` before
  fetching each source.
- `requirements.txt` pins exact dependency versions on purpose; bump them
  deliberately and re-run build + validate after any change.

## License

MIT — see [`LICENSE`](LICENSE).
