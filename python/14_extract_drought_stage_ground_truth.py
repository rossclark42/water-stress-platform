"""
14_extract_drought_stage_ground_truth.py

Phase 2, final step: reads the 70 sources found by
13_discover_ground_truth_sources.py, fetches each one's real HTML content
(not just the title), and uses a single-call LLM prompt per source to pull
out structured per-area drought-stage facts, writing them into
drought_stage_extracts.

Run sql/schema.sql (04_create_drought_stage_extracts_table.sql section)
against RDS first -- this script assumes the table already exists.

DESIGN DECISIONS (see DECISIONS.md for full write-up):

1. TWO PROMPT TEMPLATES, ONE OUTPUT SCHEMA. news_story sources (2018/2022)
   are free-form articles describing a single status-change event;
   weekly_drought_summary sources (2025/2026) are structured reports with
   an explicit area-by-area breakdown. Different enough shapes that one
   generic prompt would underperform on one or the other -- but both are
   still a single LLM call per source, not a multi-step agent, consistent
   with the project's existing AI-scope decision.

2. CLOSED-VOCABULARY AREA MATCHING. The model is given the real list of
   14 area_name values (fetched live from the `areas` table, not
   hardcoded here) and told to pick from that list only, or return
   area_name: null if a source's mention is ambiguous. This pushes
   disambiguation into the prompt itself rather than fuzzy-matching
   afterward, and means an unmatched mention shows up as a fact with no
   area_code (visible in raw_extracted_text for manual review) instead of
   a silently wrong guess.

3. "NO EXTRACTABLE FACT" IS A NORMAL OUTCOME, NOT AN ERROR. Many of the
   70 sources -- especially process/meeting announcements among the
   2018/2022 news_story set -- likely don't contain a clean per-area
   status statement. The prompt explicitly allows an empty array. Sources
   that yield zero facts write zero rows and are logged as "no_facts" in
   drought_extraction_log.csv, not treated as failures.

4. RESUME-SAFE, LOGGED IN RDS NOT A LOCAL CSV. Every source's outcome
   (ok / no_facts / fetch_error / llm_error) is written to
   ground_truth_sources_checked as it's processed. This is deliberately
   NOT a local cache file like station_history_cache.csv -- it needs to
   survive as a durable, queryable record so Phase 3/7's backtest can
   later distinguish "no ground truth exists for this area/date" from "a
   source covering this window was checked and genuinely found nothing"
   -- drought_stage_extracts alone (positive facts only) can't make that
   distinction, and treating a missing row as an implicit "Normal" would
   be a silent wrong inference, the same class of bug this project has
   already caught twice (ONSPD sentinel coordinates, the wide-date-range
   misdiagnosis). Re-running this script re-queries the table to skip
   already-processed sources; set FORCE_REPROCESS = True to redo
   everything (fetch_error/llm_error sources are always retried
   regardless, since those weren't a genuine "checked" outcome).

Run from inside python/, per project convention.
Requires: requests, psycopg2, beautifulsoup4
Requires env vars: PGPASSWORD, ANTHROPIC_API_KEY
"""

import csv
import json
import os
import re
import time
from datetime import datetime

import psycopg2
import requests
from bs4 import BeautifulSoup

# --- config ---

SOURCES_CSV = "ground_truth_sources_discovered.csv"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not ANTHROPIC_API_KEY:
    raise SystemExit("Set ANTHROPIC_API_KEY environment variable first.")

# claude-sonnet-4-6 chosen for extraction accuracy on messy real-world
# text (news articles, bulleted reports of varying structure) over the
# cheaper claude-haiku-4-5-20251001 -- swap if cost becomes a concern at
# this small a volume (70 calls), it isn't expected to.
ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

PGPASSWORD = os.environ.get("PGPASSWORD")
if not PGPASSWORD:
    raise SystemExit(
        "Set PGPASSWORD environment variable first (do not hardcode it here).")

