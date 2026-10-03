"""
Minimal Prozorro API client.

Public read API, no authentication, no API key.
Docs: https://prozorro-api-docs.readthedocs.io/en/master/

Two things matter about this API:

1. The feed (/tenders) is a CHANGE LOG ordered by dateModified, not a database
   query. You walk it with a cursor (next_page.offset). There is no "give me all
   tenders with CPV 33100000 in 2025" query - you walk and filter client-side.

2. A single tender object (/tenders/{id}) can be large (hundreds of KB) because
   of the `criteria` block. Fetch sparingly, cache everything to disk, never
   re-download.
"""

import time
import requests

BASE = "https://public-api.prozorro.gov.ua/api/2.5"
HEADERS = {"User-Agent": "TenderLens-academic-project/0.1"}

# Seconds to wait between requests. Be polite - this is a free public service.
SLEEP = 0.25


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def _get(session, url, params=None, max_retries=5):
    """GET with exponential backoff on throttling and server errors."""
    delay = 1.0
    for _ in range(max_retries):
        try:
            r = session.get(url, params=params, timeout=60)
        except requests.RequestException as e:
            print(f"    network error: {e} - retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
            continue

        if r.status_code == 200:
            return r.json()

        if r.status_code in (429, 500, 502, 503, 504):
            print(f"    HTTP {r.status_code} - retrying in {delay:.0f}s")
            time.sleep(delay)
            delay *= 2
            continue

        r.raise_for_status()

    raise RuntimeError(f"gave up after {max_retries} attempts: {url}")


def iter_feed(limit=100, descending=True, offset=None, opt_fields=None,
              max_pages=None, session=None):
    """
    Walk the tender feed, yielding one record per tender.

    limit       records per page (100 is the usual maximum)
    descending  True = newest first. False = oldest first (starts in Feb 2015)
    offset      where to start. Accepts either:
                  - an ISO date string, e.g. "2026-01-15T00:00:00"
                  - a cursor string from a previous next_page.offset
    opt_fields  list of extra fields to include on each record.
                Without it you only get id + dateModified.
    max_pages   stop after this many pages (None = until the feed ends)

    Yields dicts. `id` is always present.
    """
    session = session or make_session()

    params = {"limit": limit}
    if descending:
        params["descending"] = 1
    if offset:
        params["offset"] = offset
    if opt_fields:
        params["opt_fields"] = ",".join(opt_fields)

    page = 0
    while True:
        payload = _get(session, f"{BASE}/tenders", params=params)
        rows = payload.get("data", [])
        if not rows:
            return

        for row in rows:
            yield row

        page += 1
        if max_pages is not None and page >= max_pages:
            return

        next_offset = (payload.get("next_page") or {}).get("offset")
        if not next_offset:
            return

        params["offset"] = next_offset
        time.sleep(SLEEP)


def get_tender(tender_id, session=None):
    """Fetch one full tender object. Returns the contents of `data`."""
    session = session or make_session()
    return _get(session, f"{BASE}/tenders/{tender_id}")["data"]
