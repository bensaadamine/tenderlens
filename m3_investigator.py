"""
M3 INVESTIGATOR.

Builds the buyer -> supplier graph from CanadaBuys AWARD notices and answers,
for ONE tender: who won it, and is that a pattern?

    from m3_investigator import investigate
    investigate("cb-258-72766150", notices, graph)

Get the data once, ON WINDOWS (canadabuys.canada.ca is blocked from the
agent's container and from the device shell - CLAUDE.md gotchas):

    python m3_investigator.py --fetch          # -> data/awards.csv
    python m3_investigator.py --columns        # check what was detected

---------------------------------------------------------------- the stance

DECISION 4: market concentration is CONTEXT, NOT EVIDENCE. A department that
buys specialised trucks from the one dealer who sells them is not rigging
anything. So every M3 result is labelled weight="context", and M4 lets it
raise a case only alongside a finding from the documents - never alone.
What M3 adds is the second half of the classic pattern: a specification
narrowed to one product (M2) AND a buyer who keeps awarding to the same
supplier (M3) is a much stronger story than either half.

------------------------------------------------------------ the decisions

DEPTH 1, always: find this tender's award, its supplier, and what share of
this buyer's awards in this category went to that supplier.

DEPTH 2, only if depth 1 shows a recurring winner: look at the SUPPLIER's
other buyers. A supplier who wins everywhere is a market leader; a supplier
who lives off one buyer is a relationship. Same share at depth 1, opposite
meaning - which is why the agent goes the extra step only when it matters,
and records that it did.

-------------------------------------------------------------- the columns

Detected from the header, never assumed. The award file has not been seen
by the person who wrote this module; guessing its column names would be the
same mistake as guessing a unit (decision 15). If detection fails, the error
lists every header so the mapping can be fixed in one line.
"""

import argparse
import collections
import csv
import io
import json
import os
import re
import sys
import urllib.request

csv.field_size_limit(10_000_000)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AWARDS = "data/awards.csv"
AWARDS_URL = ("https://canadabuys.canada.ca/opendata/pub/"
              "awardNoticeComplete-avisAttributionComplet.csv")

C_SOL_TENDER = "solicitationNumber-numeroSollicitation"
C_UNSPSC_TENDER = "unspsc"

REPEAT_SHARE = 0.5   # this buyer gave at least half its awards in the
REPEAT_MIN = 3       # category to one supplier, over at least 3 awards

# Legal-form suffixes stripped so "ACME TRUCKS INC." and "Acme Trucks Inc"
# count as one supplier. Nothing smarter than that: merging two different
# companies would invent exactly the concentration this module reports.
RE_SUFFIX = re.compile(
    r"\b(inc|incorporated|ltd|ltee|ltée|limited|corp|corporation|co|company"
    r"|llc|lp|ulc|srl|sa|gmbh)\b\.?", re.I)


