"""
STEP 3b - Diagnose the four things step 3 could not answer.

Step 3 told us what exists. This tells us whether it is USABLE:

  A. Documents  - which formats, which documentTypes? How many tenders have a
                  real technical specification rather than a signature file?
  B. Baselines  - single bidding and short deadlines are the norm. What is the
                  norm *per procedure type and value band*? That is the only
                  way those signals mean anything.
  C. Complaints - step 3 only looked at the tender root. Complaints also live
                  under awards, lots and qualifications. Count them everywhere.
  D. Lots       - most tenders have lots, and bids attach to lots. How much of
                  the analysis has to happen at lot level?

Run:
    python 04_diagnose.py
    python 04_diagnose.py --in data/tenders.jsonl
"""

import argparse
import json
from collections import Counter

import pandas as pd

# documentType values that plausibly contain the technical requirements.
# Everything else is notices, protocols, signatures, award paperwork.
SPEC_TYPES = {
    "biddingDocuments",
    "technicalSpecifications",
    "tenderNotice",
    "eligibilityCriteria",
    "evaluationCriteria",
}

TEXT_EXT = ("doc", "docx", "odt", "rtf", "txt", "pdf", "xls", "xlsx", "ods")


def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def deep_count(obj, key):
    """Count entries of a list-valued key anywhere in a nested structure."""
    n = 0
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, list):
                n += len(v)
            else:
                n += deep_count(v, key)
    elif isinstance(obj, list):
        for v in obj:
            n += deep_count(v, key)
    return n


def ext_of(doc):
    """Best-effort file extension, from the title or the MIME format."""
    title = str(doc.get("title") or "").lower()
    if "." in title:
        ext = title.rsplit(".", 1)[-1].strip()
        if 1 < len(ext) <= 5:
            return ext
    fmt = str(doc.get("format") or "").lower()
    if "pdf" in fmt:
        return "pdf"
    if "word" in fmt or "msword" in fmt:
        return "doc"
    if "sheet" in fmt or "excel" in fmt:
        return "xls"
    return fmt.split("/")[-1][:12] if fmt else "unknown"


