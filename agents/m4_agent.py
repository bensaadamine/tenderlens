"""
M4 AGENT - the coordinator. Before a case is ranked, it decides which
follow-up questions to put to M2 and M3; code re-scores the case after
every answer.

Why: a case's route usually rests on one brand finding, and two cheap
checks change it most often - is the finding real (on 4110, 9 of 11 rule
findings were false), and is the brand the winner's under another spelling.
They are not worth running on every tender, so the agent runs only where a
brand finding exists and asks only what is still open.

    GOAL   answer the follow-ups that could change the case's route
    TOOLS  recheck        {"brands": [...]}  the M2 agent re-reads them
           spellings      {}                 the M3 agent: the winner's
                                             other spellings, counts redone
           winner_awards  {"text": "..."}    the winner's other award titles
                                             containing the text (shown,
                                             never scored)
           finish         {}
    LOOP   decide -> act (+ re-score) -> ... until finish, MAX_STEPS, or no
           tool left to run.

What stays in CODE, never left to the model:
  * scoring and routing: m4_case_builder.build_case, re-run after every
    answer. The model adds no claim, sets no points, writes no case text;
  * M2's and M3's own checks (verified quotes; "same company" needs a
    shared postal code or matching names);
  * a re-read finding stays only on letter "f"; another letter clears it;
    no letter leaves it unresolved (as with --agent);
  * each follow-up runs at most once; a tender without a brand finding is
    never sent to the model.

mode "fixed" runs every available follow-up in a fixed order, no model -
the baseline that shows whether the model's choices add anything.

    from m4_agent import coordinate, default_hooks
    case = coordinate(ref, m1, m2, m3, default_hooks(ref, row, graph, m3))
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

import copy
import json
from typing import TypedDict

import m4_case_builder as m4

try:
    from langgraph.graph import END, StateGraph
except ImportError:          # same loop, run by hand (see run_loop)
    StateGraph = None

MAX_STEPS = 4
AWARDS_SHOWN = 4
BRAND = "brand_without_equivalent"
# Same letters as m2_agent.LETTERS (tests check it), worded for the page.
LETTERS = {
    "a": "not a brand",
    "b": "a bidder is speaking",
    "c": "the buyer's own equipment",
    "d": "one of several brands listed",
    "e": "an equivalent is accepted",
    "f": "brand required, no equivalent",
}
# No model answering: "stop" raises AgentUnavailable (the run stops and
# resumes from this tender, as for M2/M3); "keep" ranks the case as it
# stands.
AGENT_DOWN = "stop"


class AgentUnavailable(RuntimeError):
    """--m4-agent was asked for, but no model could answer."""


class Hooks:
    """The follow-ups for one tender. Tests pass fakes.

    recheck(cands)      -> m2_agent.classify_tender's result
    spellings()         -> a new M3 result counting the winner's spellings
    winner_awards(text, keys) -> {"count": n, "lines": [...]}
    """

    def __init__(self, recheck=None, spellings=None, winner_awards=None):
        self.recheck = recheck
        self.spellings = spellings
        self.winner_awards = winner_awards


def default_hooks(reference, notice_row, graph, m3, online=False):
    """The real follow-ups: the M2 agent, the M3 agent, the award index."""
    import m2_agent
    import m3_investigator as m3i
    key = ((m3 or {}).get("award") or {}).get("supplier_key")

    def recheck(cands):
        return m2_agent.classify_tender(reference, cands, online=online)

    def spellings():
        trace = []
        same = m3i._agent_aliases(key, graph, trace)
        out = m3i.investigate(reference, notice_row, graph, spellings=same)
        out["trace"] = trace + out["trace"]
        return out

    own_sol = m3i.norm_sol((notice_row or {}).get(m3i.C_SOL_TENDER))

    def winner_awards(text, keys):
        needle = text.lower()
        recs = [r for n in [key] + list(keys or [])
                for r in graph["by_sup"].get(n, [])
                if needle in (r.get("title") or "").lower()
                and r.get("ref") != reference
                and not (own_sol and r.get("sol") == own_sol)]
        recs.sort(key=lambda r: r.get("date") or "", reverse=True)
        return {"count": len(recs),
                "lines": [f"{r.get('date') or '?'} | "
                          f"{(r.get('buyer_name') or '')[:40]} | "
                          f"{(r.get('title') or '')[:80]}"
                          for r in recs[:AWARDS_SHOWN]]}

    ok = bool(graph and key)
    return Hooks(recheck, spellings if ok else None,
                 winner_awards if ok else None)


# ------------------------------------------------------- what is still open
def _brand_findings(m2):
    return [f for f in (m2 or {}).get("findings", [])
            if f.get("kind") == BRAND and m4._verified(f.get("source"))]


def _winner(state):
    return ((state["m3"] or {}).get("award") or {}).get("supplier")


def _recheckable(state, hooks):
    """Brand findings the M2 agent has not lettered yet."""
    if not hooks.recheck:
        return []
    out = []
    for f in _brand_findings(state["m2"]):
        b = f.get("brand") or ""
        if not f.get("agent_letter") and b.lower() not in state["rechecked"] \
                and b not in out:
            out.append(b)
    return out


def _can_spell(state, hooks):
    return bool(hooks.spellings and _winner(state) and not state["spelled"]
                and not (state["m3"] or {}).get("agent")
                and _brand_findings(state["m2"])
                and not any(c["kind"] == "brand_matches_winner"
                            for c in state["case"]["claims"]))


def _searchable(state, hooks):
    """Brands worth looking for in the winner's other award titles."""
    if not (hooks.winner_awards and _winner(state)):
        return []
    return [f["brand"] for f in _brand_findings(state["m2"])
            if f["brand"].lower() not in state["searched"]]


