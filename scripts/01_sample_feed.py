"""
STEP 1 - Walk the feed and build a lightweight sample.

This costs almost nothing (one request per 100 tenders) and already answers
real data-understanding questions:
  - what procedure types actually exist, and in what proportion
  - what statuses exist
  - how fast the feed moves (tenders per day)

Run:
    python 01_sample_feed.py --pages 50
    python 01_sample_feed.py --pages 50 --start-date 2026-01-15T00:00:00
    python 01_sample_feed.py --pages 200 --out data/feed_2026.csv

Output: a CSV of tender ids plus a few cheap fields. No full objects yet.
"""

import argparse
import collections
import csv
import os

from prozorro import iter_feed

# Fields confirmed to work with opt_fields on the feed endpoint.
# Anything the API does not recognise is silently dropped, so add new ones
# one at a time and check the output.
OPT_FIELDS = ["dateCreated", "dateModified", "procurementMethodType", "status"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=50,
                    help="pages of 100 tenders to pull (default 50 = 5000)")
    ap.add_argument("--start-date", default=None,
                    help='ISO date to start from, e.g. "2026-01-15T00:00:00". '
                         "Omit to start from the newest records.")
    ap.add_argument("--ascending", action="store_true",
                    help="walk forward in time instead of backward")
    ap.add_argument("--out", default="data/feed_sample.csv")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    rows = []
    method_counts = collections.Counter()
    status_counts = collections.Counter()

    print(f"Walking the feed: {args.pages} pages x 100 records...")

    for i, rec in enumerate(iter_feed(limit=100,
                                      descending=not args.ascending,
                                      offset=args.start_date,
                                      opt_fields=OPT_FIELDS,
                                      max_pages=args.pages), start=1):
        rows.append({
            "id": rec.get("id"),
            "dateCreated": rec.get("dateCreated"),
            "dateModified": rec.get("dateModified"),
            "procurementMethodType": rec.get("procurementMethodType"),
            "status": rec.get("status"),
        })
        method_counts[rec.get("procurementMethodType")] += 1
        status_counts[rec.get("status")] += 1

        if i % 1000 == 0:
            print(f"  {i} records...")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "dateCreated", "dateModified",
                                          "procurementMethodType", "status"])
        w.writeheader()
        w.writerows(rows)

    print(f"\nSaved {len(rows)} records to {args.out}\n")

    # The feed is a change log: the same tender can appear more than once if it
    # was modified twice in the window. Worth knowing before you count anything.
    unique_ids = len({r["id"] for r in rows})
    print(f"Unique tender ids: {unique_ids}  "
          f"(duplicates in feed: {len(rows) - unique_ids})\n")

    print("procurementMethodType")
    for k, v in method_counts.most_common():
        print(f"  {str(k):30s} {v:6d}  {100*v/len(rows):5.1f}%")

    print("\nstatus")
    for k, v in status_counts.most_common():
        print(f"  {str(k):30s} {v:6d}  {100*v/len(rows):5.1f}%")

    dates = sorted(r["dateModified"] for r in rows if r["dateModified"])
    if dates:
        print(f"\ndateModified range: {dates[0]}  ->  {dates[-1]}")
        print("(use this to judge how many days of activity your sample covers)")


if __name__ == "__main__":
    main()
