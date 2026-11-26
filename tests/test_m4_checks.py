"""
M4's checks (agents/m4_checks.py), with fake M2/M3 follow-ups - no text, no
award file, no network, no tokens. Checks what must hold: the checks run in
order and only when there is a brand finding and a winner, only code sets
the route, a spelling that matches the brand raises it, and the winner's
other awards are shown but never scored.

    python -m tests.test_m4_checks
"""


import copy
import sys

from agents import m4_case_builder as m4
from agents import m4_checks

FAILS = 0
DOC = "01_rfp.pdf"
M1 = {"reference": "t-1", "route": "standard", "signals": []}


def finding(brand, line, text, letter=None):
    return {"level": 1, "kind": "brand_without_equivalent", "brand": brand,
            "confidence": "medium", "claim": f"Names '{brand}'",
            "source": {"file": DOC, "where": f"page 1, line {line}",
                       "line": text, "context": None, "checked": True},
            "line_no": line, "agent_letter": letter}


def m2(*findings):
    return {"reference": "t-1", "readable": True,
            "level1": {"findings": len(findings), "ok": 0, "unresolved": 0},
            "findings": list(findings), "trace": []}


HONDA = finding("Honda", 2, "The engine must be a Honda GX390.")
NET = finding("Net", 3, "Payment terms: Net 30 days.")
M3 = {"reference": "t-1", "available": True, "agent": False, "findings": [],
      "trace": [], "award": {"supplier": "ACME POWER INC",
                             "supplier_key": "ACME POWER",
                             "other_spellings": [], "spelling_keys": []}}
M3_SPELT = copy.deepcopy(M3)
M3_SPELT["agent"] = True
M3_SPELT["award"].update(other_spellings=["HONDA POWER EQUIPMENT CANADA"],
                         spelling_keys=["HONDA POWER EQUIPMENT"])


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def hooks():
    """Fake follow-ups, logged in order."""
    log = []

    def spellings():
        log.append(("spellings",))
        return copy.deepcopy(M3_SPELT)

    def winner_awards(text, keys):
        log.append(("winner_awards", text, tuple(keys)))
        return {"count": 1, "lines": ["2025-01-01 | DND | Honda pumps"]}
    h = m4_checks.Hooks(spellings, winner_awards)
    h.log = log
    return h


def run(m2_, m3_=M3, h=None):
    return m4_checks.check_case("t-1", M1, copy.deepcopy(m2_),
                                copy.deepcopy(m3_), h or hooks())


def main():
    before = m4.build_case(M1, m2(HONDA, NET), M3)
    check("the case starts as an alert (two rule findings)",
          before["route"] == "alert")
    check("build_case without checks writes no 'Checked again' section",
          "Checked again" not in before["narrative"]
          and before["followups"] == [])
    lettered = finding("Honda", 2, "The engine must be a Honda GX390.", "f")

    # ---- when nothing runs
    h = hooks()
    case = run(m2(), h=h)
    check("no brand finding: no check runs",
          not h.log and case["m4_checks"]["note"] == "no brand finding")
    h = hooks()
    case = run(m2(lettered), dict(M3, award=None), h=h)
    check("a brand finding but no winner: nothing runs",
          not h.log and case["m4_checks"]["note"] == "nothing to check")

    # ---- the order
    h = hooks()
    case = run(m2(lettered), h=h)
    check("the checks, in order: spellings, then the winner's awards",
          [x[0] for x in h.log] == ["spellings", "winner_awards"]
          and case["m4_checks"]["checks"] == 2)
    check("a finding is never sent back to the M2 agent (no re-read)",
          all(x[0] != "recheck" for x in h.log)
          and not hasattr(m4_checks, "_recheck"))

    # ---- spellings
    case = run(m2(lettered), h=hooks())
    check("a spelling of the winner that matches the brand -> alert",
          case["route"] == "alert"
          and any(c["kind"] == "brand_matches_winner"
                  and "also written HONDA POWER" in c["claim"]
                  for c in case["claims"]))
    h = hooks()
    run(m2(lettered), M3_SPELT, h=h)
    check("spellings already checked by --m3-agent are not asked again",
          "spellings" not in [x[0] for x in h.log])

    # ---- winner's awards: information only
    h = hooks()
    h.spellings = None
    case = run(m2(lettered), h=h)
    base = m4.build_case(M1, m2(lettered), M3)
    check("winner's award titles are shown, never scored",
          case["score"] == base["score"] and case["route"] == base["route"]
          and "not scored" in case["narrative"])
    check("every brand finding is searched in the winner's awards",
          [x[1] for x in h.log if x[0] == "winner_awards"] == ["Honda"])

    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
