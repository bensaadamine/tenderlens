"""
Does the M4 agent earn its cost? Runs the labelled tenders (tests/m2_labels.csv
and tests/m2_holdout.csv) three ways and compares the routes:

    off     M4 rules only
    fixed   every M4 follow-up, in a fixed order, no model choosing
    model   the M4 agent chooses the follow-ups

M2 runs on rules only, so M4's follow-ups (re-reading a finding, the
winner's spellings) are what changes a case. A tender counts as "should be
flagged" when one of its names is labelled f (brand required, no
equivalent); "flagged" means watch or alert. If `model` gives the same
routes as `fixed`, keep `fixed`: the model's choices add cost, not value.

    python tenderlens.py eval m4 --online                 # all three (Windows)
    python tenderlens.py eval m4 --modes off              # offline baseline

Writes reports/m4_eval/<stamp>/cases.csv and summary.txt; never overwrites.
"""


import argparse
import collections
import csv
import io
import os
import time

from tools import llm
from tests import m2_eval
from pipeline import one_tender

OUT_DIR = os.path.join("reports", "m4_eval")
MODES = ("off", "fixed", "model")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    labels = m2_eval.load_labels("all")
    want = collections.defaultdict(set)
    for lab in labels:
        want[lab["reference"]].add(lab["letter"])
    refs = sorted(want)[:a.limit or None]
    shared = one_tender.load_shared(verbose=False)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(OUT_DIR, stamp)
    os.makedirs(out_dir, exist_ok=True)

    rows, tokens = [], {}
    for mode in a.modes:
        start = len(llm.usage_rows())
        for ref in refs:
            try:
                c = one_tender.run_one(ref, shared, online=a.online,
                                       m4_mode=None if mode == "off"
                                       else mode)
                route = c["route"]
                steps = (c.get("m4_checks") or {}).get("checks", 0)
                checks = "; ".join(f["text"][:80]
                                   for f in c.get("followups", []))
            except one_tender.AGENT_DOWN as e:
                route, steps, checks = "agent did not run", 0, str(e)[:120]
            rows.append({"mode": mode, "reference": ref,
                         "labelled_lock_in": "f" in want[ref],
                         "route": route, "m4_checks": steps,
                         "followups": checks})
            print(f"  {mode:5} {ref:28} {route:6} (lock-in labelled: "
                  f"{'f' in want[ref]})")
        tokens[mode] = llm.agent_tokens(llm.usage_rows()[start:])

    with io.open(os.path.join(out_dir, "cases.csv"), "w", encoding="utf-8",
                 newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    lines = [f"M4 eval - {stamp} - {len(refs)} labelled tender(s), code "
             f"{m2_eval._commit()}", ""]
    by = {m: {r["reference"]: r for r in rows if r["mode"] == m}
          for m in a.modes}
    for m in a.modes:
        rs = list(by[m].values())
        ran = [r for r in rs if r["route"] != "agent did not run"]
        flag = [r for r in ran if r["route"] in ("watch", "alert")]
        hit = sum(r["labelled_lock_in"] for r in flag)
        should = sum(r["labelled_lock_in"] for r in ran)
        lines.append(f"{m:6} flagged {len(flag)}/{len(ran)}; of tenders with "
                     f"a labelled lock-in {hit}/{should} flagged; flagged "
                     f"without one {len(flag) - hit}; agent tokens "
                     f"{tokens.get(m, 0):,}; agent did not run on "
                     f"{len(rs) - len(ran)}")
    if "fixed" in by and "model" in by:
        diff = [ref for ref in refs
                if by["fixed"][ref]["route"] != by["model"][ref]["route"]]
        lines += ["", f"model vs fixed: {len(refs) - len(diff)}/{len(refs)} "
                      f"same route" + (f"; differ on {', '.join(diff)}"
                                       if diff else "")]
    text = "\n".join(lines)
    with io.open(os.path.join(out_dir, "summary.txt"), "w",
                 encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + text + f"\n\n-> {out_dir}")


if __name__ == "__main__":
    main()