DB_CONFIG = {
    "host": "c24-ross-clark-water-stress-platform.c57vkec7dkkx.eu-west-2.rds.amazonaws.com",
    "port": 5432,
    "dbname": "waterstress",
    "user": "postgres",
    "password": PGPASSWORD,
    "connect_timeout": 10,
}

VALID_STAGES = ["Normal", "Prolonged Dry Weather",
                "Drought", "Severe Drought", "Recovery"]

MAX_PAGE_CHARS = 18000     # budget the page text sent to the LLM
REQUEST_SLEEP = 1.0        # between gov.uk fetches
LLM_SLEEP = 1.0            # between Anthropic calls

# True: only process a small stratified sample end-to-end (some of each
# source_type present), print full LLM output, before running all 70.
# Recommended for the first run. Deliberately stratified rather than the
# first N rows, since ground_truth_sources_discovered.csv is written
# news_story-then-weekly_drought_summary (see 13_discover_...py) — the
# first N rows alone would never exercise the weekly-report prompt path.
TEST_MODE = True
TEST_MODE_PER_TYPE = 2  # sources per distinct source_type in TEST_MODE

# True: ignore ground_truth_sources_checked and reprocess every source,
# including ones already logged as ok/no_facts. Normally leave False --
# that table is what makes re-runs cheap and safe. NOTE: forcing a
# reprocess of an already-"ok" source can create duplicate rows for any
# fact where area_code or report_date is NULL (Postgres doesn't treat
# NULL = NULL for the uq_drought_stage_extract dedup) — fine for
# unmatched-area edge cases meant for manual review anyway, but don't
# force-reprocess casually.
FORCE_REPROCESS = False


# --- HTTP retry, same pattern as 09_pull_historical_readings.py ---

def request_with_retry(method, url, headers=None, params=None, json_body=None,
                       timeout=30, max_retries=6):
    delay = 5
    for attempt in range(max_retries):
        resp = requests.request(method, url, headers=headers, params=params,
                                json=json_body, timeout=timeout)
        if resp.status_code in (429, 529):  # 529 = Anthropic "overloaded"
            retry_after = resp.headers.get("Retry-After")
            wait = int(
                retry_after) if retry_after and retry_after.isdigit() else delay
            print(f"    Rate limited/overloaded ({resp.status_code}) — waiting {wait}s "
                  f"(retry {attempt + 1}/{max_retries})...", flush=True)
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        if resp.status_code >= 400:
            # Print the response body before raising — a bare HTTPError
            # loses the actual error detail (e.g. Anthropic's "model not
            # found" or a gov.uk 404 body), which is exactly what's
            # needed to diagnose a fetch_error/llm_error entry.
            print(
                f"    HTTP {resp.status_code} from {url}: {resp.text[:500]}", flush=True)
        resp.raise_for_status()
        return resp
    raise RuntimeError(f"Gave up after {max_retries} retries: {url}")


# --- page fetch + text extraction ---