def norm_name(s):
    s = (s or "").upper()
    s = RE_SUFFIX.sub(" ", s)
    s = re.sub(r"[^A-Z0-9À-Ÿ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_sol(s):
    return re.sub(r"\s+", "", (s or "")).upper()


def first_unspsc4(raw):
    for t in re.split(r"[\r\n,;|\s]+", raw or ""):
        d = re.sub(r"\D", "", t)
        if len(d) >= 4:
            return d[:4]
    return ""


# ------------------------------------------------------------------ fetch
def fetch(path=AWARDS, url=AWARDS_URL):
    """Download the award notices once. Resumable in the only sense that
    matters: if the file is there, nothing is downloaded."""
    if os.path.exists(path):
        print(f"{path} already present - not re-downloading")
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".part"
    print(f"downloading {url}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "TenderLens/0.1 (academic research)"})
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
    os.replace(tmp, path)
    print(f"-> {path} ({os.path.getsize(path)/1e6:.0f} MB)")


# -------------------------------------------------------- column detection
def detect_columns(header):
    """Map the fields M3 needs onto whatever the award file calls them."""
    eng = [h for h in header if not re.search(r"-fra\b|fra$", h, re.I)]
    low = {h: h.lower() for h in eng}

    def pick(*tests):
        for test in tests:
            for h in eng:
                if test(low[h]):
                    return h
        return None

    cols = {
        "ref": pick(lambda s: "referencenumber" in s),
        "sol": pick(lambda s: "solicitationnumber" in s),
        "supplier": pick(lambda s: "supplier" in s and "standard" in s
                         and "name" in s,
                         lambda s: "supplier" in s and "legal" in s
                         and "name" in s,
                         lambda s: "supplier" in s and "name" in s),
        "buyer": pick(lambda s: "contractingentityname" in s,
                      lambda s: "entity" in s and "name" in s),
        "value": pick(lambda s: "totalcontractvalue" in s,
                      lambda s: "contractamount" in s,
                      lambda s: "contract" in s and "value" in s),
        "date": pick(lambda s: "contractawarddate" in s,
                     lambda s: "awarddate" in s),
        "unspsc": pick(lambda s: s.strip('"﻿') == "unspsc"),
    }
    missing = [k for k in ("supplier", "buyer") if not cols[k]]
    if not cols["sol"] and not cols["ref"]:
        missing.append("sol/ref")
    if missing:
        raise SystemExit(
            "award file: could not detect " + ", ".join(missing) +
            ".\nHeaders seen:\n  " + "\n  ".join(header) +
            "\nFix the matching test in detect_columns().")
    return cols


# ------------------------------------------------------------------ graph
def load_graph(path=AWARDS):
    """Index the awards. Returns None if the file is absent - M3 then
    reports itself unavailable instead of failing the whole pipeline."""
    if not os.path.exists(path):
        return None
    with io.open(path, encoding="utf-8-sig", errors="replace",
                 newline="") as f:
        rd = csv.DictReader(f)
        cols = detect_columns(rd.fieldnames or [])
        by_sol = collections.defaultdict(list)
        by_ref = collections.defaultdict(list)
        # one count per (contract, supplier): an award notice re-published
        # with an amendment is not a second award
        seen = set()
        bg = collections.Counter()          # (buyer, grp) -> awards
        bsg = collections.Counter()         # (buyer, supplier, grp)
        sb = collections.defaultdict(collections.Counter)  # supplier->buyers
        bgs = collections.defaultdict(set)  # (buyer, grp) -> suppliers
        names = {}
        n = 0
        for r in rd:
            n += 1
            sup_raw = (r.get(cols["supplier"]) or "").strip()
            buy_raw = (r.get(cols["buyer"]) or "").strip()
            if not sup_raw or not buy_raw:
                continue
            sup, buy = norm_name(sup_raw), norm_name(buy_raw)
            names.setdefault(sup, sup_raw)
            sol = norm_sol(r.get(cols["sol"])) if cols["sol"] else ""
            ref = (r.get(cols["ref"]) or "").strip() if cols["ref"] else ""
            grp = first_unspsc4(r.get(cols["unspsc"])) if cols["unspsc"] else ""
            rec = {"supplier": sup, "supplier_name": sup_raw, "buyer": buy,
                   "buyer_name": buy_raw, "group": grp,
                   "value": (r.get(cols["value"]) or "").strip()
                   if cols["value"] else "",
                   "date": (r.get(cols["date"]) or "")[:10]
                   if cols["date"] else "",
                   "sol": sol, "ref": ref}
            if sol:
                by_sol[sol].append(rec)
            if ref:
                by_ref[ref].append(rec)
            key = (sol or ref, sup)
            if key in seen:
                continue
            seen.add(key)
            bg[(buy, grp)] += 1
            bsg[(buy, sup, grp)] += 1
            sb[sup][buy] += 1
            bgs[(buy, grp)].add(sup)
    return {"cols": cols, "rows": n, "by_sol": by_sol, "by_ref": by_ref,
            "bg": bg, "bsg": bsg, "sb": sb, "bgs": bgs, "names": names}


def investigate(reference, notice_row=None, graph=None):
    """M3 for ONE tender. Every result is weight='context' (decision 4)."""
    trace = []
    if graph is None:
        trace.append("no award data (data/awards.csv) - run "
                     "`python m3_investigator.py --fetch` on Windows")
        return {"reference": reference, "available": False,
                "award": None, "findings": [], "trace": trace}

    sol = norm_sol((notice_row or {}).get(C_SOL_TENDER))
    recs = graph["by_sol"].get(sol, []) if sol else []
    via = "solicitation number"
    if not recs:
        recs = graph["by_ref"].get(reference, [])
        via = "reference number"
    if not recs:
        trace.append(f"no award notice found for this tender (tried "
                     f"solicitation {sol or '-'} and reference)")
        return {"reference": reference, "available": True, "award": None,
                "findings": [], "trace": trace}

    a = recs[0]
    grp = a["group"] or first_unspsc4((notice_row or {}).get(C_UNSPSC_TENDER))
    trace.append(f"award found via {via}: {a['supplier_name']} "
                 f"({len(recs)} award row(s))")

    total = graph["bg"].get((a["buyer"], grp), 0)
    mine = graph["bsg"].get((a["buyer"], a["supplier"], grp), 0)
    suppliers = len(graph["bgs"].get((a["buyer"], grp), ()))
    share = mine / float(total) if total else 0.0
    trace.append(f"depth 1: buyer awarded {total} contract(s) in group "
                 f"{grp or '?'}, {mine} to this supplier ({share:.0%}), "
                 f"{suppliers} distinct supplier(s)")

    award = {"supplier": a["supplier_name"], "buyer": a["buyer_name"],
             "value": a["value"], "date": a["date"], "group": grp,
             "buyer_awards_in_group": total, "to_this_supplier": mine,
             "share": round(share, 3), "distinct_suppliers": suppliers}
    findings = []

    if total >= REPEAT_MIN and share >= REPEAT_SHARE:
        # Depth 2 - only now is it worth asking who ELSE this supplier
        # sells to. Same share at depth 1 can mean a market leader or a
        # dependency; the answer is in the supplier's other buyers.
        buyers = graph["sb"].get(a["supplier"], {})
        tot_s = sum(buyers.values())
        dep = buyers.get(a["buyer"], 0) / float(tot_s) if tot_s else 0.0
        trace.append(f"depth 2: supplier has {tot_s} award(s) across "
                     f"{len(buyers)} buyer(s); {dep:.0%} from this buyer")
        award.update({"supplier_awards": tot_s,
                      "supplier_buyers": len(buyers),
                      "supplier_dependency": round(dep, 3)})
        kind = ("dependent_supplier" if dep >= REPEAT_SHARE
                else "recurring_supplier")
        findings.append({
            "level": 3, "kind": kind, "weight": "context",
            "confidence": "medium",
            "claim": (f"{a['buyer_name']} awarded {mine} of {total} "
                      f"contracts in this category to {a['supplier_name']}"
                      + (f", which draws {dep:.0%} of its awards from this "
                         f"buyer" if kind == "dependent_supplier" else "")),
            "source": {"file": "award notices (CanadaBuys)",
                       "where": f"{mine} of {total} awards, group {grp}",
                       "line": f"{a['supplier_name']} <- {a['buyer_name']}",
                       "context": None},
        })
    else:
        trace.append("depth 2 skipped - no recurring winner at depth 1")

    return {"reference": reference, "available": True, "award": award,
            "findings": findings, "trace": trace}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="download the award notices (run on Windows)")
    ap.add_argument("--columns", action="store_true",
                    help="show which columns were detected")
    ap.add_argument("--tender")
    ap.add_argument("--awards", default=AWARDS)
    a = ap.parse_args()
    if a.fetch:
        fetch(a.awards)
        return
    g = load_graph(a.awards)
    if g is None:
        sys.exit(f"missing {a.awards} - run with --fetch first")
    if a.columns:
        print(f"{g['rows']:,} award rows")
        for k, v in g["cols"].items():
            print(f"  {k:9} <- {v}")
        return
    if a.tender:
        import m1_screener
        notices = m1_screener.load_notices()
        print(json.dumps(investigate(a.tender, notices.get(a.tender), g),
                         indent=2, default=str))


if __name__ == "__main__":
    main()
