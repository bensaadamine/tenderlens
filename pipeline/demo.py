"""
THE DEMO - the system on seven chosen tenders, three ways, side by side.

    python tenderlens.py demo             # Windows: online; the first run fills the caches
    python tenderlens.py demo --offline   # anywhere: replays from the caches, no network

Each tender is here for one reason (TENDERS below). The three setups:

    rules     M1-M4 on rules only (brand names checked on Wikidata)
    recheck   the same, then M4 hands every brand finding back to the M2
              agent before ranking - the cheap setup: the model runs only
              on cases that are already flagged
    agents    every agent: M2 letters every open brand name, M3 decides
              which spellings are the winner's company, M4 re-checks
              what is left - the thorough setup

Writes reports/demo/<stamp>/: summary.txt (the table below) and, per setup,
<setup>_queue.html / .csv / .json - the case pages an auditor would open.
"""


import argparse
import io
import os
import time

from tools import level2
from tools import llm
from tools import report
from pipeline import one_tender

TENDERS = [
    ("cb-484-58375289", "real lock-in: SUSS Microtec required, no equivalent"),
    ("cb-403-14199409", "real lock-in: 'a Formlabs Form 4L' 3D printer"),
    ("cb-251-8268519", "real lock-in the rules miss: a Gamry Interface "
                       "5000E required"),
    ("cb-708-9918231", "advance award notice naming the Zurich "
                       "Instruments amplifier its winner makes"),
    ("cb-152-1757339", "false alarm: Hitachi and JEOL are a list of makes"),
    ("cb-952-81230837", "Johnson Controls is the buyer's own system; "
                        "an ABB drive model is required"),
    ("cb-147-94499423", "ordinary tender: a shuttle bus, nothing to find"),
]

SETUPS = {
    "rules":   dict(agent=False, m3_agent=False, m4_mode=None),
    "recheck": dict(agent=False, m3_agent=False, m4_mode="fixed"),
    "agents":  dict(agent=True, m3_agent=True, m4_mode="fixed"),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--offline", action="store_true",
                    help="no network: Wikidata and model answers from the "
                         "caches only")
    ap.add_argument("--no-level2", action="store_true",
                    help="skip level 2 (dormant: numbers against peers)")
    ap.add_argument("--setups", nargs="+", choices=list(SETUPS),
                    default=list(SETUPS))
    a = ap.parse_args()
    online = not a.offline
    level2.ON[0] = not a.no_level2

    shared = one_tender.load_shared(verbose=False)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join("reports", "demo", stamp)
    os.makedirs(out_dir, exist_ok=True)

    routes, tokens = {}, {}
    for name in a.setups:
        start = len(llm.usage_rows())
        cases = []
        for ref, _why in TENDERS:
            try:
                c = one_tender.run_one(ref, shared, online=online,
                                       **SETUPS[name])
                routes[name, ref] = f"{c['route']} {c['score']}"
                cases.append(c)
            except one_tender.AGENT_DOWN as e:
                routes[name, ref] = ("not in cache" if a.offline
                                     else "no model")
                print(f"  {name} {ref}: {str(e)[:120]}")
            print(f"  {name:8} {ref:18} {routes[name, ref]}")
        tokens[name] = llm.agent_tokens(llm.usage_rows()[start:])
        if cases:
            report.write_outputs(cases, {
                "scope": f"demo, setup '{name}'"
                         f"{' (replayed offline)' if a.offline else ''}",
                "generated": time.strftime("%Y-%m-%d %H:%M"),
                "awards": bool(shared[3])},
                os.path.join(out_dir, f"{name}_queue"))

    w = max(len(why) for _, why in TENDERS)
    lines = [f"TenderLens demo - {stamp}"
             f"{' - replayed offline' if a.offline else ''}", "",
             f"{'tender':18} {'why it is here':{w}}  "
             + "".join(f"{n:14}" for n in a.setups)]
    for ref, why in TENDERS:
        lines.append(f"{ref:18} {why:{w}}  "
                     + "".join(f"{routes[n, ref]:14}" for n in a.setups))
    lines += ["", f"{'model tokens paid':{19 + w}}  "
              + "".join(f"{tokens[n]:<14,}" for n in a.setups),
              "", "(route and score; tokens are 0 when every answer came "
              "from the cache)"]
    text = "\n".join(lines)
    with io.open(os.path.join(out_dir, "summary.txt"), "w",
                 encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + text + f"\n\n-> {out_dir}")


if __name__ == "__main__":
    main()
