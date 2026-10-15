"""
M1 SCREENER.

The first agent in the chain. It reads only the NOTICE - never a document -
and decides what deserves the expensive work:

    from m1_screener import screen
    screen("cb-258-72766150")   ->  {... "route": "deep" ...}

Routes: deep (send to M2), watch (keep on the list), close (nothing cheap
says look here). It never says a tender is rigged; it says where the
expensive readers should spend their time, with the reason attached
(decision 3).

Why an agent and not a filter: M1 CHOOSES. The same short bidding window
means one thing on an open competition and nothing on an ACAN, where a
short window is the published point of the exercise. That judgement is why
the route is computed from signals rather than from a score cut-off.

--------------------------------------------------------------- the signals

Four, all free, all off the feed. Measured on 30,420 notices:

  ACAN - 1,434 by method, 1,697 by notice type. The buyer names the
      supplier it intends to award to and invites a challenge. It is legal
      and it is also exactly where a lock-in would live, so it is the
      strongest cheap signal we have.

  limitedTenderingReason - the buyer's written justification for avoiding
      competition. 8,720 notices carry the field but 8,310 say literally
      "None"; about 410 are real, of which 282 are "Exclusive Rights" - the
      buyer asserting one supplier can do it. PARSE FOR "None", or the
      signal fires on a quarter of the corpus.

  Bidding window - publication to closing. Short favours whoever already
      knew. Measured AGAINST ITS OWN METHOD (decision 5): the open-bidding
      median is 28.6 days and selective tendering runs shorter by design,
      so an absolute threshold would flag a whole procurement method and
      call it a finding.

  tradeAgreements = "*None" - 4,009 notices. CFTA and CETA are what oblige
      a functional specification and the "or equivalent" that level 1
      checks. A tender under no agreement is not breaking a rule, but the
      rule that protects the competition does not apply to it.

WHAT IS DELIBERATELY NOT HERE: contract value. The notice feed has no value
field, and the contracts file does not join to it (best match 0.8% -
`procurement_id` is a department-internal reference). CLAUDE.md lists M1 as
screening on value; it cannot, and pretending otherwise would put a column
of zeroes in the audit queue.

Also not here: buyer identity, region, category. Decision 4 - market
concentration is context, not evidence. A department that buys a lot of
trucks is a department that buys a lot of trucks.
"""

import argparse
import collections
import csv
import io
import json
import os
import re
import statistics
import sys
from datetime import datetime

csv.field_size_limit(10_000_000)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TENDERS = "data/tenders.csv"
TEXT_DIR = "reports/text"

C_REF = "referenceNumber-numeroReference"
C_TITLE = "title-titre-eng"
C_PUB = "publicationDate-datePublication"
C_CLOSE = "tenderClosingDate-appelOffresDateCloture"
C_METHOD = "procurementMethod-methodeApprovisionnement-eng"
C_NOTICE = "noticeType-avisType-eng"
C_LTR = "limitedTenderingReason-raisonAppelOffresLimite-eng"
C_TRADE = "tradeAgreements-accordsCommerciaux-eng"
C_SELECT = "selectionCriteria-criteresSelection-eng"
C_CAT = "procurementCategory-categorieApprovisionnement"
C_UNSPSC = "unspsc"
C_ENTITY = "contractingEntityName-nomEntitContractante-eng"
C_STATUS = "tenderStatus-appelOffresStatut-eng"

RE_ACAN = re.compile(r"advance contract award", re.I)
RE_LIMITED = re.compile(r"limited tendering|non-competitive", re.I)

# "None" is not a reason. 8,310 of 8,720 notices carrying this field say
# exactly that, so a truthiness test makes the signal fire on a quarter of
# the corpus and the audit queue becomes a list of everything.
RE_NO_REASON = re.compile(r"^\s*\*?\s*(none|n/?a|not applicable|sans objet)"
                          r"\s*$", re.I)

WINDOW_Z = -1.0      # how far below its own method's median a window must
                     # sit. Robust z, so "unusually short FOR THIS METHOD".
MIN_METHOD_N = 20    # below this a method has no distribution worth
                     # comparing against, so the window signal abstains
                     # rather than guessing - same spirit as decision 13's
                     # insufficient_peers.