def fetch_page_text(url):
    """
    Fetch a gov.uk HTML page and return cleaned, boilerplate-stripped
    text, truncated to MAX_PAGE_CHARS. Raises on fetch failure -- caller
    is responsible for logging that as fetch_error.
    """
    resp = request_with_retry("GET", url, timeout=20)
    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()

    # gov.uk publication pages usually put the actual content inside
    # <main>, but the 2018/2022 news_story pages may use an older
    # template than the 2025/2026 weekly reports -- try a couple of
    # known alternatives before falling back to the whole document.
    # TEST_MODE_PER_TYPE ensures both source types get spot-checked so
    # this fallback chain gets genuinely exercised, not just assumed.
    main = (soup.find("main")
            or soup.find(attrs={"role": "main"})
            or soup.find(id="content")
            or soup)

    text = main.get_text(separator="\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()

    return text[:MAX_PAGE_CHARS]


# --- prompt construction ---

def build_prompt(source_row, area_names):
    area_list_str = "\n".join(f"- {name}" for name in area_names)
    stage_list_str = ", ".join(f'"{s}"' for s in VALID_STAGES)
    fallback_date = source_row.get("public_timestamp") or ""

    shared_output_instructions = f"""
Respond with ONLY a JSON array (no markdown fences, no preamble, no
commentary before or after). Each element is an object:

{{
  "area_name": <one exact string from the list below, or null if the
                source's area mention is ambiguous or doesn't clearly
                match one of these 14>,
  "drought_stage": <one of {stage_list_str}, or null if the text
                     mentions an area but doesn't state a clear current
                     stage for it>,
  "report_date": <"YYYY-MM-DD" the date this status is TRUE AS OF, if the
                   text states or implies one more specific than the
                   fallback below; otherwise use "{fallback_date[:10] if fallback_date else ''}">,
  "quote": <a verbatim snippet from the text, 20 words or fewer, that
             directly supports this specific area+stage fact>
}}

If the source contains NO clear per-area drought-stage status statement
at all (e.g. it's a meeting announcement, a general policy statement, or
only discusses the topic without naming a specific area's current
stage), return an empty array: []

Do not guess or infer a stage from indirect language (e.g. "farmers are
struggling" is not itself a stage statement). Only extract facts the
text actually asserts.

Valid area names (choose from this list only, or use null):
{area_list_str}
""".strip()

    if source_row["source_type"] == "news_story":
        system = (
            "You are extracting structured ground-truth drought-status facts "
            "from a UK Environment Agency news article, for use validating a "
            "drought early-warning model. Precision matters more than "
            "coverage — a wrong or fabricated fact is worse than a missed one."
        )
        user = f"""
This is a GOV.UK news article about drought conditions in England, published
around {fallback_date[:10] if fallback_date else 'an unknown date'}. It
typically documents ONE status-change event (e.g. one or more areas being
newly declared into a drought stage), not a full national roundup — extract
only the area(s) whose CURRENT stage is explicitly and clearly stated in
this specific article, not areas mentioned only in historical comparison or
background context.

Article title: {source_row.get('title', '')}
Article URL: {source_row.get('url', '')}

Article text:
---
{{page_text}}
---

{shared_output_instructions}
""".strip()
    else:  # weekly_drought_summary
        system = (
            "You are extracting structured ground-truth drought-status facts "
            "from a UK Environment Agency weekly drought summary report, for "
            "use validating a drought early-warning model. These reports "
            "typically list every EA operational area's current stage "
            "explicitly, often in a bulleted or tabular breakdown — extract "
            "every area+stage pair stated, not just ones that changed this "
            "week."
        )
        user = f"""
This is a GOV.UK weekly "Dry weather and drought in England" summary report.
It should contain a breakdown of each EA operational area's current drought
stage as of this report's date. Extract every area+stage fact explicitly
stated, including areas explicitly stated as "Normal" — a Normal
classification is just as much a ground-truth fact as any other stage for
this project's backtest.

Report title/URL fragment: {source_row.get('title', '')}
Report URL: {source_row.get('url', '')}

NOTE ON FORMAT: some reports may name only the areas that are NOT Normal,
with a single blanket line covering the rest (e.g. "all other areas
remain in normal conditions"). If you see that pattern, still emit one
Normal fact for each of the 14 valid areas not otherwise named, quoting
the blanket statement as the supporting quote for each. If instead every
area is individually named, just extract each one as stated — don't
guess which format applies before reading the actual text.

Report text:
---
{{page_text}}
---

{shared_output_instructions}
""".strip()

    return system, user


def call_claude(system, user_with_placeholder, page_text):
    user = user_with_placeholder.replace("{page_text}", page_text)
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 3000,  # up to 14 area facts w/ quotes for a weekly report -- 2000 was tight
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    resp = request_with_retry(
        "POST", ANTHROPIC_API_URL, headers=headers, json_body=body, timeout=60)
    data = resp.json()
    text_blocks = [b["text"]
                   for b in data.get("content", []) if b.get("type") == "text"]
    raw = "".join(text_blocks).strip()

    # Defensive: strip markdown fences if the model adds them despite instructions
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    # let a malformed response raise -- caller logs as llm_error
    return json.loads(raw)


# --- DB helpers ---

def get_area_names(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT area_code, area_name FROM areas ORDER BY area_name")
        rows = cur.fetchall()
    return {name: code for code, name in rows}


def _valid_iso_date(s):
    if not s:
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def insert_facts(conn, facts, source_row, area_name_to_code):
    """
    Writes each extracted fact as a row. Deliberately per-fact fault
    tolerant: one malformed fact (e.g. a hallucinated invalid date)
    shouldn't lose the rest of a source's facts, and shouldn't crash the
    whole run and poison the DB connection for every source after it —
    each fact gets its own try/except + rollback rather than one
    transaction for the whole batch.
    """
    inserted = 0
    for fact in facts:
        area_name = fact.get("area_name")
        area_code = area_name_to_code.get(area_name) if area_name else None
        if area_name and area_code is None:
            print(f"    WARNING: area_name '{area_name}' from the model didn't match "
                  f"the closed list exactly — treated as unmatched (area_code=NULL). "
                  f"Check for a near-miss (punctuation/wording) vs a genuine ambiguity.", flush=True)

        drought_stage = fact.get("drought_stage")
        if drought_stage is not None and drought_stage not in VALID_STAGES:
            print(f"    WARNING: drought_stage '{drought_stage}' isn't one of the 5 valid "
                  f"stages — dropped to NULL.", flush=True)
            drought_stage = None

        report_date = _valid_iso_date(fact.get("report_date"))
        if fact.get("report_date") and report_date is None:
            print(f"    WARNING: report_date '{fact.get('report_date')}' isn't a valid "
                  f"YYYY-MM-DD date — dropped to NULL.", flush=True)

        quote = (fact.get("quote") or "")[:500]

        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO drought_stage_extracts
                        (area_code, report_date, source, drought_stage,
                         raw_extracted_text, source_pdf_url, reviewed_flag)
                    VALUES (%s, %s, %s, %s, %s, %s, FALSE)
                    ON CONFLICT (source_pdf_url, area_code, report_date) DO NOTHING
                    RETURNING extract_id
                    """,
                    (area_code, report_date, source_row["source_type"], drought_stage,
                     quote, source_row["url"]),
                )
                row = cur.fetchone()
            conn.commit()
            if row is not None:
                inserted += 1
        except Exception as e:
            conn.rollback()  # required before the connection can be used again
            print(f"    DB ERROR writing fact {fact}: {e}", flush=True)

    return inserted


# --- log (resume-safety + downstream coverage visibility — lives in RDS,
# see ground_truth_sources_checked in schema.sql for why this isn't a
# local CSV like station_history_cache.csv) ---

def load_checked(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_url, status FROM ground_truth_sources_checked")
        return dict(cur.fetchall())


def upsert_checked(conn, url, source_type, status, num_facts_written):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ground_truth_sources_checked
                (source_url, source_type, status, num_facts_written, checked_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (source_url) DO UPDATE SET
                status = EXCLUDED.status,
                num_facts_written = EXCLUDED.num_facts_written,
                checked_at = EXCLUDED.checked_at
            """,
            (url, source_type, status, num_facts_written),
        )
    conn.commit()


def load_sources():
    with open(SOURCES_CSV, newline="") as f:
        return list(csv.DictReader(f))


def main():
    print("Connecting to RDS...", flush=True)
    conn = psycopg2.connect(**DB_CONFIG)
    print("Connected.", flush=True)

    area_name_to_code = get_area_names(conn)
    print(
        f"Loaded {len(area_name_to_code)} area names from RDS for closed-list matching.", flush=True)
    if len(area_name_to_code) != 14:
        print("WARNING: expected 14 areas — check the areas table before trusting output.", flush=True)

    sources = load_sources()
    checked = load_checked(conn) if not FORCE_REPROCESS else {}
    print(f"Loaded {len(sources)} source(s) from {SOURCES_CSV}.", flush=True)
    print(f"{len(checked)} already checked (in ground_truth_sources_checked) — ok/no_facts will be skipped.", flush=True)

    if TEST_MODE:
        by_type = {}
        for s in sources:
            by_type.setdefault(s["source_type"], []).append(s)
        sample = []
        for source_type, rows in by_type.items():
            sample.extend(rows[:TEST_MODE_PER_TYPE])
        sources = sample
        print(f"TEST_MODE: processing {len(sources)} source(s) stratified across "
              f"{list(by_type.keys())}, with full output printed.", flush=True)

    total_facts = 0
    n_ok, n_no_facts, n_fetch_error, n_llm_error, n_skipped = 0, 0, 0, 0, 0

    for i, source_row in enumerate(sources, 1):
        url = source_row["url"]
        if checked.get(url) in ("ok", "no_facts"):
            n_skipped += 1
            continue

        print(f"[{i}/{len(sources)}] {source_row['source_type']} — {source_row.get('title', '')[:70]}", flush=True)

        try:
            page_text = fetch_page_text(url)
        except Exception as e:
            print(f"    FETCH ERROR: {e}", flush=True)
            upsert_checked(
                conn, url, source_row["source_type"], "fetch_error", 0)
            n_fetch_error += 1
            time.sleep(REQUEST_SLEEP)
            continue

        system, user_template = build_prompt(
            source_row, sorted(area_name_to_code.keys()))

        try:
            facts = call_claude(system, user_template, page_text)
            if not isinstance(facts, list):
                raise ValueError(f"expected a JSON array, got {type(facts)}")
        except Exception as e:
            print(f"    LLM/PARSE ERROR: {e}", flush=True)
            upsert_checked(
                conn, url, source_row["source_type"], "llm_error", 0)
            n_llm_error += 1
            time.sleep(LLM_SLEEP)
            continue

        if TEST_MODE:
            print(
                f"    Raw extracted facts: {json.dumps(facts, indent=2)}", flush=True)

        if not facts:
            print("    No extractable fact — logged as no_facts.", flush=True)
            upsert_checked(conn, url, source_row["source_type"], "no_facts", 0)
            n_no_facts += 1
        else:
            written = insert_facts(conn, facts, source_row, area_name_to_code)
            print(
                f"    {len(facts)} fact(s) extracted, {written} new row(s) written.", flush=True)
            upsert_checked(conn, url, source_row["source_type"], "ok", written)
            total_facts += written
            n_ok += 1

        time.sleep(REQUEST_SLEEP)
        time.sleep(LLM_SLEEP)

    print("\n--- Summary ---")
    print(f"Sources skipped (already checked in RDS): {n_skipped}")
    print(f"Sources with facts written (ok): {n_ok}")
    print(f"Sources with no extractable fact: {n_no_facts}")
    print(f"Sources that failed to fetch: {n_fetch_error}")
    print(f"Sources that failed LLM extraction/parsing: {n_llm_error}")
    print(f"Total new rows written to drought_stage_extracts: {total_facts}")

    if TEST_MODE:
        print("\nTEST_MODE was on — only 3 sources processed. Review the raw "
              "extracted facts above, spot-check them against the article/report "
              "yourself, then set TEST_MODE = False and re-run for the full 70. "
              "Progress is tracked in ground_truth_sources_checked (RDS), not a "
              "local file — a re-run will pick up where this one left off.")

    conn.close()


if __name__ == "__main__":
    main()
