"""
STEP 5 - Choose the categories to work on, and count the legal-floor violations.

Ranking CPV-4 groups by raw volume is misleading: the biggest groups are road
works, fuel and food, whose documents are bills of quantities and price lists,
not technical parameters. The over-precision model needs numeric requirements,
and those only exist in GOODS.

So a category is usable only if it has enough tenders that are:
    - mainProcurementCategory == "goods"
    - AND carry a readable specification document

This script ranks by that number, not by raw count.

It also counts tenders whose bidding window is below the legal minimum for
their procedure - a rare, legally grounded signal, unlike single bidding.

Run:
    python 06_pick_categories.py
    python 06_pick_categories.py --top 20 --min-usable 30
"""

import argparse
import json
from collections import defaultdict

import pandas as pd

SPEC_TYPES = {"biddingDocuments", "technicalSpecifications",
              "tenderNotice", "eligibilityCriteria", "evaluationCriteria"}
TEXT_EXT = ("doc", "docx", "odt", "rtf", "txt", "pdf", "xls", "xlsx", "ods")

# Legal minimum bidding window in days, by procedure.
# aboveThreshold: 15 days is the statutory floor in the Ukrainian law for
# open procedures above the EU-equivalent threshold. Anything below it is a
# violation on its face, not a heuristic.
# Confirm these against the current law text before quoting them in a report.
LEGAL_MIN_DAYS = {
    "aboveThreshold": 15,
    "aboveThresholdUA": 15,
    "aboveThresholdEU": 30,
    "belowThreshold": 2,
}


def has_readable_spec(t):
    for d in (t.get("documents") or []):
        if (d.get("documentType") or "") not in SPEC_TYPES:
            continue
        title = str(d.get("title") or "").lower()
        fmt = str(d.get("format") or "").lower()
        if any(title.endswith("." + e) for e in TEXT_EXT) or "pdf" in fmt \
           or "word" in fmt or "msword" in fmt:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/tenders.jsonl")
    ap.add_argument("--out", dest="outdir", default="reports")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-usable", type=int, default=30)
    args = ap.parse_args()

    rows = []
    with open(args.inp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            t = json.loads(line)
            items = t.get("items") or []
            cpv = t.get("_cpv") or (((items[0].get("classification") or {}).get("id"))
                                    if items else None)
            tp = t.get("tenderPeriod") or {}
            bids = t.get("bids") or []
            rows.append({
                "cpv4": cpv[:4] if cpv else None,
                "cpv2": cpv[:2] if cpv else None,
                "kind": t.get("mainProcurementCategory"),
                "method": t.get("procurementMethodType"),
                "status": t.get("status"),
                "spec": has_readable_spec(t),
                "n_bids": len(bids) if bids else None,
                "value": (t.get("value") or {}).get("amount"),
                "start": tp.get("startDate"),
                "end": tp.get("endDate"),
            })

    df = pd.DataFrame(rows)
    df["start"] = pd.to_datetime(df.start, errors="coerce", utc=True)
    df["end"] = pd.to_datetime(df.end, errors="coerce", utc=True)
    df["days"] = (df.end - df.start).dt.total_seconds() / 86400
    print(f"Loaded {len(df)} tenders\n")

    # ---------------------------------------------------------------
    print("=" * 70)
    print("WHAT KIND OF THING IS BEING BOUGHT")
    print("=" * 70)
    print(df.kind.value_counts(dropna=False).to_string())
    print("\n  Only `goods` carry numeric technical parameters.")
    print("  `works` = bills of quantities.  `services` = scope of work.")

    # ---------------------------------------------------------------
    print("\n" + "=" * 70)
    print(f"CATEGORY RANKING  (usable = goods AND readable spec)")
    print("=" * 70)

    g = df.groupby("cpv4").agg(
        total=("cpv4", "size"),
        goods=("kind", lambda s: (s == "goods").sum()),
        with_spec=("spec", "sum"),
    )
    usable = (df[(df.kind == "goods") & (df.spec)]
              .groupby("cpv4").size().rename("usable"))
    g = g.join(usable).fillna({"usable": 0})
    g["usable"] = g.usable.astype(int)

    # Bidding behaviour per category - this is the baseline each signal
    # will later be measured against.
    scored = df[df.n_bids.notna()]
    beh = scored.groupby("cpv4").agg(
        n_scored=("n_bids", "size"),
        median_bids=("n_bids", "median"),
        single_rate=("n_bids", lambda s: round(100 * (s <= 1).mean(), 1)),
    )
    g = g.join(beh)

    g = g.sort_values("usable", ascending=False)
    g.to_csv(f"{args.outdir}/cpv_ranking.csv")

    sel = g[g.usable >= args.min_usable].head(args.top)
    print(f"\nTop {args.top} categories with at least {args.min_usable} usable tenders:\n")
    print(sel.to_string())

    print(f"\n  categories meeting the bar ... {(g.usable >= args.min_usable).sum()}")
    print(f"  tenders they cover ........... {int(sel.usable.sum())}")
    if (g.usable >= args.min_usable).sum() < args.top:
        need = len(df) * args.top / max((g.usable >= args.min_usable).sum(), 1)
        print(f"\n  Not enough categories yet. Rough target: ~{need:,.0f} tenders.")
        print("  Keep harvesting: python 05_harvest.py --target <bigger>")

    # Keep this list - it is the project's scope from here on.
    with open(f"{args.outdir}/selected_cpv4.json", "w") as f:
        json.dump(sorted(sel.index.tolist()), f, indent=2)
    print(f"\n  Written: {args.outdir}/cpv_ranking.csv")
    print(f"  Written: {args.outdir}/selected_cpv4.json  <- the project scope")

    # ---------------------------------------------------------------
    print("\n" + "=" * 70)
    print("BIDDING WINDOWS BELOW THE LEGAL MINIMUM")
    print("=" * 70)
    print("\n  A rare, legally grounded flag - unlike single bidding, which is")
    print("  the norm. This is a violation on its face.\n")

    total_viol = 0
    for method, floor in LEGAL_MIN_DAYS.items():
        sub = df[(df.method == method) & (df.days.notna())]
        if not len(sub):
            continue
        viol = sub[sub.days < floor]
        total_viol += len(viol)
        print(f"  {method:18s} floor {floor:>2}d | {len(sub):5d} tenders | "
              f"{len(viol):4d} below ({100*len(viol)/len(sub):4.1f}%)")
        if len(viol):
            print(f"  {'':18s}           shortest observed: {viol.days.min():.1f} days")

    print(f"\n  TOTAL below the legal floor: {total_viol} of {len(df)} "
          f"({100*total_viol/len(df):.2f}%)")
    print("\n  THIS is what a real signal looks like: rare, defined by law,")
    print("  and checkable from two dates. Put the number in the presentation.")
    print("\n  Caveat: verify the floors in LEGAL_MIN_DAYS against the current")
    print("  law text before quoting them. They also depend on the publication")
    print("  date rather than the period start in some cases.")


if __name__ == "__main__":
    main()
