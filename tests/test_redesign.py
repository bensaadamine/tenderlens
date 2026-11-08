"""
The 2026-11-01 changes, tested offline (no data, no network, no tokens):

  * native tool calling in tools/llm.py: a tool call comes back as the JSON
    the agents read; the tool list is part of the cache key; a provider that
    rejects tools is asked again without them; TENDERLENS_NATIVE_TOOLS=off
    restores the old request; the local model stays out of the shared pool;
  * M2: brand names only from the requirements section; what the agent is
    shown before its first step (what the tender buys, the buyer's-equipment
    sentences) and the supply test in its instructions;
  * M3: decisions shared by a group of spellings; no chained merges;
  * the incremental run's --reprocess.

    python -m tests.test_redesign
"""


import io
import json
import os
import sys
import tempfile
import time
import urllib.error

from tools import llm
from agents import m2_agent
from agents import m3_agent
from tools import documents

FAILS = 0


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


# ------------------------------------------------------------------ llm.py
def test_llm():
    msg = {"content": "check the clause",
           "tool_calls": [{"function": {"name": "search",
                                        "arguments": '{"text": "equivalent"}'}},
                          {"function": {"name": "read", "arguments": "{}"}}]}
    got = json.loads(llm.tool_answer(msg))
    check("a native tool call reads as {thought, action, args}; first call",
          got == {"thought": "check the clause", "action": "search",
                  "args": {"text": "equivalent"}})

    # a fake provider, an in-memory cache, no usage log
    saved = {k: getattr(llm, k) for k in
             ("_post", "_load_cache", "_save_cache", "_log_usage",
              "_log_error", "models_for", "available")}
    cache, posts = {}, []
    llm._load_cache = lambda path=None: cache
    llm._save_cache = lambda c, path=None: None
    llm._log_usage = llm._log_error = lambda *a, **k: None
    llm.models_for = lambda p, **k: ("m", "m")
    prov = {"name": "groq", "env": "X", "base": "b"}
    llm.available = lambda provider=None: [prov]
    reject = {"on": True}

    def fake_post(p, model, system, user, mt, temp, timeout, tools=None):
        posts.append(tools)
        if tools and reject["on"]:
            raise urllib.error.HTTPError(
                "u", 400, "bad", {}, io.BytesIO(b'{"error": "tools are not '
                                                b'supported"}'))
        return ('{"action": "finish", "args": {}}', {})
    llm._post = fake_post
    try:
        T = [llm.tool("finish", "stop")]
        llm._no_tools.clear()
        llm.ask("s", "u", tools=T)
        check("a provider that rejects tools is asked again without them",
              posts == [T, None] and "groq" in llm._no_tools)
        llm._no_tools.clear()
        reject["on"] = False
        posts.clear()
        llm.ask("s2", "u", tools=T)
        llm.ask("s2", "u")
        check("the tool list is part of the cache key (two calls, not one)",
              posts == [T, None])
        posts.clear()
        llm.NATIVE_TOOLS = "off"
        llm.ask("s3", "u", tools=T)
        llm.NATIVE_TOOLS = "on"
        check("NATIVE_TOOLS = 'off' sends no tools", posts == [None])
    finally:
        for k, v in saved.items():
            setattr(llm, k, v)

    # the real bookkeeping, not a fake (a removed constant once broke it
    # while every test passed, 2026-11-04)
    saved_paths = llm.USAGE_PATH, llm.ERRORS_PATH
    tmp = tempfile.mkdtemp()
    llm.USAGE_PATH = os.path.join(tmp, "usage.csv")
    llm.ERRORS_PATH = os.path.join(tmp, "errors.csv")
    try:
        llm._log_usage("m2_agent", "groq", "m", {"total_tokens": 7}, False, 0)
        llm._log_error("m2_agent", "groq", "m", "HTTP 429", "quota")
        with io.open(llm.USAGE_PATH, encoding="utf-8") as f:
            rows = f.read().splitlines()
        check("the token log and the error log really write",
              len(rows) == 2 and rows[1].endswith(",7,0")
              and os.path.exists(llm.ERRORS_PATH))
    finally:
        llm.USAGE_PATH, llm.ERRORS_PATH = saved_paths

    # offline: a cached answer is served with no key; a missing one is
    # reported, never sent
    saved = {k: getattr(llm, k) for k in ("_post", "_load_cache", "_log_usage",
                                          "available")}
    sent = []
    model = llm.PROVIDERS[0]["strong"]
    llm._load_cache = lambda path=None: {llm._key(model, "s", "cached"):
                                         {"answer": "yes"}}
    llm._log_usage = lambda *a, **k: None
    llm.available = lambda: []                       # no key at all
    llm._post = lambda *a, **k: sent.append(a) or ("paid", {})
    os.environ["TENDERLENS_OFFLINE"] = "1"
    try:
        hit = llm.ask("s", "cached", strong=True)
        try:
            llm.ask("s", "never asked", strong=True)
            missed = False
        except llm.NotCached:
            missed = True
        check("offline: a cached answer needs no key; a miss is reported, "
              "nothing sent", hit == "yes" and missed and not sent)
    finally:
        os.environ.pop("TENDERLENS_OFFLINE", None)
        for k, v in saved.items():
            setattr(llm, k, v)

    groq, gemini = llm.PROVIDERS
    check("the pinned models are the ones the cache was filled with",
          llm.models_for(groq) == ("openai/gpt-oss-20b",
                                   "openai/gpt-oss-120b")
          and llm.models_for(gemini)[1] == "models/gemini-3-flash-preview")
    saved_win = dict(llm._windows)
    llm._windows["groq"] = [(time.time(), 10 ** 6)]     # Groq's minute is full
    try:
        check("Groq first even when busy: Gemini is only a fallback",
              llm._order([groq, gemini], 100) == [groq, gemini])
        llm.SHARE = True
        check("TENDERLENS_SHARE=1 restores sharing: the free one first",
              llm._order([groq, gemini], 100) == [gemini, groq])
    finally:
        llm.SHARE = False
        llm._windows.clear()
        llm._windows.update(saved_win)