def available(state, hooks):
    out = {}
    r = _recheckable(state, hooks)
    if r:
        out["recheck"] = r
    if _can_spell(state, hooks):
        out["spellings"] = True
    s = _searchable(state, hooks)
    if s:
        out["winner_awards"] = s
    return out


# ----------------------------------------------------------------- the state
class AgentState(TypedDict):
    reference: str
    m2: dict
    m3: dict
    case: dict
    followups: list
    rechecked: list          # brands (lowercase) already re-read
    spelled: bool
    searched: list           # texts (lowercase) already searched
    history: list
    steps: int
    done: bool
    stop_reason: str


SYSTEM = """You coordinate the audit of one Canadian public tender. Fixed
rules score the case from what the other agents found and give it a route:
alert, watch or close. Before it is ranked, ask the follow-up questions that
could change that route; stop when none could.

Tools:
recheck        {"brands": ["..."]}  the spec agent re-reads those brand
               findings in the whole tender (bidders' questions, "or
               equivalent", the buyer's own equipment). Findings marked
               "rules only" are often false: re-read them.
spellings      {}  the award agent checks the winner's other spellings in
               the award notices; the brand may be the winner's under
               another name, or the winner a recurring one.
winner_awards  {"text": "..."}  titles of the winner's other awards that
               contain the text (e.g. the brand). Shown to the auditor,
               never scored.
finish         {}  stop.

Only tools under "Available now" can run, each once. Re-read first: a
finding that goes away makes the other questions pointless.

Reply with ONE JSON object and nothing else:
{"thought": "<one sentence>", "action": "<tool>", "args": {...}}
"""


