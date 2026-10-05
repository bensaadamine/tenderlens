"""
CANADA - STEP 1: profile the tender notices.

Replaces what took scripts 03, 04 and 06 on the Ukrainian data. One pass,
no pandas (Windows Application Control blocks its compiled DLL).

Answers:
  A. What is being bought, and by which procurement method?
  B. Bidding windows - publication to closing, per method. The legal-floor
     signal, computable here with no extra data.
  C. limitedTenderingReason - the buyer's written justification for avoiding
     competition. This replaces the single-bidder signal we lost.
  D. Attachment coverage - what fraction of tenders we can actually read.
  E. Category depth - which UNSPSC groups have enough peers to compare
     specifications within. This sets the project scope.

Run:
    pip install requests
    python 01_profile_tenders.py

It reuses data/tenders.csv if present, otherwise downloads it (185 MB).
If you already have it from the spike, copy it in first:
    copy ..\\canada\\canada_spike\\all_tenders.csv data\\tenders.csv
"""

import argparse
import csv
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime

import requests

csv.field_size_limit(10_000_000)

FEED = ("https://canadabuys.canada.ca/opendata/pub/"
        "tenderNoticeComplete-avisAppelOffresComplet.csv")

C = {  # the columns we care about, by their bilingual names
    "ref": "referenceNumber-numeroReference",
    "sol": "solicitationNumber-numeroSollicitation",
    "title": "title-titre-eng",
    "desc": "tenderDescription-descriptionAppelOffres-eng",
    "pub": "publicationDate-datePublication",
    "close": "tenderClosingDate-appelOffresDateCloture",
    "status": "tenderStatus-appelOffresStatut-eng",
    "unspsc": "unspsc",
    "unspsc_desc": "unspscDescription-eng",
    "category": "procurementCategory-categorieApprovisionnement",
    "method": "procurementMethod-methodeApprovisionnement-eng",
    "criteria": "selectionCriteria-criteresSelection-eng",
    "ltr": "limitedTenderingReason-raisonAppelOffresLimite-eng",
    "trade": "tradeAgreements-accordsCommerciaux-eng",
    "buyer": "contractingEntityName-nomEntitContractante-eng",
    "att": "attachment-piecesJointes-eng",
}

# procurementCategory codes. Only goods carry numeric technical parameters;
# the same lesson as Ukraine, so the scope filter is the same shape.
#
# The field is messy: values are prefixed with "*" and a tender can carry
# several, separated by newlines ("*GD\n*SRV"). So we parse it as a set.
GOODS = {"GD"}


def categories(raw):
    """'*GD\\n*SRV' -> {'GD', 'SRV'}"""
    if not raw:
        return set()
    return {t.strip().lstrip("*").strip().upper()
            for t in re.split(r"[\r\n,;|]+", raw) if t.strip()}


def first_unspsc(raw):
    """The feed may hold several codes; take the first all-digit one."""
    if not raw:
        return None
    for t in re.split(r"[\r\n,;|\s]+", raw):
        d = re.sub(r"\D", "", t)
        if len(d) >= 4:
            return d
    return None


