#!/usr/bin/env python3
"""
Maintainer utility: given a page URL, report whether it already has a
native RSS/Atom feed (via <link rel="alternate"> autodiscovery or common
feed paths), and otherwise print a quick structural summary to help write
CSS selectors for sources.yml.

Usage:
    python scripts/discover_feeds.py https://www.courts.maine.gov/courts/sjc/opinions.html
"""
from __future__ import annotations

import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common import make_client

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
COMMON_FEED_PATHS = ("/feed", "/feed/", "/rss.xml", "/rss", "/atom.xml", "/index.xml")


def find_autodiscovered_feeds(soup: BeautifulSoup, base_url: str) -> list[str]:
    found = []
    for link in soup.find_all("link"):
        if link.get("type") in FEED_TYPES and link.get("href"):
            found.append(urljoin(base_url, link["href"]))
    return found


def probe_common_paths(base_url: str, client) -> list[str]:
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    found = []
    for path in COMMON_FEED_PATHS:
        url = urljoin(root, path)
        try:
            resp = client.get(url, timeout=10.0, follow_redirects=True)
            if resp.status_code == 200 and (
                "xml" in resp.headers.get("content-type", "") or "rss" in resp.text[:200].lower()
            ):
                found.append(url)
        except Exception:
            continue
    return found


def summarize_structure(soup: BeautifulSoup) -> None:
    candidates = []
    for tag in ("table", "ul", "ol"):
        for el in soup.find_all(tag):
            rows = el.find_all("tr") if tag == "table" else el.find_all("li")
            if len(rows) >= 3:
                classes = " ".join(el.get("class", [])) or "(no class)"
                candidates.append((tag, classes, len(rows)))
    if not candidates:
        print("No obvious repeating list/table structure found (>=3 rows/items).")
        print("This page may need page-fingerprint monitoring instead of item selectors.")
        return
    print("Candidate repeating containers (tag, class, item count):")
    for tag, classes, count in candidates[:10]:
        print(f"  <{tag} class=\"{classes}\">  -> {count} child rows/items")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 1
    url = sys.argv[1]
    client = make_client()
    try:
        resp = client.get(url, timeout=20.0, follow_redirects=True)
    except Exception as exc:
        print(f"Could not fetch {url}: {exc}", file=sys.stderr)
        return 1

    print(f"Fetched {url} -> HTTP {resp.status_code}")
    soup = BeautifulSoup(resp.text, "lxml")

    auto = find_autodiscovered_feeds(soup, str(resp.url))
    common_paths = probe_common_paths(str(resp.url), client)

    if auto or common_paths:
        print("\nNative feed(s) found:")
        for f in dict.fromkeys(auto + common_paths):
            print(f"  {f}")
        print("\n-> Use type: native_rss with rss_url set to one of the above in sources.yml.")
    else:
        print("\nNo native RSS/Atom feed autodiscovered.")
        print("-> Use type: html with CSS selectors, or type: page_monitor as a fallback.\n")
        summarize_structure(soup)

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