# ------------------------------------------------------------------ M2
SPEC = """PART 1 GENERAL
The Contractor must deliver to 250 Howe Street, Suite 1400.
Questions from bidders will be answered by amendment.

ANNEX A - STATEMENT OF REQUIREMENT
1.1 The engine must be a Honda GX390 engine.
1.2 Length minimum 2.5 m.

ANNEX B - BASIS OF PAYMENT
Payment terms: Net 30 days.
"""


def test_m2():
    view = documents.requirement_view(SPEC)
    check("requirements view keeps ANNEX A, blanks the rest, same length",
          "Honda GX390" in view and "Howe Street" not in view
          and "Net 30" not in view and len(view) == len(SPEC))
    check("no requirements section: the whole document is kept",
          documents.requirement_view("Suite 1400\nHonda GX390") ==
          "Suite 1400\nHonda GX390")

    # what the agent is shown before its first step (2026-11-04)
    tmp = tempfile.mkdtemp()
    os.makedirs(os.path.join(tmp, "4110"))
    with io.open(os.path.join(tmp, "4110", "t-1__rfp.pdf.txt"), "w",
                 encoding="utf-8") as f:
        f.write("SCOPE\nThe simulator will be based on an existing wind\n"
                "turbine generator previously procured by NRC.\n"
                "Question 3: we already own a Honda pump?\nAnswer 3: no.\n"
                "ANNEX A\nGenerator Eocycle-C5000\n")
    text = m2_agent.TenderText("t-1", tmp)
    owned = text.owned()
    check("buyer's-equipment sentences are found, with the next line",
          "existing wind turbine generator previously procured by NRC"
          in owned)
    check("...but not inside a bidder's question", "Honda" not in owned)
    ctx = m2_agent.context(text, "Wind turbine simulator")
    check("the context says what the tender buys and what the buyer has",
          "What this tender buys: Wind turbine simulator" in ctx
          and "previously procured by NRC" in ctx)
    st = m2_agent.AgentState(reference="t-1", candidates=[{
        "id": 1, "name": "Eocycle", "document": "rfp.pdf", "line_no": 7,
        "evidence": "Generator Eocycle-C5000"}], open_ids=[1], verdicts={},
        context=ctx, history=[], steps=0, done=False, stop_reason="")
    check("the model sees the context before its first step",
          "previously procured by NRC" in m2_agent.render(st))
    check("the instructions carry the supply test; no facts tool",
          "must SUPPLY" in m2_agent.SYSTEM
          and "facts" not in [t["name"] for t in m2_agent._tools()])


