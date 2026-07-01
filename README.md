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
3. Runs `scripts/validate_feeds.py`, which writes `docs/status.html` and
   fails the job only for build-breaking problems (malformed XML/JSON, a
   broken `sources.yml`, an OPML entry with no matching feed file) — not
   for an individual government site being temporarily down.
4. Commits and pushes any changed files in `docs/` and `data/state/` back
   to the repository.

GitHub Pages is configured (Settings → Pages) to publish from the `main`
branch, `/docs` folder, so every push from the workflow republishes the
site automatically — no separate deploy step is needed.

## Limitations

- **No native feeds for most Maine state sites.** Maine.gov properties
  generally do not publish RSS/Atom feeds (confirmed against Maine's own
  RSS directory), so most sources here are HTML-scraped using CSS
  selectors, with page-fingerprint monitoring as a fallback. Selectors can
  break silently when a government site is redesigned; check
  `docs/status.html` periodically and watch for sources stuck on
  "selectors/parser returned no items" notes in the build log.
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
is back up.

## Maintenance notes

- State files in `data/state/` are the only "memory" the build has between
  runs; deleting one resets that source's de-duplication history (it will
  re-emit recent items as if new on the next run).
- `MAX_ITEMS_PER_FEED` (50) and request timeout/retry settings live in
  `scripts/common.py`.
- The site base URL used inside generated feeds is controlled by the
  `SITE_BASE_URL` environment variable (defaults to the GitHub Pages URL
  for this repo) — set it locally if testing under a different domain.
- This project sends a descriptive `User-Agent` identifying itself and
  this repository on every request, and checks `robots.txt` before
  fetching each source.

## License

MIT — see [`LICENSE`](LICENSE).
