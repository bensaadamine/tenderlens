"""
The M4 model experiment (experiments/m4_model_coordinator.py), tested with a
scripted fake model and fake M2/M3 follow-ups - no text, no award file, no network, no tokens. Checks what must
hold whatever the model says: only code sets the route, follow-ups run once
and only when open, a re-read that clears a finding drops the route, a
spelling that matches the brand raises it, a missing model never changes a
case silently, and the LangGraph graph and the plain loop agree.

    python -m tests.test_m4_model_coordinator
"""


import copy
import sys

from agents import m4_case_builder as m4
from agents import m4_checks
from experiments import m4_model_coordinator as mc

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


def act(name, **args):
    return {"thought": "", "action": name, "args": args}


def run(ask, m2_, m3_=M3, h=None, **kw):
    return mc.coordinate("t-1", M1, copy.deepcopy(m2_),
                               copy.deepcopy(m3_), h or hooks(), ask=ask,
                               use_graph=kw.pop("use_graph", False), **kw)


def main():
    before = m4.build_case(M1, m2(HONDA, NET), M3)
    check("the case starts as an alert (two rule findings)",
          before["route"] == "alert")
    check("build_case without follow-ups writes no 'Checked again' section",
          "Checked again" not in before["narrative"]
          and before["followups"] == [])

    # ---- gate: nothing sent to the model
    ask = scripted()
    case = run(ask, m2())
    check("no brand finding: the model is never called",
          not ask.calls and case["m4_checks"]["note"] == "no brand finding")
    ask = scripted()
    lettered = finding("Honda", 2, "The engine must be a Honda GX390.", "f")
    m3_nowin = dict(M3, award=None)
    case = run(ask, m2(lettered), m3_nowin)
    check("finding already lettered by M2's agent, no winner: no call",
          not ask.calls and case["m4_checks"]["note"] == "nothing to check")

    # ---- recheck
    h = hooks({"Honda": "f", "Net": "a"})
    ask = scripted(act("recheck", brands=["Honda", "Net"]), act("finish"))
    case = run(ask, m2(HONDA, NET), h=h)
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
    case = run(scripted(act("recheck", brands=["Honda", "Net"])),
               m2(HONDA, NET), h=h)
    check("a finding the agent leaves unlettered no longer counts",
          [c.get("brand") for c in case["claims"]] == ["Honda"]
          and any("could not be settled" in f["text"]
                  for f in case["followups"]))

    h = hooks({"Honda": "f"})
    case = run(scripted(act("recheck", brands=["Ford"]),
                        act("recheck", brands=["Honda"]),
                        act("recheck", brands=["Honda"])), m2(HONDA), h=h)
    check("an unknown brand is refused; a brand is re-read once",
          [x for x in h.log if x[0] == "recheck"] == [("recheck", ["Honda"])]
          and any("'ford': refused" in t for t in case["trace"]["M4"]))

    # ---- spellings
    h = hooks({"Honda": "f"})
    case = run(scripted(act("recheck", brands=["Honda"]), act("spellings")),
               m2(HONDA), h=h)
    check("a spelling of the winner that matches the brand -> alert",
          case["route"] == "alert"
          and any(c["kind"] == "brand_matches_winner"
                  and "also written HONDA POWER" in c["claim"]
                  for c in case["claims"]))
    m4.BRAND_VS_SPELLINGS = False
    case = run(scripted(act("recheck", brands=["Honda"]), act("spellings")),
               m2(HONDA), h=hooks({"Honda": "f"}))
    m4.BRAND_VS_SPELLINGS = True
    check("BRAND_VS_SPELLINGS = False restores the winner's name only",
          case["route"] == "watch")

    h = hooks({"Honda": "f"})
    case = run(scripted(act("spellings"), act("spellings")), m2(HONDA), h=h)
    check("spellings run once",
          [x for x in h.log if x[0] == "spellings"] == [("spellings",)])

    ask = scripted()
    case = run(ask, m2(HONDA), M3_SPELT, h=hooks())
    check("spellings already checked by --m3-agent are not offered",
          "spellings" not in ask.calls[0].split("Available now:")[1])

    # ---- winner_awards: information only
    h = hooks({"Honda": "f"})
    case = run(scripted(act("winner_awards", text="Honda")), m2(HONDA), h=h)
    base = m4.build_case(M1, m2(HONDA), M3)
    check("winner's award titles are shown, never scored",
          case["score"] == base["score"] and case["route"] == base["route"]
          and "not scored" in case["narrative"])

    # ---- the loop
    h = hooks({"Honda": "f", "Net": "a"})
    ask = scripted(act("recheck", brands=["Honda", "Net"]), act("spellings"),
                   act("winner_awards", text="Honda"))
    case = run(ask, m2(HONDA, NET), h=h)
    check("it stops by itself when no follow-up is left",
          len(ask.calls) == 3
          and case["m4_checks"]["note"].endswith("nothing left to check"))

    ask = scripted(*[{"no": "json"}] * 10)
    case = run(ask, m2(HONDA, NET))
    check("the step budget stops a model that never acts",
          len(ask.calls) == mc.MAX_STEPS
          and case["route"] == before["route"])

    case = run(scripted(act("finish", route="close", score=0)), m2(HONDA, NET))
    check("the model cannot set the route",
          case["route"] == "alert" and case["score"] == before["score"])

    case = run(scripted({"name": "recheck", "arguments": {"brands": ["Net"]}}),
               m2(HONDA, NET), h=hooks({"Net": "a"}))
    check("a native tool-call reply is read as an action",
          [c.get("brand") for c in case["claims"]
           if c["kind"] == "brand_without_equivalent"] == ["Honda"])

    # ---- no model
    try:
        run(scripted(RuntimeError("groq: HTTP 429")), m2(HONDA, NET))
        raised = False
    except m4_checks.AgentUnavailable:
        raised = True
    check("no model: the run stops (AgentUnavailable)", raised)
    m4_checks.AGENT_DOWN = "keep"
    case = run(scripted(RuntimeError("groq: HTTP 429")), m2(HONDA, NET))
    m4_checks.AGENT_DOWN = "stop"
    check("AGENT_DOWN = 'keep': the case is ranked as it stands",
          case["route"] == before["route"]
          and case["score"] == before["score"])
    try:
        run(scripted(act("recheck", brands=["Net"])), m2(HONDA, NET),
            h=hooks(recheck_out={"verdicts": [], "steps": 0,
                                 "stop_reason": "model unavailable: 429"}))
        raised = False
    except m4_checks.AgentUnavailable:
        raised = True
    check("M2's agent without a model stops the run too", raised)

    # ---- graph = loop
    if mc.agent_loop.StateGraph is None:
        print("skip langgraph not installed - graph vs loop not compared")
    else:
        out = []
        for g in (True, False):
            c = run(scripted(act("recheck", brands=["Honda", "Net"]),
                             act("spellings")), m2(HONDA, NET),
                    h=hooks({"Honda": "f", "Net": "a"}), use_graph=g)
            out.append((c["route"], c["score"],
                        [f["text"] for f in c["followups"]]))
        check("LangGraph and the plain loop give the same case",
              out[0] == out[1])

    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
