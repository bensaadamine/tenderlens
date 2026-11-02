"""
The 2026-11-01 changes, tested offline (no data, no network, no tokens):

  * native tool calling in tools/llm.py: a tool call comes back as the JSON
    the agents read; the tool list is part of the cache key; a provider that
    rejects tools is asked again without them; TENDERLENS_NATIVE_TOOLS=off
    restores the old request; the local model stays out of the shared pool;
  * M2: brand names only from the requirements section; the facts module
    keeps only facts whose quote is in the document; the agent's facts tool;
  * M3: decisions shared by a group of spellings; no chained merges;
  * the incremental run's --reprocess.

    python tests/test_redesign.py
"""

import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import repo  # noqa: E402  (agents/ tools/ prepare/ tests/ importable; works from the repo root)

import io
import json
import os
import sys
import tempfile
import urllib.error

import facts
import llm
import m2_agent
import m3_agent
import documents

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

    os.environ["TENDERLENS_LOCAL_MODEL"] = "gemma4:e4b"
    os.environ.pop("TENDERLENS_LOCAL_SHARE", None)
    try:
        check("the local model stays out of the shared pool",
              all(not p.get("local") for p in llm.available()))
        loc = llm.available("ollama")
        check("...but is there when asked for, with the user's model",
              len(loc) == 1 and llm.models_for(loc[0])
              == ("gemma4:e4b", "gemma4:e4b")
              and llm._base(loc[0]).endswith(":11434/v1"))
    finally:
        os.environ.pop("TENDERLENS_LOCAL_MODEL", None)


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

    docs = [{"document": "rfp.pdf", "category": "x", "text": SPEC},
            {"document": "amd.pdf", "category": "x",
             "text": "Answer 3: The Honda engine is the one the buyer "
                     "already owns.\nThe existing generator procured by "
                     "NRC must be used.\n"}]
    ps = facts.passages(docs, ["Honda"])
    check("facts read passages outside the requirements section only",
          ps and all("must be a Honda GX390" not in t for _d, _l, t in ps)
          and any("already owns" in t for _d, _l, t in ps))

    def fake_local(system, user):
        return {"facts": [
            {"name": "Honda", "kind": "buyer_owns", "document": "amd.pdf",
             "line": 1, "quote": "The Honda engine is the one the buyer "
                                 "already owns"},
            {"name": "Honda", "kind": "buyer_answer", "document": "amd.pdf",
             "line": 1, "quote": "Honda is refused"},          # invented
            {"name": "*", "kind": "opinion", "document": "amd.pdf",
             "line": 2, "quote": "The existing generator"}]}   # bad kind
    facts.FACTS_DIR = tempfile.mkdtemp()
    out = facts.get("t-1", ["Honda"], ask=fake_local, docs=docs)
    check("a fact is kept only if its quote is in the document and its "
          "kind is known", [f["kind"] for f in out["facts"]] == ["buyer_owns"])
    check("facts are stored and reused without asking again",
          facts.get("t-1", ["Honda"], ask=None, docs=docs)["facts"]
          == out["facts"])
    shown = facts.facts_for("t-1", ["Honda"], "Honda", docs=docs)
    check("the facts tool shows the kept fact", "buyer_owns:" in shown
          and "already owns" in shown)
    os.environ.pop("TENDERLENS_LOCAL_MODEL", None)
    none = facts.facts_for("t-2", ["Bendix"], "Bendix", docs=docs)
    check("no local model: the tool says so, nothing stored",
          none.startswith("no facts: no local model")
          and not os.path.exists(facts._path("t-2")))

    # the agent dispatches the facts tool
    seen = []
    replies = iter([{"action": "facts", "args": {"name": "Honda"}},
                    {"action": "finish", "args": {}}])
    text = m2_agent.TenderText.__new__(m2_agent.TenderText)
    text.docs, text.bidder, text.pages = {}, {}, {}
    decide, act = m2_agent.make_nodes(
        text, lambda s, u: next(replies), facts_fn=lambda n: seen.append(n)
        or "buyer_owns: \u201cx\u201d")
    st = m2_agent.AgentState(reference="t", candidates=[{"id": 1,
                             "name": "Honda", "document": "rfp.pdf",
                             "line_no": 5, "evidence": "Honda GX390"}],
                             open_ids=[1], verdicts={}, history=[], steps=0,
                             done=False, stop_reason="")
    st = m2_agent.agent_loop.run_loop(st, decide, act)
    check("the M2 agent's facts tool reaches the facts module",
          seen == ["Honda"]
          and st["history"][0]["observation"].startswith("buyer_owns"))
    check("tool schemas: facts offered when on, not when off",
          "facts" in [t["name"] for t in m2_agent._tools()])
    facts.FACTS = "off"
    check("...off: the facts tool and its prompt line are gone",
          "facts" not in [t["name"] for t in m2_agent._tools()]
          and m2_agent.system_prompt() == m2_agent.SYSTEM_NO_FACTS)
    facts.FACTS = "on"


# ------------------------------------------------------------------ M3
def test_m3():
    import test_m3_agent as fx
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
    import m1_screener
    import tenderlens_stream as ts
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
