"""
EXPERIMENT - M4 with a model choosing the checks (--m4-checks model).

The same three checks as agents/m4_checks.py (re-read a brand finding, the
winner's spellings, the winner's other awards), but a model decides which
to run and when to stop, in a loop with a step budget (agents/agent_loop.py).
Scoring and routing stay in code; the model sets no points.

Measured and NOT chosen (tests/m4_eval.py, 73 labelled tenders, M2 on rules
only, 2026-11-04): 4 flagged, 2 of them false alarms, 14k tokens - against
2 flagged, 0 false, 70k tokens for the fixed order. The model saved tokens by
skipping the re-read on two tenders (cb-760-71260031 Renault/Volvo,
cb-952-81230837 Johnson Controls), and their false alarms stayed. Kept so
the comparison can be re-run; the main path does not import it.
"""


import json
from typing import TypedDict

from agents import agent_loop
from agents import m4_case_builder as m4
from agents import m4_checks as checks
from agents.m4_checks import _brand_findings

MAX_STEPS = 4


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


def _tools():
    """Native tool schemas (llm.tool); same tools as the prompt."""
    from tools import llm
    t = llm.tool
    return [
        t("recheck", "The spec agent re-reads these brand findings in the "
                     "whole tender.",
          {"brands": {"type": "array", "items": {"type": "string"}}},
          ["brands"]),
        t("spellings", "The award agent checks the winner's other spellings "
                       "in the award notices."),
        t("winner_awards", "Titles of the winner's other awards containing "
                           "the text. Shown to the auditor, never scored.",
          {"text": {"type": "string"}}, ["text"]),
        t("finish", "Stop."),
    ]


def available(state, hooks):
    """The checks still open for this tender, as the model is shown them."""
    out = {}
    r = checks._recheckable(state["m2"], hooks, state["rechecked"])
    if r:
        out["recheck"] = r
    if not state["spelled"] and checks._can_spell(
            state["m2"], state["m3"], state["case"], hooks):
        out["spellings"] = True
    s = checks._searchable(state["m2"], state["m3"], hooks, state["searched"])
    if s:
        out["winner_awards"] = s
    return out


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


def model_chooser(ask, hooks):
    def choose(state):
        return agent_loop.parse_action(
            agent_loop.ask_model(ask, SYSTEM, render(state, hooks)))
    return choose


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
                m2, new, obs = checks._recheck(
                    state["reference"], state["m2"], hooks, set(ok))
                followups += new
                rechecked += ok
                notes.append(obs)
            obs = "\n".join(notes) or "error: give 'brands': [...]"
        elif action == "spellings":
            if "spellings" not in av:
                obs = "refused - not available now"
            else:
                m3, new, obs = checks._spellings(hooks)
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
                new, obs = checks._winner_awards(state["m3"], hooks, text)
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


def coordinate(reference, m1, m2, m3, hooks, ask=None, use_graph=True):
    """Build the case, let the model choose the checks, re-score.
    Returns build_case's dict plus "m4_checks" and trace["M4"]."""
    case = m4.build_case(m1, m2, m3)
    state = AgentState(reference=reference, m2=m2 or {}, m3=m3 or {},
                       case=case, followups=[], rechecked=[], spelled=False,
                       searched=[], history=[], steps=0, done=False,
                       stop_reason="")
    if not _brand_findings(m2):
        return checks.mark(case, "model", [], "no brand finding")
    if not available(state, hooks):
        return checks.mark(case, "model", [], "nothing to check")
    if ask is None:
        from tools import llm
        ask = lambda system, user: llm.ask_json(
            system, user, strong=True, max_tokens=1500, tag="m4_agent",
            tools=_tools())
    decide, act = make_nodes(m1, hooks, model_chooser(ask, hooks))
    state, engine = agent_loop.run(state, decide, act, AgentState, MAX_STEPS,
                                   use_graph)
    if state["stop_reason"].startswith("model") and \
            checks.AGENT_DOWN == "stop":
        raise checks.AgentUnavailable(f"{reference}: {state['stop_reason']}")
    trace = [f"step {h['step']}: {h['action']} "
             f"{json.dumps(h['args'], ensure_ascii=False)[:120]} -> "
             f"{h['observation'].replace(chr(10), ' | ')[:300]}"
             for h in state["history"]]
    return checks.mark(state["case"], "model", trace,
                       f"{engine}, {state['steps']} step(s), "
                       f"{state['stop_reason']}")
