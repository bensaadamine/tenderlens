"""
STEP 3 - Profile the cached tenders. This is the real "data understanding".

NO DEPENDENCIES. Standard library only - no pandas, no numpy, nothing compiled.
Written this way because Windows Application Control blocks pandas' DLLs on
some machines.

It answers, with numbers instead of assumptions:
  1. Do we get the number of bidders?           -> signal "single bidder"
  2. Do we get attached documents, and PDFs?    -> the Spec Auditor agent
  3. Do we get a stable company identifier?     -> the company graph
  4. Do we get cancelled / contested status?    -> ground truth
  5. Do we get both bidding dates?              -> signal "deadline too short"

Run:
    python 03_profile.py
    python 03_profile.py --in data/tenders.jsonl --out reports

Outputs printed to screen plus:
    reports/field_presence.csv   every top-level field and its fill rate
    reports/tenders_flat.csv     one row per tender, ready for step 4
"""

import argparse
import csv
import json
import os
import statistics
from collections import Counter
from datetime import datetime


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def load(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def parse_dt(s):
    """Parse Prozorro's ISO timestamps, e.g. 2026-01-15T02:03:57.704239+02:00"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def pct(n, total):
    return f"{100.0 * n / total:5.1f}%" if total else "  n/a"


def show_counts(counter, limit=None, indent="  "):
    items = counter.most_common(limit)
    if not items:
        print(indent + "(none)")
        return
    width = max(len(str(k)) for k, _ in items)
    for k, v in items:
        print(f"{indent}{str(k):<{width}}  {v}")


def stats_line(values):
    """min / median / mean / max for a list of numbers."""
    if not values:
        return "no values"
    return (f"min {min(values):,.1f} | median {statistics.median(values):,.1f} | "
            f"mean {statistics.fmean(values):,.1f} | max {max(values):,.1f}")


# --------------------------------------------------------------------------
# flatten one tender into the handful of fields we care about
# --------------------------------------------------------------------------

def flatten(t):
    tp = t.get("tenderPeriod") or {}
    ep = t.get("enquiryPeriod") or {}
    aw = t.get("awards") or []
    bids = t.get("bids") or []
    docs = t.get("documents") or []
    items = t.get("items") or []
    value = t.get("value") or {}
    pe_id = (t.get("procuringEntity") or {}).get("identifier") or {}

    cpv = (items[0].get("classification") or {}).get("id") if items else None

    # first supplier of the first award, when there is one
    sup_id = sup_scheme = None
    if aw:
        sups = aw[0].get("suppliers") or []
        if sups:
            ident = sups[0].get("identifier") or {}
            sup_id = ident.get("id")
            sup_scheme = ident.get("scheme")

    doc_formats = [str(d.get("format") or "") for d in docs]

    start = parse_dt(tp.get("startDate"))
    end = parse_dt(tp.get("endDate"))
    bidding_days = (end - start).total_seconds() / 86400 if (start and end) else None

    n_bids = len(bids) if bids else t.get("numberOfBids")

    return {
        "id": t.get("id"),
        "tenderID": t.get("tenderID"),
        "method": t.get("procurementMethodType"),
        "status": t.get("status"),

        "value_amount": value.get("amount"),
        "value_currency": value.get("currency"),

        # Q1 - bidders
        "n_bids": n_bids,
        "has_bids_array": bool(bids),
        "n_awards": len(aw),

        # Q2 - documents
        "n_docs": len(docs),
        "n_pdf": sum(1 for fmt in doc_formats if "pdf" in fmt.lower()),

        # Q3 - company identifiers
        "buyer_scheme": pe_id.get("scheme"),
        "buyer_id": pe_id.get("id"),
        "supplier_scheme": sup_scheme,
        "supplier_id": sup_id,

        # Q4 - ground truth
        "n_complaints": len(t.get("complaints") or []),
        "has_cancellations": bool(t.get("cancellations")),

        # Q5 - dates
        "tender_start": tp.get("startDate"),
        "tender_end": tp.get("endDate"),
        "enquiry_start": ep.get("startDate"),
        "enquiry_end": ep.get("endDate"),
        "bidding_days": round(bidding_days, 2) if bidding_days is not None else None,

        "cpv": cpv,
        "cpv_division": cpv[:2] if cpv else None,
    }


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/tenders.jsonl")
    ap.add_argument("--out", dest="outdir", default="reports")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    tenders = load(args.inp)
    n = len(tenders)
    print(f"Loaded {n} tenders from {args.inp}\n")
    if n == 0:
        print("Nothing to profile. Run step 2 first.")
        return

    # ---------- 1. which top-level fields exist, and how often ----------
    presence = Counter()
    for t in tenders:
        for k, v in t.items():
            if v not in (None, "", [], {}):
                presence[k] += 1

    with open(f"{args.outdir}/field_presence.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["field", "filled", "pct"])
        for k, v in presence.most_common():
            w.writerow([k, v, round(100.0 * v / n, 1)])

    print("=" * 62)
    print("TOP-LEVEL FIELDS (filled = not null / not empty)")
    print("=" * 62)
    for k, v in presence.most_common(40):
        print(f"  {k:<28} {v:>6}  {pct(v, n)}")

    # ---------- 2. flat table ----------
    rows = [flatten(t) for t in tenders]
    fieldnames = list(rows[0].keys())
    with open(f"{args.outdir}/tenders_flat.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    # ---------- 3. the five questions ----------
    print("\n" + "=" * 62)
    print("THE FIVE QUESTIONS, ANSWERED ON REAL DATA")
    print("=" * 62)

    # Q1
    print("\n[1] NUMBER OF BIDDERS")
    with_array = sum(1 for r in rows if r["has_bids_array"])
    known = [r["n_bids"] for r in rows if isinstance(r["n_bids"], (int, float))]
    print(f"  bids array present .......... {pct(with_array, n)}")
    print(f"  n_bids known ................ {pct(len(known), n)}")
    if known:
        single = sum(1 for b in known if b <= 1)
        print(f"  single bidder ............... {pct(single, len(known))} of those")
        print(f"  mean bidders ................ {statistics.fmean(known):.2f}")
        print("  distribution:")
        show_counts(Counter(known), limit=10, indent="    ")
    else:
        print("  -> bids are hidden until the procedure is complete.")
        print("     Re-run step 2 with --statuses complete and compare.")

    # Q2
    print("\n[2] DOCUMENTS")
    with_doc = sum(1 for r in rows if r["n_docs"] > 0)
    with_pdf = sum(1 for r in rows if r["n_pdf"] > 0)
    doc_counts = [r["n_docs"] for r in rows]
    print(f"  at least one document ....... {pct(with_doc, n)}")
    print(f"  at least one PDF ............ {pct(with_pdf, n)}")
    print(f"  mean documents per tender ... {statistics.fmean(doc_counts):.1f}")

    # Q3
    print("\n[3] COMPANY IDENTIFIERS")
    buyers = [r["buyer_id"] for r in rows if r["buyer_id"]]
    sups = [r["supplier_id"] for r in rows if r["supplier_id"]]
    print(f"  buyer identifier ............ {pct(len(buyers), n)}")
    print(f"  supplier identifier ......... {pct(len(sups), n)}")
    print(f"  distinct buyers ............. {len(set(buyers))}")
    print(f"  distinct suppliers .......... {len(set(sups))}")
    print("  buyer schemes:")
    show_counts(Counter(r["buyer_scheme"] for r in rows if r["buyer_scheme"]),
                limit=5, indent="    ")

    # Q4
    print("\n[4] GROUND TRUTH SIGNALS")
    compl = sum(1 for r in rows if r["n_complaints"] > 0)
    canc = sum(1 for r in rows if r["has_cancellations"])
    print(f"  has complaints .............. {pct(compl, n)}")
    print(f"  has cancellations ........... {pct(canc, n)}")
    print("  status counts:")
    show_counts(Counter(r["status"] for r in rows), indent="    ")

    # Q5
    print("\n[5] DATES")
    days = [r["bidding_days"] for r in rows if r["bidding_days"] is not None]
    enq = sum(1 for r in rows if r["enquiry_start"])
    print(f"  tenderPeriod complete ....... {pct(len(days), n)}")
    print(f"  enquiryPeriod present ....... {pct(enq, n)}")
    if days:
        print(f"  bidding window (days): {stats_line(days)}")
        short = sum(1 for d in days if d < 7)
        print(f"  windows under 7 days ........ {pct(short, len(days))}")

    # ---------- 4. context ----------
    print("\n" + "=" * 62)
    print("CONTEXT")
    print("=" * 62)

    print("\nprocurementMethodType:")
    show_counts(Counter(r["method"] for r in rows), indent="  ")

    print("\nTop 10 CPV divisions (first 2 digits):")
    show_counts(Counter(r["cpv_division"] for r in rows if r["cpv_division"]),
                limit=10, indent="  ")

    vals = [r["value_amount"] for r in rows
            if isinstance(r["value_amount"], (int, float))]
    if vals:
        cur = Counter(r["value_currency"] for r in rows if r["value_currency"])
        cur_name = cur.most_common(1)[0][0] if cur else "?"
        print(f"\nContract value ({cur_name}): {stats_line(vals)}")

    print(f"\nWrote {args.outdir}/field_presence.csv and "
          f"{args.outdir}/tenders_flat.csv")
    print("tenders_flat.csv is the input for step 4 (the charts).")


if __name__ == "__main__":
    main()