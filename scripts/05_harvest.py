"""
STEP 4 - Harvest at scale.

Why this script exists: opt_fields on the feed only serves top-level scalar
fields. items / classification / value are NOT available there, so there is no
way to filter by CPV before downloading. The only strategy is:

    walk the feed (cheap)  ->  fetch everything (expensive)  ->  filter after.

Two things make that affordable:

  1. We drop the `criteria` block on write. It is the legal exclusion-criteria
     boilerplate, near-identical across tenders, and it is most of the payload.
     Pass --keep-criteria if you ever need it.

  2. A cursor file. The feed walk resumes exactly where the last run stopped,
     so you can harvest over several sessions without re-reading or re-fetching.

Run (first time):
    python 05_harvest.py --target 3000

Run again later - it continues from the cursor, nothing is repeated:
    python 05_harvest.py --target 5000

Watch the live CPV depth report it prints every 500 tenders. Stop when enough
CPV-4 groups have crossed 30. Then re-run 04_diagnose.py on the bigger file.
"""

import argparse
import json
import os
import time
from collections import Counter

from prozorro import iter_feed, get_tender, make_session, SLEEP

CURSOR_FILE = "data/.feed_cursor"

COMPETITIVE = {
    "aboveThreshold", "aboveThresholdUA", "aboveThresholdEU",
    "aboveThresholdUA.defense", "belowThreshold",
    "open", "openua", "openeu",
    "competitiveDialogueUA", "competitiveDialogueEU",
    "competitiveOrdering", "priceQuotation", "esco",
}

# Equipment categories: specifications full of numeric parameters
# (capacity, voltage, dimensions, speed) - exactly what the over-precision
# model needs. Services and raw commodities have almost no parameters,
# so they are poor material for that objective.
#   30 computing & office equipment   31 electrical equipment
#   32 radio/telecom equipment        33 medical equipment
#   42 industrial machinery           34 transport equipment
SUGGESTED_DIVISIONS = {"30", "31", "32", "33", "42", "34"}


def load_cursor():
    if os.path.exists(CURSOR_FILE):
        with open(CURSOR_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    return None


def save_cursor(offset):
    os.makedirs(os.path.dirname(CURSOR_FILE), exist_ok=True)
    with open(CURSOR_FILE, "w", encoding="utf-8") as f:
        f.write(offset or "")


def load_done(path):
    done = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    done.add(json.loads(line)["id"])
                except Exception:
                    continue
    return done


def cpv_of(tender):
    items = tender.get("items") or []
    if not items:
        return None
    return ((items[0].get("classification") or {}).get("id"))


def depth_report(path, limit=12):
    """How many CPV-4 groups have crossed the usable threshold?"""
    c4 = Counter()
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                cpv = json.loads(line).get("_cpv")
            except Exception:
                continue
            if cpv:
                c4[cpv[:4]] += 1
    if not c4:
        return
    ge10 = sum(1 for v in c4.values() if v >= 10)
    ge30 = sum(1 for v in c4.values() if v >= 30)
    ge60 = sum(1 for v in c4.values() if v >= 60)
    print(f"    CPV-4 depth: {len(c4)} groups | >=10: {ge10} | >=30: {ge30} | >=60: {ge60}")
    top = ", ".join(f"{k}:{v}" for k, v in c4.most_common(6))
    print(f"    deepest: {top}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=3000,
                    help="total tenders you want in the file when this run ends")
    ap.add_argument("--out", default="data/tenders.jsonl")
    ap.add_argument("--methods", default="competitive",
                    help='"competitive" (default), "all", or a comma-separated list')
    ap.add_argument("--divisions", default=None,
                    help='keep only these CPV divisions, e.g. "30,31,32". '
                         "Default: keep everything (you filter later). "
                         "Note: filtering here still costs one fetch per tender, "
                         "it only saves disk space.")
    ap.add_argument("--keep-criteria", action="store_true",
                    help="keep the bulky legal criteria block (default: drop it)")
    ap.add_argument("--restart", action="store_true",
                    help="ignore the saved cursor and start again from the newest")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)

    if args.methods == "competitive":
        wanted = COMPETITIVE
    elif args.methods == "all":
        wanted = None
    else:
        wanted = {m.strip() for m in args.methods.split(",")}

    divisions = ({d.strip() for d in args.divisions.split(",")}
                 if args.divisions else None)
    if divisions:
        print(f"CPV divisions kept: {sorted(divisions)}")

    done = load_done(args.out)
    print(f"Already in file: {len(done)} tenders")
    need = args.target - len(done)
    if need <= 0:
        print("Target already reached. Raise --target to go further.")
        depth_report(args.out)
        return
    print(f"Fetching {need} more this run.\n")

    cursor = None if args.restart else load_cursor()
    print("Starting from the newest records." if not cursor
          else f"Resuming feed from saved cursor.\n")

    session = make_session()
    feed = iter_feed(limit=100, descending=True, offset=cursor,
                     opt_fields=["procurementMethodType", "status", "dateModified"],
                     session=session)

    added = skipped = failed = seen = 0
    t0 = time.time()
    # We resume by dateModified, not by the opaque next_page cursor: the API
    # accepts an ISO date as `offset`, and with descending=1 that means
    # "continue backwards in time from here".
    last_date = None

    out = open(args.out, "a", encoding="utf-8")
    try:
        for rec in feed:
            seen += 1
            if rec.get("dateModified"):
                last_date = rec["dateModified"]

            if rec["id"] in done:
                continue
            if wanted and rec.get("procurementMethodType") not in wanted:
                skipped += 1
                continue

            try:
                t = get_tender(rec["id"], session=session)
            except Exception as e:
                print(f"  FAILED {rec['id']}: {e}")
                failed += 1
                time.sleep(SLEEP)
                continue

            cpv = cpv_of(t)
            if divisions and (not cpv or cpv[:2] not in divisions):
                skipped += 1
                done.add(rec["id"])
                time.sleep(SLEEP)
                continue

            if not args.keep_criteria:
                t.pop("criteria", None)
            # Denormalised so later scripts never have to dig into items[].
            t["_cpv"] = cpv

            out.write(json.dumps(t, ensure_ascii=False) + "\n")
            out.flush()
            done.add(rec["id"])
            added += 1

            if added % 100 == 0:
                rate = added / (time.time() - t0)
                mb = os.path.getsize(args.out) / 1e6
                eta = (need - added) / rate / 60 if rate else 0
                print(f"  {added}/{need} fetched | {rate:.1f}/s | "
                      f"{mb:.0f} MB | ~{eta:.0f} min left | feed read: {seen}")

            if added % 500 == 0:
                depth_report(args.out)

            time.sleep(SLEEP)

            if added >= need:
                break
    except KeyboardInterrupt:
        print("\nInterrupted - progress is saved, just run the script again.")
    finally:
        out.close()
        # Save where the feed walk got to, so the next run continues backwards
        # from here instead of re-reading everything.
        if last_date:
            save_cursor(last_date)
            print(f"Cursor saved at {last_date}")

    mb = os.path.getsize(args.out) / 1e6
    print(f"\nAdded {added} | skipped {skipped} | failed {failed}")
    print(f"File: {args.out}  ({mb:.0f} MB, {len(done)} tenders)")
    print(f"Feed records read this run: {seen}")
    depth_report(args.out)
    print("\nNext: python 04_diagnose.py   (section E tells you if you have enough depth)")


if __name__ == "__main__":
    main()