def pct(a, b):
    return f"{100.0 * a / b:5.1f}%" if b else "  n/a"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/tenders.jsonl")
    ap.add_argument("--out", dest="outdir", default="reports")
    args = ap.parse_args()

    tenders = load(args.inp)
    n = len(tenders)
    print(f"Loaded {n} tenders\n")

    # =================================================================
    # A. DOCUMENTS - the question that decides whether Agent 2 can exist
    # =================================================================
    print("=" * 62)
    print("A. DOCUMENTS")
    print("=" * 62)

    ext_c, type_c = Counter(), Counter()
    with_spec = with_textual_spec = 0

    for t in tenders:
        docs = t.get("documents") or []
        has_spec = has_text = False
        for d in docs:
            e = ext_of(d)
            dt = d.get("documentType") or "(none)"
            ext_c[e] += 1
            type_c[dt] += 1
            if dt in SPEC_TYPES:
                has_spec = True
                if e in TEXT_EXT:
                    has_text = True
        with_spec += has_spec
        with_textual_spec += has_text

    print("\nFile extensions (all documents):")
    total_docs = sum(ext_c.values())
    for e, c in ext_c.most_common(15):
        print(f"  {e:14s} {c:6d}  {pct(c, total_docs)}")

    print("\ndocumentType:")
    for dt, c in type_c.most_common(15):
        print(f"  {dt:28s} {c:6d}  {pct(c, total_docs)}")

    print(f"\n  tenders with a spec-type document ........ {pct(with_spec, n)}")
    print(f"  ...and in a machine-readable format ...... {pct(with_textual_spec, n)}")
    print("\n  ^ THIS second number is the real ceiling for the Spec Auditor agent.")
    print("    Everything below it is a tender we simply cannot read.")

    # =================================================================
    # B. BASELINES - a signal only means something against its own group
    # =================================================================
    print("\n" + "=" * 62)
    print("B. BASELINES PER GROUP")
    print("=" * 62)

    rows = []
    for t in tenders:
        bids = t.get("bids") or []
        tp = t.get("tenderPeriod") or {}
        items = t.get("items") or []
        cpv = ((items[0].get("classification") or {}).get("id")) if items else None
        rows.append({
            "method": t.get("procurementMethodType"),
            "status": t.get("status"),
            "n_bids": len(bids) if bids else None,
            "value": (t.get("value") or {}).get("amount"),
            "start": tp.get("startDate"),
            "end": tp.get("endDate"),
            "cpv2": cpv[:2] if cpv else None,
            "cpv4": cpv[:4] if cpv else None,
        })

    df = pd.DataFrame(rows)
    df["start"] = pd.to_datetime(df.start, errors="coerce", utc=True)
    df["end"] = pd.to_datetime(df.end, errors="coerce", utc=True)
    df["days"] = (df.end - df.start).dt.total_seconds() / 86400

    # Only tenders that reached a stage where bids are published.
    scored = df[df.n_bids.notna()].copy()
    print(f"\nTenders with bids published: {len(scored)} of {n}")

    if len(scored):
        print("\nSingle-bidder rate BY PROCEDURE (this is the baseline to beat):")
        g = scored.groupby("method").agg(
            tenders=("n_bids", "size"),
            single_rate=("n_bids", lambda s: (s <= 1).mean()),
            median_bids=("n_bids", "median"),
        ).sort_values("tenders", ascending=False)
        g["single_rate"] = (100 * g.single_rate).round(1)
        print(g.to_string())

        scored["band"] = pd.cut(
            scored.value,
            bins=[0, 50_000, 200_000, 1_000_000, 5_000_000, float("inf")],
            labels=["<50k", "50-200k", "200k-1M", "1-5M", ">5M"])
        print("\nSingle-bidder rate BY VALUE BAND (UAH):")
        g2 = scored.groupby("band", observed=True).agg(
            tenders=("n_bids", "size"),
            single_rate=("n_bids", lambda s: (s <= 1).mean()),
        )
        g2["single_rate"] = (100 * g2.single_rate).round(1)
        print(g2.to_string())

    print("\nBidding window BY PROCEDURE (days):")
    g3 = df[df.days.notna()].groupby("method").days.describe()[
        ["count", "min", "25%", "50%", "75%", "max"]].round(1)
    print(g3.to_string())
    print("\n  ^ A 5-day window is suspicious for aboveThreshold and perfectly")
    print("    normal for priceQuotation. Never compare across procedures.")

    # =================================================================
    # C. COMPLAINTS - look everywhere, not just the root
    # =================================================================
    print("\n" + "=" * 62)
    print("C. GROUND TRUTH, COUNTED PROPERLY")
    print("=" * 62)

    root = deep = canc = quals = 0
    for t in tenders:
        root += 1 if (t.get("complaints") or []) else 0
        deep += 1 if deep_count(t, "complaints") else 0
        canc += 1 if deep_count(t, "cancellations") else 0
        quals += 1 if (t.get("qualifications") or []) else 0

    print(f"\n  complaints at tender root only ....... {root:4d}  {pct(root, n)}")
    print(f"  complaints ANYWHERE in the object .... {deep:4d}  {pct(deep, n)}")
    print(f"  cancellations anywhere ............... {canc:4d}  {pct(canc, n)}")
    print(f"  has qualifications block ............. {quals:4d}  {pct(quals, n)}")
    print(f"  status 'unsuccessful' ................ "
          f"{(df.status == 'unsuccessful').sum():4d}  "
          f"{pct((df.status=='unsuccessful').sum(), n)}")
    print(f"  status 'cancelled' ................... "
          f"{(df.status == 'cancelled').sum():4d}  "
          f"{pct((df.status=='cancelled').sum(), n)}")
    print("\n  If 'anywhere' is much larger than 'root', the real label count is")
    print("  higher than step 3 suggested. Extrapolate to decide the sample size:")
    if deep:
        print(f"  -> about {deep/n*10000:.0f} labelled cases per 10,000 tenders fetched.")

    # =================================================================
    # D. LOTS - does the analysis have to move to lot level?
    # =================================================================
    print("\n" + "=" * 62)
    print("D. LOT STRUCTURE")
    print("=" * 62)

    lot_counts = Counter()
    bids_on_lots = 0
    for t in tenders:
        lots = t.get("lots") or []
        lot_counts[len(lots)] += 1
        for b in (t.get("bids") or []):
            if b.get("lotValues"):
                bids_on_lots += 1
                break

    multi = sum(c for k, c in lot_counts.items() if k > 1)
    print(f"\n  no lots ............. {pct(lot_counts.get(0,0), n)}")
    print(f"  exactly one lot ..... {pct(lot_counts.get(1,0), n)}")
    print(f"  more than one lot ... {pct(multi, n)}")
    print(f"  tenders whose bids carry lotValues ... {pct(bids_on_lots, n)}")
    print("\n  Multi-lot tenders need lot-level analysis: one tender can have a")
    print("  competitive lot and a single-bidder lot at the same time.")

    # =================================================================
    # E. CATEGORY DEPTH - can we compare specs within a category?
    # =================================================================
    print("\n" + "=" * 62)
    print("E. CATEGORY DEPTH (the over-precision model needs peers)")
    print("=" * 62)

    d4 = df.cpv4.value_counts()
    print(f"\n  distinct CPV-4 groups in sample ...... {len(d4)}")
    print(f"  groups with >= 10 tenders ............ {(d4 >= 10).sum()}")
    print(f"  groups with >= 30 tenders ............ {(d4 >= 30).sum()}")
    print("\n  Top 10 CPV-4 groups:")
    print(d4.head(10).to_string())
    print("\n  To compare a requirement against its peers you need roughly 30+")
    print("  tenders in the SAME CPV-4 group. Scale your sample from this number,")
    print("  or narrow the project to 2-3 categories and go deep instead of wide.")


if __name__ == "__main__":
    main()