def render(state, hooks):
    case = state["case"]
    out = [f"Tender {state['reference']}. Steps left: "
           f"{MAX_STEPS - state['steps']}.",
           f"Route now: {case['route']} (score {case['score']}). Alert needs "
           f"{m4.ALERT} points from document findings including a brand; "
           f"watch needs {m4.WATCH}.", "", "Claims that score:"]
    scored = [c for c in case["claims"] if c.get("points")]
    out += [f"  - {c['points']} pt ({c.get('nature') or c['kind']}): "
            f"{(c.get('plain') or c['claim'])[:200]}" for c in scored] \
        or ["  (none)"]
    out += ["", "Brand findings:"]
    for f in _brand_findings(state["m2"]):
        s = f["source"]
        who = (f"lettered {f['agent_letter']} by the spec agent"
               if f.get("agent_letter") else "rules only")
        out.append(f"  - \"{f['brand']}\" - {s.get('file')}, "
                   f"{s.get('where')}: {str(s.get('line'))[:160]} [{who}]")
    aw = (state["m3"] or {}).get("award") or {}
    if aw.get("supplier"):
        sp = ("checked: " + ("; ".join(aw.get("other_spellings") or [])
                             or "none the same")
              if (state["m3"] or {}).get("agent") else "not checked")
        out.append(f"\nWinner: {aw['supplier']} (other spellings {sp}).")
    else:
        out.append("\nWinner: unknown.")
    av = available(state, hooks)
    out.append("Available now: " + (", ".join(
        k + (f" {json.dumps(v, ensure_ascii=False)}"
             if isinstance(v, list) else "")
        for k, v in av.items()) or "nothing - finish"))
    if state["history"]:
        out += ["", "What you did so far:"]
        for h in state["history"]:
            out.append(f"- step {h['step']}: {h['action']} "
                       f"{json.dumps(h['args'], ensure_ascii=False)[:160]}")
            out.append("  -> " + h["observation"][:800]
                       .replace("\n", "\n     "))
    return "\n".join(out)


def _as_action(reply):
    """Normalise a reply into (action, args, thought)."""
    if not isinstance(reply, dict):
        return "", {}, ""
    if "action" not in reply and "name" in reply:   # a native tool call
        reply = {"action": reply.get("name"),
                 "args": reply.get("arguments") or {}}
    action = str(reply.get("action", "")).strip().lower().split(".")[-1]
    args = reply.get("args") if isinstance(reply.get("args"), dict) else {}
    return action, args, str(reply.get("thought", ""))[:300]


def model_chooser(ask, hooks):
    def choose(state):
        try:
            reply = ask(SYSTEM, render(state, hooks))
        except ValueError:            # not JSON: a wasted step
            reply = {}
        except RuntimeError as e:     # an empty answer is a wasted step too
            if type(e).__name__ != "EmptyAnswer":
                raise
            reply = {}
        return _as_action(reply)
    return choose


def fixed_chooser(hooks):
    """Every available follow-up, in a fixed order, no model."""
    def choose(state):
        av = available(state, hooks)
        if "recheck" in av:
            return "recheck", {"brands": av["recheck"]}, "fixed order"
        if "spellings" in av:
            return "spellings", {}, "fixed order"
        if "winner_awards" in av:
            return "winner_awards", {"text": av["winner_awards"][0]}, \
                "fixed order"
        return "finish", {}, "fixed order"
    return choose


# ----------------------------------------------------------- the follow-ups
def _recheck(state, hooks, brands):
    """Hand the findings back to the M2 agent; apply its letters."""
    m2 = copy.deepcopy(state["m2"])
    rows = [f for f in _brand_findings(m2)
            if (f.get("brand") or "").lower() in brands
            and not f.get("agent_letter")]
    cands = [{"name": f["brand"], "document": f["source"].get("file"),
              "line_no": f.get("line_no"), "evidence": f["source"].get("line")}
             for f in rows]
    out = hooks.recheck(cands)
    if not out.get("steps") and str(out.get("stop_reason", "")) \
            .startswith("model"):
        if AGENT_DOWN == "stop":
            raise AgentUnavailable(f"{state['reference']}: "
                                   f"{out['stop_reason']}")
        return m2, [], "the spec agent has no model - findings kept"
    by_id = {v.get("id"): v for v in out.get("verdicts", [])}
    l1 = m2.setdefault("level1", {}) or {}
    m2["level1"] = l1
    gone, followups, notes = set(), [], []
    for i, f in enumerate(rows, 1):
        b, v = f["brand"], by_id.get(i)
        if v is None:
            gone.add(id(f))
            l1["unresolved"] = (l1.get("unresolved") or 0) + 1
            res, text = "unresolved", (f"'{b}' was re-read by the spec agent "
                                       f"but could not be settled; it no "
                                       f"longer counts.")
        else:
            where = f"{v.get('document')}, line {v.get('line')}"
            if v["letter"] == "f":
                f.update(agent_letter="f", confidence="high",
                         line_no=v.get("line"),
                         detail="re-read by the M2 agent: required, no "
                                "equivalent accepted")
                f["source"] = dict(f["source"], file=v.get("document"),
                                   where=f"page {v.get('page')}, line "
                                         f"{v.get('line')}",
                                   line=v.get("quote"))
                res, text = "kept", (f"'{b}' was re-read by the spec agent: "
                                     f"required, no equivalent accepted "
                                     f"(\u201c{v.get('quote')}\u201d, "
                                     f"{where}).")
            else:
                gone.add(id(f))
                l1["ok"] = (l1.get("ok") or 0) + 1
                res, text = "cleared", (
                    f"'{b}' was re-read by the spec agent and removed - "
                    f"{LETTERS.get(v['letter'], v['letter'])}: "
                    f"\u201c{v.get('quote')}\u201d ({where}).")
        followups.append({"tool": "recheck", "brand": b, "result": res,
                          "letter": (v or {}).get("letter"),
                          "quote": (v or {}).get("quote"), "text": text})
        notes.append(f"{b}: {res}"
                     + (f" ({v['letter']})" if v else ""))
    m2["findings"] = [f for f in m2.get("findings", []) if id(f) not in gone]
    m2["trace"] = list(m2.get("trace", [])) + [
        "M4 recheck: " + t for t in out.get("trace", [])]
    return m2, followups, "; ".join(notes)


