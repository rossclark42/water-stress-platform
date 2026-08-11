"""
13_discover_ground_truth_sources.py

Phase 2, ground-truth discovery step (precedes the actual extraction
pipeline). Uses GOV.UK's public Search API (documented, free, no key —
https://www.gov.uk/api/search.json) to programmatically find EVERY
Environment Agency publication relevant to area drought-status ground
truth for both validation years, rather than relying on manual web
search (which was found, live, to mix up 2022/2023/2026 results due to
near-identical report titles/language across years).

Two source types, per the finding this session:
  - 2022: no weekly HTML report series existed yet (it started July 2025)
    — ground truth instead comes from individual GOV.UK news articles,
    each documenting a specific area status-change event (e.g. "More
    areas in England declared in drought after record dry July").
  - 2025 (and 2026): a continuous weekly "Dry weather and drought in
    England" HTML report series exists, one page per week, with an
    explicit area-status-by-stage section every time.

This script queries for both, and writes a combined CSV for manual
review BEFORE building the actual extraction/LLM step — so we can
confirm coverage looks complete (a full drought season's worth of
status-change events for 2022/2018; a full run of weekly reports for
2025) before trusting it at scale, per this project's established
discipline.

FUTURE-PROOFING: the weekly-report collection page(s) are discovered
dynamically (via search, not a hardcoded URL) — see
find_current_collection_pages(). This means re-running this script in
2026, 2027, or any future year should automatically pick up whatever
collection page(s) EA is currently publishing to, including a fresh one
started for a new drought season, without needing anyone to edit a URL.
This same discovery logic is what a future Phase 3/4 scheduled n8n
pipeline would reuse for live ground-truth ingestion, not just this
one-off historical backfill — see DECISIONS.md.

Run from inside python/.
Requires: requests
"""

import csv
import re
import time

import requests

SEARCH_API_BASE = "https://www.gov.uk/api/search.json"
GOVUK_BASE = "https://www.gov.uk"

# Known collection pages as of this session -- kept only as a fallback
# seed if dynamic discovery (below) finds nothing, e.g. if GOV.UK's search
# index is temporarily unavailable. NOT relied on as the primary source,
# since these will go stale: EA has already been observed spinning up a
# new collection page per drought season (a 2025-2026 one and a separate
# 2026-only one both exist), and there is no guarantee about what a 2027+
# page will be named.
FALLBACK_COLLECTION_PAGES = [
    "https://www.gov.uk/government/publications/dry-weather-and-drought-in-england-summary-reports",
    "https://www.gov.uk/government/publications/dry-weather-and-drought-in-england-2026-summary-reports",
]


def find_current_collection_pages():
    """
    Dynamically discover whichever 'Dry weather and drought in England...
    summary reports' collection page(s) currently exist.

    Primary method: probe predictable URLs directly (HTTP HEAD, checking
    for 200 vs 404) using the exact naming pattern already confirmed
    twice this session -- a base slug with no year, plus a year-suffixed
    variant (e.g. "...-2026-summary-reports"). This replaced an earlier
    search-API-based approach that was tried twice (with and without a
    format filter) and never found the collection page even once,
    including in a completely unfiltered pass -- GOV.UK's own docs note
    a newer search-api-v2 exists with "no equivalent publicly available
    endpoint", so Whitehall "finder" collection pages like this one may
    simply not be well-indexed in the public search API being used here.
    Direct URL probing sidesteps that entirely.

    Checks the current year plus one year either side, to catch a page
    created slightly before/after a year boundary. Falls back to a
    broad, unfiltered search as a second layer only if no probed URL
    exists -- in case EA genuinely changes the naming pattern in some
    future year.
    """
    current_year = int(time.strftime("%Y"))
    candidate_slugs = ["dry-weather-and-drought-in-england-summary-reports"]
    for year in range(current_year - 1, current_year + 2):
        candidate_slugs.append(
            f"dry-weather-and-drought-in-england-{year}-summary-reports")

    found = []
    for slug in candidate_slugs:
        url = f"{GOVUK_BASE}/government/publications/{slug}"
        try:
            resp = requests.head(url, timeout=10, allow_redirects=True)
            if resp.status_code == 200:
                found.append(url)
        except requests.RequestException:
            pass
        time.sleep(0.2)

    if found:
        return sorted(set(found))

    # Fallback: broad unfiltered search, in case the naming pattern has
    # genuinely changed in a way URL-probing can't anticipate.
    results = search_gov_uk(
        query="dry weather and drought in England summary reports",
        organisation="environment-agency",
        date_from="2015-01-01",
        date_to=time.strftime("%Y-%m-%d"),
        formats=[],
        count=100,
    )
    collection_urls = set()
    for r in results:
        title = (r.get("title") or "").lower()
        link = r.get("link", "")
        if "summary reports" not in title:
            continue
        path_parts = [p for p in link.split("/") if p]
        if len(path_parts) == 3 and path_parts[0] == "government" and path_parts[1] == "publications":
            collection_urls.add(GOVUK_BASE + link)

    return sorted(collection_urls)


