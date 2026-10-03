"""
STEP 2 - Fetch full tender objects for a sample, cache them to disk.

One request per tender, so this is the slow part. It is resumable: run it,
stop it with Ctrl-C, run it again and it picks up where it left off.

Run:
    python 02_fetch_tenders.py --max 500
    python 02_fetch_tenders.py --max 2000 --methods aboveThreshold
    python 02_fetch_tenders.py --max 500 --statuses complete

Output: data/tenders.jsonl - one full tender object per line.
Never delete this file. It is your dataset.
"""

import argparse
import csv
import json
import os
import time

from prozorro import get_tender, make_session, SLEEP

# Procedures where companies actually submit competing bids.
# These are the only ones where "was the competition real?" is a question.
COMPETITIVE = {
    "aboveThreshold", "aboveThresholdUA", "aboveThresholdEU",
    "aboveThresholdUA.defense", "belowThreshold",
    "open", "openua", "openeu",
    "competitiveDialogueUA", "competitiveDialogueEU",
    "competitiveOrdering", "priceQuotation", "esco",
}

# Direct awards with no competition at all. Useful as a contrast group,
# but they cannot be scored for rigged competition - there was none.
NON_COMPETITIVE = {"reporting", "negotiation", "negotiation.quick"}


def load_done(path):
    """Ids already in the output file, so we never fetch the same tender twice."""
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:
                    continue
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="data/feed_sample.csv")
    ap.add_argument("--out", default="data/tenders.jsonl")
    ap.add_argument("--max", type=int, default=500,
                    help="how many NEW tenders to fetch this run")
    ap.add_argument("--methods", default="competitive",
                    help='"competitive" (default), "all", or a comma-separated '
                         'list such as "aboveThreshold,belowThreshold"')
    ap.add_argument("--statuses", default=None,
                    help='optional comma-separated filter, e.g. "complete"')
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    if args.methods == "competitive":
        wanted_methods = COMPETITIVE
    elif args.methods == "all":
        wanted_methods = None
    else:
        wanted_methods = {m.strip() for m in args.methods.split(",")}

    wanted_statuses = ({s.strip() for s in args.statuses.split(",")}
                       if args.statuses else None)

    with open(args.sample, encoding="utf-8") as f:
        candidates = list(csv.DictReader(f))

    done = load_done(args.out)
    print(f"Already cached: {len(done)} tenders")

    queue, seen = [], set(done)
    for row in candidates:
        tid = row["id"]
        if tid in seen:
            continue
        if wanted_methods and row["procurementMethodType"] not in wanted_methods:
            continue
        if wanted_statuses and row["status"] not in wanted_statuses:
            continue
        seen.add(tid)
        queue.append(tid)
        if len(queue) >= args.max:
            break

    print(f"To fetch this run: {len(queue)}\n")
    if not queue:
        print("Nothing to do. Widen --methods/--statuses, or run step 1 for more pages.")
        return

    session = make_session()
    t0 = time.time()
    ok = fail = 0

    # Append mode: safe to interrupt at any point.
    with open(args.out, "a", encoding="utf-8") as out:
        for i, tid in enumerate(queue, start=1):
            try:
                tender = get_tender(tid, session=session)
                out.write(json.dumps(tender, ensure_ascii=False) + "\n")
                out.flush()
                ok += 1
            except Exception as e:
                print(f"  FAILED {tid}: {e}")
                fail += 1

            if i % 25 == 0:
                rate = i / (time.time() - t0)
                left = (len(queue) - i) / rate if rate else 0
                print(f"  {i}/{len(queue)}  ({rate:.1f}/s, ~{left/60:.1f} min left)")

            time.sleep(SLEEP)

    size_mb = os.path.getsize(args.out) / 1e6
    print(f"\nDone. {ok} fetched, {fail} failed.")
    print(f"{args.out} is now {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