def _spellings(hooks):
    new = hooks.spellings()
    sp = (new.get("award") or {}).get("other_spellings") or []
    text = ("The winner's other spellings in the award notices were checked: "
            + ("; ".join(sp) + " counted as the same company." if sp
               else "none is the same company."))
    return new, {"tool": "spellings", "result": sp, "text": text}, \
        ("same company: " + "; ".join(sp) if sp else "no other spelling "
         "is the same company")


def _winner_awards(state, hooks, text):
    keys = ((state["m3"] or {}).get("award") or {}).get("spelling_keys") or []
    res = hooks.winner_awards(text, keys)
    n, lines = res.get("count", 0), res.get("lines", [])
    page = (f"Other awards to the winner whose title mentions '{text}': "
            + (f"{n}, e.g. " + " / ".join(lines[:2]) if n else "none")
            + " (shown for the auditor, not scored).")
    obs = f"{n} award(s)" + ("\n" + "\n".join(lines) if lines else "")
    return {"tool": "winner_awards", "text_searched": text, "count": n,
            "lines": lines, "text": page}, obs


# ----------------------------------------------------------- decide and act
def make_nodes(m1, hooks, choose):
    def decide(state):
        try:
            action, args, thought = choose(state)
        except Exception as e:        # no provider, every provider failed
            return {"done": True,
                    "stop_reason": f"model unavailable: {str(e)[:200]}"}
        return {"history": state["history"] + [{
                    "step": state["steps"] + 1, "action": action,
                    "args": args, "thought": thought, "observation": ""}],
                "steps": state["steps"] + 1}

    def act(state):
        h = dict(state["history"][-1])
        action, args = h["action"], h["args"]
        av = available(state, hooks)
        m2, m3 = state["m2"], state["m3"]
        followups = list(state["followups"])
        rechecked, searched = list(state["rechecked"]), list(state["searched"])
        spelled, done = state["spelled"], False
        if action == "recheck":
            names = args.get("brands") or args.get("names") or \
                args.get("brand") or []
            names = [names] if isinstance(names, str) else names
            open_ = {b.lower() for b in av.get("recheck", [])}
            want = [str(n).strip().lower() for n in names
                    if isinstance(n, (str, int))]
            ok = [n for n in dict.fromkeys(want) if n in open_]
            bad = [n for n in want if n not in open_]
            notes = [f"'{n}': refused - not a finding waiting to be re-read "
                     f"(available: {sorted(open_) or 'none'})" for n in bad]
            if ok:
                m2, new, obs = _recheck(state, hooks, set(ok))
                followups += new
                rechecked += ok
                notes.append(obs)
            obs = "\n".join(notes) or "error: give 'brands': [...]"
        elif action == "spellings":
            if "spellings" not in av:
                obs = "refused - not available now"
            else:
                m3, new, obs = _spellings(hooks)
                followups.append(new)
                spelled = True
        elif action == "winner_awards":
            text = str(args.get("text") or "").strip()
            if "winner_awards" not in av:
                obs = "refused - not available now"
            elif len(text) < 3:
                obs = "error: give a 'text' of at least 3 characters"
            elif text.lower() in searched:
                obs = "refused - already searched"
            else:
                new, obs = _winner_awards(state, hooks, text)
                followups.append(new)
                searched.append(text.lower())
        elif action == "finish":
            obs, done = "finished", True
        elif not action:
            obs = "error: reply with one JSON object with an 'action'"
        else:
            obs = (f"error: unknown action '{action}'. Use one of: recheck, "
                   f"spellings, winner_awards, finish")
        h["observation"] = obs
        case = m4.build_case(m1, m2, m3, followups)
        new_state = dict(state, m2=m2, m3=m3, case=case, rechecked=rechecked,
                         searched=searched, spelled=spelled)
        left = available(new_state, hooks)
        stop = done or not left or state["steps"] >= MAX_STEPS
        reason = ("finished by the model" if done else
                  "nothing left to check" if not left else
                  "step budget used" if stop else "")
        return {"history": state["history"][:-1] + [h], "m2": m2, "m3": m3,
                "case": case, "followups": followups, "rechecked": rechecked,
                "searched": searched, "spelled": spelled, "done": stop,
                "stop_reason": reason}

    return decide, act