def search_gov_uk(query, organisation, date_from, date_to, formats, count=100):
    params = {
        "q": query,
        "filter_organisations": organisation,
        "filter_public_timestamp": f"from:{date_from},to:{date_to}",
        "count": count,
        "order": "public_timestamp",
    }
    # filter_format can be repeated for an OR match across formats
    resp = requests.get(
        SEARCH_API_BASE,
        params=list(params.items()) + [("filter_format", f) for f in formats],
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def fetch_weekly_report_list(collection_url):
    """
    Parse a collection page's markdown-rendered content for its "Documents"
    section links -- each one is a direct URL to one weekly report, with
    the date range in its title.
    """
    resp = requests.get(collection_url, timeout=15)
    resp.raise_for_status()
    html = resp.text

    # Links to individual reports follow the pattern:
    # /government/publications/dry-weather-and-drought-in-england...-summary-reports/dry-weather-and-drought-in-england-...
    pattern = re.compile(
        r'href="(/government/publications/dry-weather-and-drought-in-england[^"]*-summary-reports/dry-weather-and-drought-in-england-[^"]+)"'
    )
    links = sorted(set(pattern.findall(html)))
    return links


def main():
    all_sources = []

    # --- 2022: individual news-story announcements, tightened to actual
    # news/press-release formats only (cuts out speeches, corporate
    # reports, consultations, guidance -- all noise for this purpose) ---
    print("Querying GOV.UK Search API: Environment Agency drought news, 2022 (format-filtered)...", flush=True)
    results_2022 = search_gov_uk(
        query="drought",
        organisation="environment-agency",
        date_from="2022-01-01",
        date_to="2023-03-31",
        formats=["news_story", "press_release"],
    )
    for r in results_2022:
        all_sources.append({
            "year": 2022,
            "source_type": "news_story",
            "title": r.get("title"),
            "url": GOVUK_BASE + r.get("link", ""),
            "public_timestamp": r.get("public_timestamp"),
            "format": r.get("format"),
        })
    print(
        f"  Found {len(results_2022)} result(s) after format filtering.", flush=True)
    time.sleep(0.5)

    # --- 2018: readings data already covers this (MIN_DATE=2015-01-01 on
    # every pull so far) -- corroborated by multiple independent sources
    # as a real, EA-recognised drought ("the last drought in England was
    # back in 2018"), and a genuinely different event profile (shorter,
    # more sudden) than 2022/2025, adding real diversity rather than a
    # third similar data point. Same discovery method, no new data pull
    # needed -- purely a ground-truth question. ---
    print("Querying GOV.UK Search API: Environment Agency drought news, 2018...", flush=True)
    results_2018 = search_gov_uk(
        query="drought",
        organisation="environment-agency",
        date_from="2018-01-01",
        date_to="2019-03-31",
        formats=["news_story", "press_release"],
    )
    for r in results_2018:
        all_sources.append({
            "year": 2018,
            "source_type": "news_story",
            "title": r.get("title"),
            "url": GOVUK_BASE + r.get("link", ""),
            "public_timestamp": r.get("public_timestamp"),
            "format": r.get("format"),
        })
    print(f"  Found {len(results_2018)} result(s).", flush=True)
    time.sleep(0.5)

    # --- 2025/2026 (and beyond): dynamically discover whichever collection
    # page(s) currently exist, rather than relying on hardcoded URLs ---
    collection_pages = find_current_collection_pages()
    if not collection_pages:
        print("WARNING: dynamic discovery found no collection pages — "
              "falling back to known seed URLs (may be stale).", flush=True)
        collection_pages = FALLBACK_COLLECTION_PAGES
    else:
        print(
            f"Dynamically discovered {len(collection_pages)} collection page(s):", flush=True)
        for c in collection_pages:
            print(f"  {c}", flush=True)

    for collection_url in collection_pages:
        print(f"Fetching collection page: {collection_url}", flush=True)
        links = fetch_weekly_report_list(collection_url)
        print(f"  Found {len(links)} report link(s).", flush=True)
        for link in links:
            # Extract the human-readable date range from the URL's tail
            title_guess = link.rsplit("/", 1)[-1].replace("-", " ")
            all_sources.append({
                "year": "2025/2026",
                "source_type": "weekly_drought_summary",
                "title": title_guess,
                "url": GOVUK_BASE + link,
                "public_timestamp": "",  # not available from the link list alone
                "format": "html_publication",
            })
        time.sleep(0.5)

    out_path = "ground_truth_sources_discovered.csv"
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
                                "year", "source_type", "title", "url", "public_timestamp", "format"])
        writer.writeheader()
        for s in all_sources:
            writer.writerow(s)

    print(f"\nWritten {len(all_sources)} discovered source(s) to {out_path}.")
    print("Review this file before building the extraction step — check:")
    print("  1. Do the 2022 results form a coherent timeline (PDW -> drought -> recovery), or are there gaps?")
    print("  2. Are any 2022 results still false positives (mention 'drought' but aren't status announcements)?")
    print("  3. Does the weekly report list run continuously with no missing weeks?")


if __name__ == "__main__":
    main()