# ------------------------------------------------------------------ M3
def test_m3():
    from tests import test_m3_agent as fx
    tmp = tempfile.mkdtemp()
    g = fx.make_graph(tmp)
    index = m3_agent.AwardIndex(g)
    w, acc, other = ("AGILENT LABS SOLUTIONS", ["AGILENT TECHNOLOGIES"],
                     "TOWER DENTAL CLINIC")
    check("no chain: a postal code shared only with an accepted spelling "
          "is not enough", m3_agent.allowed_same(index, w, other, acc)
          is not None)
    m3_agent.CHAIN = True
    check("CHAIN = True restores the old rule",
          m3_agent.allowed_same(index, w, other, acc) is None)
    m3_agent.CHAIN = False

    path = os.path.join(tmp, "aliases.csv")
    m3_agent.save_aliases([{"winner": "AGILENT TECHNOLOGIES CANADA",
                            "name": "AGILENT TECHNOLOGIES", "verdict": "same",
                            "reason": "x", "decided_by": "agent",
                            "date": "2026-11-01"},
                           {"winner": "AGILENT TECHNOLOGIES CANADA",
                            "name": "TOWER DENTAL CLINIC",
                            "verdict": "different", "reason": "x",
                            "decided_by": "agent", "date": "2026-11-01"}],
                          path)
    ask = fx.scripted()
    out = m3_agent.aliases_for("AGILENT TECHNOLOGIES", g, ask=ask,
                               path=path, use_graph=False)
    check("a merged spelling that wins later reuses the group's decisions",
          "AGILENT TECHNOLOGIES CANADA" in out["same"]
          and "TOWER DENTAL CLINIC" in out["different"]
          and "AGILENT TECHNOLOGIES CANADA" not in out["asked"]
          and "TOWER DENTAL CLINIC" not in out["asked"])
    m3_agent.GROUPS = False
    out = m3_agent.aliases_for("AGILENT TECHNOLOGIES", g, ask=fx.scripted(),
                               path=path, use_graph=False, store=False)
    m3_agent.GROUPS = True
    check("GROUPS = False restores per-winner decisions (asked again)",
          "AGILENT TECHNOLOGIES CANADA" in out["asked"])


# ------------------------------------------------------------------ stream
def test_reprocess():
    from agents import m1_screener
    from pipeline import stream as ts
    kb = ts.KB("9999", tempfile.mkdtemp())
    notices = {r: {m1_screener.C_UNSPSC: "99990000",
                   m1_screener.C_PUB: d}
               for r, d in (("t-1", "2025-02-01"), ("t-2", "2025-01-01"),
                            ("t-3", "2025-03-01"))}
    for ref, pile in (("t-1", "quarantine"), ("t-2", "quarantine"),
                      ("t-3", "base")):
        kb.append_event(reference=ref, run_id="r", event="processed",
                        status="watch", pile=pile)
    kb.append_event(reference="t-1", run_id="r", event="feedback",
                    status="confirmed", pile="quarantine")
    state = kb.state()
    q = ts.reprocess_list(kb, notices, "9999", state, ["quarantine"])
    check("--reprocess quarantine: quarantined tenders, the judged one kept",
          q == ["t-2"])
    a = ts.reprocess_list(kb, notices, "9999", state, ["all"])
    check("--reprocess all: every unjudged tender, oldest first",
          a == ["t-2", "t-3"])
    n = ts.reprocess_list(kb, notices, "9999", state, ["t-3", "nope"])
    check("--reprocess REF: named tenders of the category only", n == ["t-3"])


def main():
    test_llm()
    test_m2()
    test_m3()
    test_reprocess()
    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