def download(path):
    print(f"Downloading tender notices (~185 MB)...")
    with requests.get(FEED, stream=True, timeout=900,
                      headers={"User-Agent": "TenderLens/0.1"}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            done = 0
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if done % (20 << 20) < (1 << 20):
                    print(f"  {done/1e6:.0f} MB")
    print(f"  saved {os.path.getsize(path)/1e6:.0f} MB\n")


def parse_date(s):
    if not s:
        return None
    s = s.strip()[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def pct(a, b):
    return f"{100.0*a/b:5.1f}%" if b else "  n/a"


def quantiles(vals):
    if not vals:
        return None
    v = sorted(vals)
    n = len(v)
    q = lambda p: v[min(int(p * n), n - 1)]
    return q(0.0), q(0.25), q(0.50), q(0.75), q(1.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenders", default="data/tenders.csv")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--min-usable", type=int, default=30)
    args = ap.parse_args()

    os.makedirs("data", exist_ok=True)
    os.makedirs(args.out, exist_ok=True)

    if not os.path.exists(args.tenders):
        download(args.tenders)

    rows = []
    with open(args.tenders, encoding="utf-8-sig", errors="replace",
              newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    n = len(rows)
    print(f"Loaded {n:,} tender notices\n")

    # ------------------------------------------------------------------
    print("=" * 66)
    print("A. WHAT IS BEING BOUGHT, AND HOW")
    print("=" * 66)

    cat = Counter()
    for r in rows:
        for c in (categories(r.get(C["category"])) or {"(blank)"}):
            cat[c] += 1
    print("\nprocurementCategory (a tender may count in several):")
    for k, v in cat.most_common():
        print(f"  {k:10s} {v:7,}  {pct(v, n)}")
    print("  GD=goods  SRV=services  CNST=construction  SRVTGD=services for goods")

    meth = Counter(r.get(C["method"], "") for r in rows)
    print("\nprocurementMethod:")
    for k, v in meth.most_common(10):
        print(f"  {(k or '(blank)')[:44]:46s} {v:7,}  {pct(v, n)}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("B. BIDDING WINDOWS  (publication -> closing)")
    print("=" * 66)

    by_method = defaultdict(list)
    no_dates = 0
    for r in rows:
        p, c = parse_date(r.get(C["pub"])), parse_date(r.get(C["close"]))
        if not p or not c:
            no_dates += 1
            continue
        d = (c - p).total_seconds() / 86400
        if 0 < d < 400:
            by_method[r.get(C["method"], "")].append(d)

    print(f"\n  tenders without usable dates ... {pct(no_dates, n)}\n")
    print(f"  {'method':46s} {'n':>6}  {'min':>6} {'25%':>6} {'med':>6} "
          f"{'75%':>6} {'max':>6}")
    for m, vals in sorted(by_method.items(), key=lambda kv: -len(kv[1]))[:10]:
        q = quantiles(vals)
        print(f"  {(m or '(blank)')[:44]:46s} {len(vals):6,}  "
              f"{q[0]:6.1f} {q[1]:6.1f} {q[2]:6.1f} {q[3]:6.1f} {q[4]:6.1f}")
    print("\n  A tight cluster = everyone sitting on the legal minimum.")
    print("  The thin tail below it is the high-precision signal.")

    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("C. LIMITED TENDERING REASON  (the declared justification)")
    print("=" * 66)

    ltr = Counter(r.get(C["ltr"], "").strip() for r in rows)
    has_ltr = n - ltr.get("", 0)
    print(f"\n  tenders giving a reason ... {pct(has_ltr, n)}  ({has_ltr:,})\n")
    for k, v in ltr.most_common(12):
        if not k:
            continue
        print(f"  {v:6,}  {pct(v, has_ltr)}  {k[:78]}")
    print("\n  This is a claim the buyer makes in writing. Each one is testable")
    print("  against the facts - that is what makes it better than a bid count.")

    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("D. ATTACHMENTS  (can we read the specification?)")
    print("=" * 66)

    direct = portal = none = 0
    hosts = Counter()
    for r in rows:
        a = (r.get(C["att"]) or "").strip()
        if len(a) < 10:
            none += 1
            continue
        urls = [u for u in re.split(r"[,;|\s]+", a) if u.startswith("http")]
        for u in urls:
            hosts[re.sub(r"^https?://([^/]+).*", r"\1", u)] += 1
        if any("canadabuys.canada.ca" in u for u in urls):
            direct += 1
        else:
            portal += 1

    print(f"\n  no attachment ............... {pct(none, n)}")
    print(f"  attachment, direct download . {pct(direct, n)}   <-- usable")
    print(f"  attachment, portal only ..... {pct(portal, n)}   (login wall)")
    print("\n  hosts:")
    for h, c in hosts.most_common(6):
        print(f"    {c:7,}  {h}")

    # ------------------------------------------------------------------
    print("\n" + "=" * 66)
    print("E. CATEGORY DEPTH  (usable = goods + direct attachment)")
    print("=" * 66)

    depth4, depth6, labels = Counter(), Counter(), {}
    n_goods = n_goods_att = n_goods_att_code = 0
    for r in rows:
        if not (categories(r.get(C["category"])) & GOODS):
            continue
        n_goods += 1
        if "canadabuys.canada.ca" not in (r.get(C["att"]) or ""):
            continue
        n_goods_att += 1
        u = first_unspsc(r.get(C["unspsc"]))
        if not u or len(u) < 4:
            continue
        n_goods_att_code += 1
        depth4[u[:4]] += 1
        if len(u) >= 6:
            depth6[u[:6]] += 1
            labels.setdefault(u[:6], (r.get(C["unspsc_desc"]) or "").strip()[:48])
        labels.setdefault(u[:4], (r.get(C["unspsc_desc"]) or "").strip()[:48])

    print(f"\n  goods tenders ................. {n_goods:,}")
    print(f"  ...with a direct attachment ... {n_goods_att:,}")
    print(f"  ...and a UNSPSC code .......... {n_goods_att_code:,}   <-- usable")

    for name, d in (("UNSPSC-4 (family)", depth4), ("UNSPSC-6 (class)", depth6)):
        ge = sum(1 for v in d.values() if v >= args.min_usable)
        print(f"\n  {name}: {len(d)} groups | "
              f">={args.min_usable}: {ge} | >=60: {sum(1 for v in d.values() if v >= 60)}")

    print(f"\n  Top 20 UNSPSC-4 groups with >= {args.min_usable} usable tenders:\n")
    sel = [(v, k) for k, v in depth4.items() if v >= args.min_usable]
    sel.sort(reverse=True)
    for v, k in sel[:20]:
        print(f"    {k}  {v:5,}   {labels.get(k,'')}")

    scope = [k for _, k in sel[:20]]
    with open(os.path.join(args.out, "selected_unspsc4.json"), "w") as f:
        import json
        json.dump(sorted(scope), f, indent=2)

    print(f"\n  usable tenders in the top 20 ... {sum(v for v, _ in sel[:20]):,}")
    print(f"  Written: {args.out}/selected_unspsc4.json  <- the project scope")


if __name__ == "__main__":
    main()