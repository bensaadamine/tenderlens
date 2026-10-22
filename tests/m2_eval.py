"""
The M2 exam: run level 1 on every case already judged by hand
(tests/m2_labels.csv) and compare the letter it gives with the auditor's.

    python tests/m2_eval.py                    # rules only (the baseline)
    python tests/m2_eval.py --agent --online   # with the M2 agent (Windows)
    python tests/m2_eval.py --agent --online --only cb-425-92729935 --only cb-152-1757339

Letters are the auditor's grid (README): a not a brand, b bidder speaking,
c buyer's equipment, d list of brands, e equivalent accepted, f required
with no equivalent. What counts most is the line "finding right": a case is
right when it is a finding exactly when the auditor said "f".

With --agent, a tender counts only if the agent really judged it: when no
model answers, M2 keeps the rules' result, and scoring that as the agent's
would measure the rules again (it did, on 2026-10-22). Such tenders are
listed apart, and after STOP_AFTER of them in a row the run stops - the
providers are down or out of quota (see reports/llm_errors.csv).

Writes reports/m2_eval/<rules|agent>.csv, one row per labelled case, and
reports/m2_eval/agent_traces.txt, every step the agent took.
"""

# --- repo layout: find the agents/, tools/, prepare/ folders and run from
# the repository root, whatever folder the script sits in (2026-10-22).
import os as _os
import sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("agents", "tools", "prepare", "tests", ""):
    _p = _os.path.join(_ROOT, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
if __name__ == "__main__":
    _os.chdir(_ROOT)
# ---

import argparse
import collections
import csv
import io
import os

import m2_spec_auditor
import spec_tools

LABELS = os.path.join("tests", "m2_labels.csv")
OUT_DIR = os.path.join("reports", "m2_eval")
STOP_AFTER = 3      # tenders in a row where the agent could not run

# What a rule clearance means in the auditor's grid.
STATUS_LETTER = {
    "OK": "e", "OK_EQUIV_PRODUCTS_CLAUSE": "e", "OK_BLANKET_CLAUSE": "e",
    "OK_PDF_ORDER": "e", "OK_BUYER_EQUIPMENT": "c", "OK_MAKE_LIST": "d",
    "OK_LLM_CLEARED": "cleared by the LLM judge",
}


def letter_for(l1, name):
    """The letter level 1 gave this name, or why it gave none."""
    key = name.lower()

    def same(row):
        b = (row.get("brand") or "").lower()
        return key in b or b in key

    for r in l1.get("findings", []):
        if same(r):
            return "f"
    for r in l1.get("ok", []):
        if same(r):
            if r.get("agent_letter"):
                return r["agent_letter"]
            return STATUS_LETTER.get(r.get("status"), r.get("status"))
    for r in l1.get("unresolved", []):
        if same(r):
            return "unresolved"
    return "not a candidate"


def agent_status(l1, trace):
    """Did the agent really judge this tender?"""
    if l1.get("agent"):
        stop = l1["agent"].get("stop_reason", "")
        return "incomplete: " + stop if stop.startswith("model") else "ran"
    for t in trace:
        if t.startswith("agent could not start"):
            return "did not run: " + t.split("(", 1)[-1].split(")")[0][:80]
        if t.startswith("agent: nothing"):
            return "ran (nothing to check)"
        if t.startswith("--agent asked but no LLM key"):
            return "did not run: no LLM key"
    return "ran (rules cleared everything)"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--only", action="append", default=[], metavar="REF",
                    help="only this tender (repeatable)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N tenders")
    spec_tools.add_l1_args(ap)
    a = ap.parse_args()
    spec_tools.apply_l1_args(a)

    with io.open(LABELS, encoding="utf-8", newline="") as f:
        labels = list(csv.DictReader(f))
    by_ref = collections.OrderedDict()
    for lab in labels:
        by_ref.setdefault(lab["reference"], []).append(lab)

    if a.only:
        by_ref = collections.OrderedDict(
            (r, by_ref[r]) for r in a.only if r in by_ref)
    if a.limit:
        by_ref = collections.OrderedDict(list(by_ref.items())[:a.limit])

    os.makedirs(OUT_DIR, exist_ok=True)
    traces = io.open(os.path.join(OUT_DIR, "agent_traces.txt"), "w",
                     encoding="utf-8") if a.agent else None
    rows, down_in_a_row = [], 0
    for ref, labs in by_ref.items():
        trace = []
        l1 = m2_spec_auditor.run_level1(ref, online=a.online, agent=a.agent,
                                        trace=trace)
        ran = agent_status(l1, trace) if a.agent else "-"
        for lab in labs:
            got = letter_for(l1, lab["name"])
            want = lab["letter"]
            rows.append({
                "reference": ref, "name": lab["name"], "expected": want,
                "got": got, "letter_right": got == want,
                "finding_right": (got == "f") == (want == "f"),
                "agent": ran,
                "agent_steps": (l1.get("agent") or {}).get("steps", "")})
        print(f"  {ref:28} [{ran}] " + ", ".join(
            f"{r['name']}: {r['got']} (want {r['expected']})"
            for r in rows[-len(labs):]))
        if traces:
            traces.write(f"=== {ref}\n" + "\n".join(trace) + "\n\n")
            traces.flush()
        down_in_a_row = down_in_a_row + 1 if ran.startswith("did not run") \
            else 0
        if down_in_a_row >= STOP_AFTER:
            print(f"\nStopped: the agent could not run on {STOP_AFTER} "
                  f"tenders in a row. See reports/llm_errors.csv.")
            break
    if traces:
        traces.close()

    out = os.path.join(OUT_DIR, ("agent" if a.agent else "rules") + ".csv")
    with io.open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    skipped = [r for r in rows if r["agent"].startswith("did not run")]
    rows = [r for r in rows if not r["agent"].startswith("did not run")]
    if skipped:
        print(f"\n{len(skipped)} case(s) left out - the agent did not run "
              f"on their tender: " + "; ".join(sorted({r["agent"]
                                                       for r in skipped})))
    if not rows:
        print("Nothing to score.")
        return
    n = len(rows)
    f_want = [r for r in rows if r["expected"] == "f"]
    f_got = [r for r in rows if r["got"] == "f"]
    print(f"\n{'AGENT' if a.agent else 'RULES ONLY'}"
          f"{' + online' if a.online else ''} - {n} labelled cases")
    print(f"  finding right (finding exactly when the auditor said f): "
          f"{sum(r['finding_right'] for r in rows)}/{n}")
    print(f"  real lock-ins found:   {sum(r['got'] == 'f' for r in f_want)}"
          f"/{len(f_want)}")
    print(f"  false findings:        "
          f"{sum(r['expected'] != 'f' for r in f_got)}/{len(f_got)} findings")
    print(f"  exact letter:          {sum(r['letter_right'] for r in rows)}/{n}")
    print("  what each was given:")
    for k, c in collections.Counter(r["got"] for r in rows).most_common():
        print(f"    {c:3d}  {k}")
    print(f"\n-> {out}")


if __name__ == "__main__":
    main()