# ------------------------------------------------------------------ the loop
def build_graph(decide, act):
    g = StateGraph(AgentState)
    g.add_node("decide", decide)
    g.add_node("act", act)
    g.set_entry_point("decide")
    g.add_conditional_edges("decide", lambda s: "end" if s["done"] else "act",
                            {"act": "act", "end": END})
    g.add_conditional_edges("act", lambda s: "end" if s["done"] else "decide",
                            {"decide": "decide", "end": END})
    return g.compile()


def run_loop(state, decide, act):
    """The same loop without langgraph."""
    while not state["done"]:
        state = {**state, **decide(state)}
        if state["done"]:
            break
        state = {**state, **act(state)}
    return state


def coordinate(reference, m1, m2, m3, hooks, mode="model", ask=None,
               use_graph=True):
    """Build the case, ask the follow-ups that are still open, re-score.

    mode: "model" (the model chooses) or "fixed" (every follow-up, no
    model). Returns build_case's dict plus "m4_agent" and trace["M4"].
    """
    case = m4.build_case(m1, m2, m3)
    state = AgentState(reference=reference, m2=m2 or {}, m3=m3 or {},
                       case=case, followups=[], rechecked=[], spelled=False,
                       searched=[], history=[], steps=0, done=False,
                       stop_reason="")
    if not _brand_findings(m2):
        return _mark(case, mode, "none", 0, "no brand finding", [])
    if not available(state, hooks):
        return _mark(case, mode, "none", 0, "nothing to check", [])

    if mode == "fixed":
        choose = fixed_chooser(hooks)
    else:
        if ask is None:
            import llm
            ask = lambda system, user: llm.ask_json(
                system, user, strong=True, max_tokens=1500, tag="m4_agent")
        choose = model_chooser(ask, hooks)
    decide, act = make_nodes(m1, hooks, choose)
    engine = "langgraph" if (use_graph and StateGraph) else "loop"
    if engine == "langgraph":
        state = build_graph(decide, act).invoke(
            state, {"recursion_limit": 4 * MAX_STEPS + 4})
    else:
        state = run_loop(state, decide, act)

    if state["stop_reason"].startswith("model") and AGENT_DOWN == "stop":
        raise AgentUnavailable(f"{reference}: {state['stop_reason']}")
    trace = [f"step {h['step']}: {h['action']} "
             f"{json.dumps(h['args'], ensure_ascii=False)[:120]} -> "
             f"{h['observation'].replace(chr(10), ' | ')[:300]}"
             for h in state["history"]]
    return _mark(state["case"], mode, engine, state["steps"],
                 state["stop_reason"], trace)


def _mark(case, mode, engine, steps, reason, trace):
    case["m4_agent"] = {"mode": mode, "engine": engine, "steps": steps,
                        "stop_reason": reason}
    case["trace"]["M4"] = [f"agent ({mode}, {engine}, {steps} step(s), "
                           f"{reason})"] + trace
    return case
