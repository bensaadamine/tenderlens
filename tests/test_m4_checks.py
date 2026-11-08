"""
M4's checks (agents/m4_checks.py), with fake M2/M3 follow-ups - no text, no
award file, no network, no tokens. Checks what must hold: the checks run in
order and only when open, only code sets the route, a re-read that clears a
finding drops the route, a spelling that matches the brand raises it, and a
missing model never changes a case silently.

    python tests/test_m4_checks.py
"""

import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import repo  # noqa: E402  (agents/ tools/ prepare/ tests/ importable; works from the repo root)

import copy
import sys

import m4_case_builder as m4
import m4_checks

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


def scripted(*replies):
    """A fake model that answers from a list, then says finish; counts."""
    it = iter(replies)
    calls = []

    def ask(system, user):
        calls.append(user)
        r = next(it, {"action": "finish", "args": {}})
        if isinstance(r, Exception):
            raise r
        return r
    ask.calls = calls
    return ask


def hooks(letters=None, recheck_out=None):
    """Fake follow-ups. letters: brand -> letter the M2 agent gives."""
    log = []

    def recheck(cands):
        log.append(("recheck", [c["name"] for c in cands]))
        if recheck_out is not None:
            return recheck_out
        verdicts = []
        for i, c in enumerate(cands, 1):
            l = (letters or {}).get(c["name"])
            if l:
                verdicts.append({"id": i, "name": c["name"], "letter": l,
                                 "quote": c["evidence"], "document": DOC,
                                 "line": c["line_no"], "page": 1})
        return {"verdicts": verdicts, "steps": 1, "stop_reason": "done",
                "trace": ["fake"]}

    def spellings():
        log.append(("spellings",))
        return copy.deepcopy(M3_SPELT)

    def winner_awards(text, keys):
        log.append(("winner_awards", text, tuple(keys)))
        return {"count": 1, "lines": ["2025-01-01 | DND | Honda pumps"]}
    h = m4_checks.Hooks(recheck, spellings, winner_awards)
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

    # ---- when nothing runs
    h = hooks()
    case = run(m2(), h=h)
    check("no brand finding: no check runs",
          not h.log and case["m4_checks"]["note"] == "no brand finding")
    lettered = finding("Honda", 2, "The engine must be a Honda GX390.", "f")
    h = hooks()
    case = run(m2(lettered), dict(M3, award=None), h=h)
    check("finding already lettered by M2's agent, no winner: nothing runs",
          not h.log and case["m4_checks"]["note"] == "nothing to check")

    # ---- the order
    h = hooks({"Honda": "f", "Net": "a"})
    case = run(m2(HONDA, NET), h=h)
    check("every check, in order: re-read, spellings, winner's awards",
          [x[0] for x in h.log] == ["recheck", "spellings", "winner_awards"]
          and case["m4_checks"]["checks"] == 3)
    check("only the findings left after the re-read are searched",
          [x[1] for x in h.log if x[0] == "winner_awards"] == ["Honda"])

    # ---- re-read
    h = hooks({"Honda": "f", "Net": "a"})
    h.spellings = h.winner_awards = None
    case = run(m2(HONDA, NET), h=h)
    brands = [c.get("brand") for c in case["claims"]
              if c["kind"] == "brand_without_equivalent"]
    check("a re-read that clears 'Net' drops the route alert -> watch",
          case["route"] == "watch" and brands == ["Honda"])
    check("the kept finding now quotes the agent's verified line",
          case["claims"][0]["source"]["line"]
          == "The engine must be a Honda GX390."
          and case["claims"][0].get("agent_letter") == "f")
    check("the case page says what was re-read and why it was removed",
          "Checked again before ranking" in case["narrative"]
          and "'Net' was re-read by the spec agent and removed - not a brand"
          in case["narrative"])
    h = hooks({"Honda": "f"})
    h.spellings = h.winner_awards = None
    case = run(m2(HONDA, NET), h=h)
    check("a finding the agent leaves unlettered no longer counts",
          [c.get("brand") for c in case["claims"]] == ["Honda"]
          and any("could not be settled" in f["text"]
                  for f in case["followups"]))
    h = hooks({"Honda": "a"})
    case = run(m2(HONDA), h=h)
    check("every finding cleared: spellings and awards are not asked",
          [x[0] for x in h.log] == ["recheck"] and case["route"] == "close")

    # ---- spellings
    case = run(m2(HONDA), h=hooks({"Honda": "f"}))
    check("a spelling of the winner that matches the brand -> alert",
          case["route"] == "alert"
          and any(c["kind"] == "brand_matches_winner"
                  and "also written HONDA POWER" in c["claim"]
                  for c in case["claims"]))
    m4.BRAND_VS_SPELLINGS = False
    case = run(m2(HONDA), h=hooks({"Honda": "f"}))
    m4.BRAND_VS_SPELLINGS = True
    check("BRAND_VS_SPELLINGS = False restores the winner's name only",
          case["route"] == "watch")
    h = hooks()
    run(m2(lettered), M3_SPELT, h=h)
    check("spellings already checked by --m3-agent are not asked again",
          "spellings" not in [x[0] for x in h.log])

    # ---- winner's awards: information only
    h = hooks({"Honda": "f"})
    h.spellings = None
    case = run(m2(HONDA), h=h)
    base = m4.build_case(M1, m2(lettered), M3)
    check("winner's award titles are shown, never scored",
          case["score"] == base["score"] and case["route"] == base["route"]
          and "not scored" in case["narrative"])

    # ---- no model
    try:
        run(m2(HONDA, NET),
            h=hooks(recheck_out={"verdicts": [], "steps": 0,
                                 "stop_reason": "model unavailable: 429"}))
        raised = False
    except m4_checks.AgentUnavailable:
        raised = True
    check("M2's agent without a model stops the run", raised)
    m4_checks.AGENT_DOWN = "keep"
    case = run(m2(HONDA, NET),
               h=hooks(recheck_out={"verdicts": [], "steps": 0,
                                    "stop_reason": "model unavailable: 429"}))
    m4_checks.AGENT_DOWN = "stop"
    check("AGENT_DOWN = 'keep': the findings are kept as they were",
          case["route"] == before["route"])

    try:
        import m2_agent
        check("M4's letters are M2's letters",
              m4_checks.LETTERS == m2_agent.LETTERS)
    except Exception as e:
        print(f"skip m2_agent not importable ({e})")

    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
