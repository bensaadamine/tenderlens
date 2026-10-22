"""
The M2 agent's rules, tested with a scripted fake model - no network, no
tokens. Checks what must hold whatever the model says: quotes are verified
against the text, an "f" must quote the brand, the step budget stops the
loop, a bad or missing model never produces a finding, and the LangGraph
graph and the plain loop give the same result.

    python tests/test_m2_agent.py
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

import os
import sys
import tempfile

import llm
import m2_agent

FAILS = 0
DOC = "01_rfp.pdf"
TEXT = "\n".join([
    "ANNEX A - REQUIREMENTS",                                    # 1
    "The engine must be a Honda GX390.",                         # 2
    "Payment terms: Net 30 days.",                               # 3
    "",                                                          # 4
    "Question 1: We propose a Kohler CH440, is it acceptable?",  # 5
    "Answer 1: No.",                                             # 6
])
CANDS = [
    {"name": "Honda", "document": DOC, "line_no": 2,
     "evidence": "The engine must be a Honda GX390."},
    {"name": "Net", "document": DOC, "line_no": 3,
     "evidence": "Payment terms: Net 30 days."},
    {"name": "Kohler", "document": DOC, "line_no": 5,
     "evidence": "We propose a Kohler CH440"},
]


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def scripted(*replies):
    """A fake model that answers from a list, then says finish."""
    it = iter(replies)

    def ask(system, user):
        r = next(it, {"action": "finish", "args": {}})
        if isinstance(r, Exception):
            raise r
        return r
    return ask


def v(cid, letter, quote, line):
    return {"id": cid, "letter": letter, "quote": quote, "document": DOC,
            "line": line}


def run(ask, text_dir, **kw):
    return m2_agent.classify_tender("t-1", CANDS, ask=ask, text_dir=text_dir,
                                    **kw)


def main():
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "2510"))
    with open(os.path.join(tmp, "2510", f"t-1__{DOC}.txt"), "w",
              encoding="utf-8") as f:
        f.write(TEXT)

    good = [
        {"action": "search", "args": {"text": "Honda"}},
        {"action": "classify", "args": {"verdicts": [
            v(1, "f", "must be a Honda GX500", 2)]}},           # not in text
        {"action": "classify", "args": {"verdicts": [
            v(1, "f", "The engine must be a Honda GX390", 2),
            v(2, "a", "Net 30 days", 3),
            v(3, "b", "We propose a Kohler CH440", 5)]}},
    ]
    out = run(scripted(*good), tmp)
    letters = {x["name"]: x["letter"] for x in out["verdicts"]}
    check("all three candidates lettered (f, a, b)",
          letters == {"Honda": "f", "Net": "a", "Kohler": "b"})
    check("a quote that is not in the text is refused",
          any("quote is not in" in t for t in out["trace"]))
    check("stops as soon as every candidate has a letter",
          out["steps"] == 3 and out["stop_reason"] == "every candidate lettered")
    check("the recorded line is where the quote really is",
          [x["line"] for x in out["verdicts"] if x["name"] == "Kohler"] == [5])

    loop = run(scripted(*good), tmp, use_graph=False)
    check("LangGraph and the plain loop give the same verdicts",
          sorted((x["name"], x["letter"]) for x in loop["verdicts"])
          == sorted((x["name"], x["letter"]) for x in out["verdicts"]))
    check("engine reported", out["engine"] in ("langgraph", "loop")
          and loop["engine"] == "loop")

    out = run(scripted({"action": "classify", "args": {"verdicts": [
        v(1, "f", "The engine must be a", 2)]}}), tmp)
    check("an 'f' whose quote does not contain the brand is refused",
          not out["verdicts"] and any("must contain" in t
                                      for t in out["trace"]))

    out = run(scripted(*[{"action": "search", "args": {"text": "engine"}}]
                       * 50), tmp)
    check("the step budget stops the loop; nothing is lettered",
          out["steps"] == m2_agent.MAX_STEPS and len(out["unresolved"]) == 3
          and out["stop_reason"] == "step budget used")

    out = run(scripted(ValueError("no JSON"), {"action": "finish"}), tmp)
    check("a reply that is not JSON costs one step and the loop goes on",
          out["steps"] == 2 and out["stop_reason"] == "finished by the model")

    out = run(scripted(llm.EmptyAnswer("groq: empty answer"),
                       {"action": "finish"}), tmp)
    check("an empty answer costs one step and the loop goes on",
          out["steps"] == 2 and out["stop_reason"] == "finished by the model")

    out = run(scripted(RuntimeError("every provider failed")), tmp)
    check("no model available: no verdict, everything unresolved",
          not out["verdicts"] and len(out["unresolved"]) == 3
          and out["stop_reason"].startswith("model unavailable"))

    text = m2_agent.TenderText("t-1", tmp)
    check("read marks the bidder's question",
          "5 [bidder question]:" in text.read(DOC, 5)
          and "2 [bidder question]" not in text.read(DOC, 5))

    seen, systems = [], []

    def spy(system, user):
        seen.append(user)
        systems.append(system)
        return {"action": "search", "args": {"text": "engine"}}
    run(spy, tmp)
    check("the last step tells the model to classify now",
          "LAST STEP" in seen[-2] and not any("LAST STEP" in u
                                              for u in seen[:-2]))
    check("a last step that is not 'classify' gets one more call",
          len(seen) == m2_agent.MAX_STEPS + 1
          and systems[-1] == m2_agent.FINAL_SYSTEM
          and "search" not in m2_agent.FINAL_SYSTEM)

    out = run(scripted(*([{"action": "search", "args": {"text": "x"}}]
                         * (m2_agent.MAX_STEPS - 1)
                         + [{"action": "search", "args": {"text": "x"}},
                            {"verdicts": [v(2, "a", "Net 30 days", 3)]}])),
              tmp)
    check("the extra last call can still letter candidates",
          [x["name"] for x in out["verdicts"]] == ["Net"]
          and out["steps"] == m2_agent.MAX_STEPS)
    check("a repeated search is not run again",
          any("already done at step 1" in t for t in out["trace"]))

    out = run(scripted({"action": "repo_browser.classify", "args": {
        "verdicts": [v(2, "a", "Net 30 days", 3)]}}), tmp)
    check("a tool name with a prefix ('repo_browser.') is understood",
          [x["name"] for x in out["verdicts"]] == ["Net"])

    out = run(scripted(RuntimeError("groq: HTTP 429")), tmp)
    check("the model's error is kept in the stop reason",
          "HTTP 429" in out["stop_reason"])

    out = m2_agent.classify_tender("t-1", [], ask=scripted(), text_dir=tmp)
    check("no candidates: the model is never called",
          out["steps"] == 0 and out["stop_reason"] == "no candidates")

    out = run(scripted({"action": "classify", "args": {"verdicts": [
        v(1, "f", "The engine ... Honda GX390", 2),
        v(2, "a", "30 days ... Payment terms", 3)]}}), tmp)
    letters = {x["name"]: x["letter"] for x in out["verdicts"]}
    check("a quote cut with '...' counts only if every piece is there, in order",
          letters == {"Honda": "f"})

    out = run(scripted({"name": "classify", "arguments": {"verdicts": [
        v(2, "a", "Net 30 days", 3)]}}), tmp)
    check("a native tool-call reply is read as an action",
          [x["name"] for x in out["verdicts"]] == ["Net"])

    body = ('{"error":{"code":"tool_use_failed","failed_generation":'
            '"{\\"name\\": \\"search\\", \\"arguments\\": '
            '{\\"text\\": \\"Axis\\"}}"}}')
    check("Groq's 'model called a tool' error gives back the call",
          llm._tool_slip(body) == '{"name": "search", "arguments": '
                                  '{"text": "Axis"}}'
          and llm._tool_slip('{"error":{"code":"other"}}') is None)

    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