def parse_date(s):
    s = (s or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def load_notices(path=TENDERS):
    """{reference: row}. One streaming pass, stdlib csv (pandas is blocked)."""
    if not os.path.exists(path):
        raise SystemExit(f"missing {path} - run 01_profile_tenders.py")
    out = {}
    with io.open(path, encoding="utf-8-sig", errors="replace",
                 newline="") as f:
        for r in csv.DictReader(f):
            ref = (r.get(C_REF) or "").strip()
            if ref:
                out[ref] = r
    return out


def window_days(row):
    a, b = parse_date(row.get(C_PUB)), parse_date(row.get(C_CLOSE))
    if not a or not b:
        return None
    d = (b - a).total_seconds() / 86400.0
    return d if d >= 0 else None


def method_windows(notices):
    """Per method: median and MAD of the bidding window.

    This is decision 5 in one function. Selective tendering is shorter than
    open bidding by design, so the question is never "is 12 days short" but
    "is 12 days short for THIS method" - otherwise the screener flags a
    procurement method and calls it a signal.
    """
    by = collections.defaultdict(list)
    for r in notices.values():
        d = window_days(r)
        if d is not None:
            by[(r.get(C_METHOD) or "").strip()].append(d)
    stats = {}
    for m, ds in by.items():
        if len(ds) < MIN_METHOD_N:
            continue
        med = statistics.median(ds)
        mad = 1.4826 * statistics.median([abs(x - med) for x in ds])
        stats[m] = {"n": len(ds), "median": round(med, 1),
                    "mad": round(mad, 3)}
    return stats


def has_documents(reference, text_dir=TEXT_DIR):
    """Can M2 read anything at all? A routing fact, not a risk signal."""
    if not os.path.isdir(text_dir):
        return 0
    n = 0
    for grp in os.listdir(text_dir):
        d = os.path.join(text_dir, grp)
        if not os.path.isdir(d):
            continue
        n += sum(1 for f in os.listdir(d) if f.startswith(reference + "__"))
    return n


def screen(reference, notices=None, wstats=None, text_dir=TEXT_DIR):
    """Screen ONE tender. Returns route, signals and the reason for each."""
    notices = load_notices() if notices is None else notices
    row = notices.get(reference)
    if row is None:
        return {"reference": reference, "error": "not in the notice feed",
                "route": "close", "signals": []}
    wstats = method_windows(notices) if wstats is None else wstats

    method = (row.get(C_METHOD) or "").strip()
    notice = (row.get(C_NOTICE) or "").strip()
    ltr = (row.get(C_LTR) or "").strip()
    trade = (row.get(C_TRADE) or "").strip()
    signals = []

    # ---- strong: the buyer has already said competition is limited
    if RE_ACAN.search(method) or RE_ACAN.search(notice):
        signals.append({
            "signal": "acan", "weight": "strong",
            "why": "Advance Contract Award Notice - the buyer names the "
                   "supplier it intends to award to. Legal, and the place a "
                   "lock-in would live.",
            "evidence": notice or method})
    if RE_LIMITED.search(method):
        signals.append({
            "signal": "limited_tendering_method", "weight": "strong",
            "why": "Procurement method itself forgoes open competition.",
            "evidence": method})
    if ltr and not RE_NO_REASON.match(ltr):
        signals.append({
            "signal": "limited_tendering_reason", "weight": "strong",
            "why": "The buyer filed a written justification for avoiding "
                   "competition. ~410 of 30,420 notices carry a real one.",
            "evidence": ltr[:120]})

    # ---- weak: suggestive only, and never on their own
    d = window_days(row)
    st = wstats.get(method)
    if d is not None and st and st["mad"] > 0:
        z = (d - st["median"]) / st["mad"]
        if z <= WINDOW_Z:
            signals.append({
                "signal": "short_window", "weight": "weak",
                "why": f"{d:.1f} days against a median of {st['median']} "
                       f"for {method!r} (n={st['n']}). Short favours a "
                       f"bidder who already knew.",
                "evidence": f"z={z:.2f}"})
    elif d is not None and not st:
        signals.append({"signal": "window_not_comparable", "weight": "info",
                        "why": f"Too few notices on method {method!r} to say "
                               f"what a normal window is.",
                        "evidence": f"{d:.1f} days"})

    if re.match(r"^\s*\*?\s*none\s*$", trade, re.I):
        signals.append({
            "signal": "no_trade_agreement", "weight": "weak",
            "why": "Under no trade agreement, so the CFTA/CETA duty to write "
                   "a functional specification - the duty level 1 checks - "
                   "does not apply here.",
            "evidence": trade})

    strong = [s for s in signals if s["weight"] == "strong"]
    weak = [s for s in signals if s["weight"] == "weak"]

    docs = has_documents(reference, text_dir)
    if strong or len(weak) >= 2:
        route = "deep"
    elif weak:
        route = "watch"
    else:
        route = "close"

    # A tender M2 cannot read cannot be analysed, however it screened. Say
    # so plainly rather than sending it on to fail quietly downstream.
    if route == "deep" and not docs:
        route = "deep_unreadable"

    return {
        "reference": reference,
        "title": (row.get(C_TITLE) or "").strip()[:120],
        "entity": (row.get(C_ENTITY) or "").strip()[:80],
        "method": method, "notice_type": notice,
        "status": (row.get(C_STATUS) or "").strip(),
        "published": (row.get(C_PUB) or "")[:10],
        "window_days": round(d, 1) if d is not None else None,
        "documents": docs,
        "route": route,
        "strong": len(strong), "weak": len(weak),
        "signals": signals,
    }


def screen_all(notices=None, only_unspsc4=None, only_with_docs=False,
               text_dir=TEXT_DIR):
    notices = load_notices() if notices is None else notices
    wstats = method_windows(notices)
    out = []
    for ref, row in notices.items():
        if only_unspsc4:
            code = ""
            for t in re.split(r"[\r\n,;|\s]+", row.get(C_UNSPSC) or ""):
                dd = re.sub(r"\D", "", t)
                if len(dd) >= 4:
                    code = dd
                    break
            if code[:4] != only_unspsc4:
                continue
        res = screen(ref, notices, wstats, text_dir)
        if only_with_docs and not res.get("documents"):
            continue
        out.append(res)
    order = {"deep": 0, "deep_unreadable": 1, "watch": 2, "close": 3}
    out.sort(key=lambda r: (order.get(r["route"], 9), -r["strong"],
                            -r["weak"]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tender")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--group", help="restrict --all to a UNSPSC-4 group")
    ap.add_argument("--with-docs", action="store_true",
                    help="only tenders whose documents we actually have")
    ap.add_argument("--tenders", default=TENDERS)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    notices = load_notices(args.tenders)

    if args.all:
        q = screen_all(notices, args.group, args.with_docs)
        if args.json:
            print(json.dumps(q[:args.limit], indent=2))
            return
        counts = collections.Counter(r["route"] for r in q)
        print(f"\nscreened {len(q):,} notices"
              + (f" in UNSPSC-4 {args.group}" if args.group else ""))
        for k in ("deep", "deep_unreadable", "watch", "close"):
            if counts[k]:
                print(f"   {k:16} {counts[k]:6,}  "
                      f"{100.0*counts[k]/len(q):5.1f}%")
        print(f"\n{'route':16} {'docs':>4}  tender                 signals")
        print("-" * 78)
        for r in q[:args.limit]:
            sig = ",".join(s["signal"] for s in r["signals"]
                           if s["weight"] != "info")
            print(f"{r['route']:16} {r['documents']:4d}  "
                  f"{r['reference']:22} {sig[:34]}")
        return

    if not args.tender:
        ap.error("--tender or --all")
    res = screen(args.tender, notices)
    if args.json:
        print(json.dumps(res, indent=2))
        return
    if res.get("error"):
        print(res["error"])
        return
    print(f"\n{res['reference']}  ->  {res['route'].upper()}")
    print(f"  {res['title']}")
    print(f"  {res['entity']}")
    print(f"  {res['method']}  |  published {res['published']}  |  "
          f"window {res['window_days']} d  |  {res['documents']} document(s)\n")
    if not res["signals"]:
        print("  no cheap signal fires. Nothing here says spend the "
              "expensive readers on it.")
    for s in res["signals"]:
        print(f"  [{s['weight']:6}] {s['signal']}")
        print(f"           {s['why']}")
        print(f"           evidence: {s['evidence']}\n")


if __name__ == "__main__":
    main()
