"""
M3 AGENT - decides which other spellings in the award file are the SAME
supplier as a tender's winner.

Why an agent: the award file writes one company many ways ("AGILENT
TECHNOLOGIES" / "AGILENT TECHNOLOGIES CANADA", "OLYMPUS NDT CANADA" /
"EVIDENT CANADA" after a rebrand), and different companies share one
address (an office tower, an industrial park). Measured 2026-10-26: of the
winners in 2510 and 4110, 69 and 128 share a postal code with another name,
and the right answer differs case by case. Counting variants apart hides a
recurring winner; merging two companies invents one. So the agent looks,
and code checks what it may conclude.

    GOAL   for each candidate spelling: same company as the winner, or not
    TOOLS  variants  {"name"}  other spellings near a name: same postal code,
                               or same first word; with city, codes, awards
           awards    {"name"}  what a name won: date, buyer, category, title
           decide    {"same": [...], "different": [...], "reason": "..."}
           finish    {}
    LOOP   decide -> act -> ... until every candidate is decided, the model
           finishes, or MAX_STEPS is reached; the last call has no tools.

What stays in CODE, never left to the model:
  * only names the tools have shown can be decided;
  * "same" needs a shared postal code with the winner or one of its accepted
    spellings, or the two names equal once generic words are dropped
    ("CANADA", "GROUP"...). A joint venture is never the same as a partner.
  * the counts (shares, buyers) are computed by m3_investigator from the
    decisions; the model never produces a number.
  * every decision is stored in reports/supplier_aliases.csv with its
    reason; a row the auditor writes (decided_by = auditor) wins, and a
    pair already decided is never asked again.

    from agents.m3_agent import aliases_for
    out = aliases_for("AGILENT TECHNOLOGIES", graph)   # -> {"same": [...]}
"""


import csv
import io
import json
import os
import re
import time
from typing import TypedDict

from agents import agent_loop

ALIASES = os.path.join("reports", "supplier_aliases.csv")
ALIAS_COLS = ["winner", "name", "verdict", "reason", "decided_by", "date"]
MAX_STEPS = 6
MAX_CANDIDATES = 12      # spellings offered per winner (closest first)
AWARDS_SHOWN = 6

# Dropped before comparing two names: they say where or what kind of
# company, not which one.
GENERIC = {"CANADA", "CANADIAN", "GROUP", "THE", "LE", "LA", "LES", "DE",
           "DU", "DES", "ET", "AND", "OF", "LLP", "LP", "INTERNATIONAL",
           "DIVISION", "DIV", "HOLDINGS"}
RE_JV = re.compile(r"\b(JOINT VENTURE|J V|JV|S E N C|SENC|COENTREPRISE)\b")


def core(name):
    """A name without its generic words: 'AGILENT TECHNOLOGIES CANADA' ->
    'AGILENT TECHNOLOGIES'."""
    return " ".join(w for w in name.split() if w not in GENERIC)


# What describe() adds after a name: " - Ottawa (K2E7L6); 6 award(s)".
# Measured 2026-11-01: the model copied whole description lines into
# "decide" and every one was refused, 6 steps running.
RE_DESCRIPTION = re.compile(
    r"\s+-\s+[^()]*\([^()]*\)(?:;\s*\d+\s+AWARD\(S\))?\s*$", re.I)


def clean_name(text):
    """A name as the model wrote it, without the description we added."""
    return RE_DESCRIPTION.sub("", str(text or "").strip().upper()).strip()


def first_word(name):
    words = [w for w in core(name).split() if len(w) >= 4 and not w.isdigit()]
    return words[0] if words else ""


# ------------------------------------------------------- the award file
class AwardIndex:
    """Read-only views of the award graph built by m3_investigator."""

    def __init__(self, graph):
        self.g = graph
        self._by_first = {}
        for sup in graph["by_sup"]:
            w = first_word(sup)
            if w:
                self._by_first.setdefault(w, set()).add(sup)

    def codes(self, name):
        return set(self.g["postal"].get(name, ()))

    def describe(self, name):
        g = self.g
        cities = ", ".join(c for c, _ in g["city"].get(name, {}).most_common(2))
        codes = ", ".join(sorted(self.codes(name))) or "no postal code"
        return (f"{name} - {cities or 'city unknown'} ({codes}); "
                f"{len(g['by_sup'].get(name, []))} award(s)")

    def variants(self, name):
        """Other names at one of its postal codes, or with its first word."""
        out = set()
        for pc in self.codes(name):
            out |= self.g["at_postal"].get(pc, set())
        w = first_word(name)
        if w:
            out |= self._by_first.get(w, set())
        out.discard(name)
        shared = self.codes(name)
        # closest first: shared code and shared first word, then either
        return sorted(out, key=lambda n: (not (self.codes(n) & shared),
                                          first_word(n) != w, n)
                      )[:MAX_CANDIDATES]

    def awards(self, name):
        recs = sorted(self.g["by_sup"].get(name, []),
                      key=lambda r: r["date"], reverse=True)
        if not recs:
            return f"no award to '{name}'"
        lines = [f"{r['date'] or '?'} | {r['buyer_name'][:50]} | "
                 f"cat {r['group'] or '?'} | {r['title'][:70]}"
                 for r in recs[:AWARDS_SHOWN]]
        more = len(recs) - AWARDS_SHOWN
        return "\n".join(lines) + (f"\n... {more} more" if more > 0 else "")


# A merge must share a postal code with the WINNER ITSELF (or have the same
# name once generic words are dropped). Sharing one with an accepted
# spelling is not enough: merges would chain - A shares a code with B, B with
# C, and C becomes A without sharing anything with it.
# A decision applies to the whole group of spellings judged the same
# company, so a spelling merged into FISHER SCIENTIFIC is not asked again
# from scratch the day it wins a tender itself (aliases_for).


def allowed_same(index, winner, name, accepted):
    """Code's rule for "same". Returns None if allowed, else why not."""
    if RE_JV.search(name) and not RE_JV.search(winner):
        return "a joint venture is not the same company as a partner"
    if core(name) == core(winner):
        return None
    if index.codes(name) & index.codes(winner):
        return None
    return ("no shared postal code with the winner, and the names differ "
            "beyond generic words")


def group_of(winner, stored):
    """Every spelling judged the same company as `winner`, through any
    stored "same" decision (whichever spelling was the winner then)."""
    links = {}
    for (w, n), r in stored.items():
        if r["verdict"] == "same":
            links.setdefault(w, set()).add(n)
            links.setdefault(n, set()).add(w)
    group, todo = {winner}, [winner]
    while todo:
        for n in links.get(todo.pop(), ()):
            if n not in group:
                group.add(n)
                todo.append(n)
    return group


# ----------------------------------------------------------- stored answers
def load_aliases(path=None):
    """{(winner, name): row}; an auditor's row beats the agent's."""
    path = path or ALIASES
    out = {}
    if not os.path.exists(path):
        return out
    with io.open(path, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            key = (r["winner"], r["name"])
            if key in out and out[key]["decided_by"] == "auditor" \
                    and r["decided_by"] != "auditor":
                continue
            out[key] = r
    return out


def save_aliases(rows, path=None):
    path = path or ALIASES
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALIAS_COLS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


# ------------------------------------------------------------------ prompt
SYSTEM = """You check Canadian government award notices. The same supplier
is often written several ways, and different companies often share an
address. For the WINNER below, decide for each candidate spelling whether it
is the same company.

Same company: legal-form or language variants ("Groupe Bell Horizon" /
"Le Groupe Bell Horizon"), "Canada" added, a typo, an "operating as" name,
a renamed company at the same address doing the same kind of work.
Different: another business at the same address (an office tower, a
plaza), a numbered company you cannot tie to the winner, a joint venture
(never the same as one of its partners), a dealer of the winner's products.
When unsure, say different: a wrong merge invents a pattern.

Reply with ONE JSON object and nothing else:
{"thought": "<one sentence>", "action": "<tool>", "args": {...}}

Tools:
variants  {"name": "..."}   other spellings near this name
awards    {"name": "..."}   what this name won (date | buyer | category | title)
decide    {"same": ["..."], "different": ["..."], "reason": "<one sentence>"}
          names exactly as shown; obvious ones in your first reply
finish    {}                stop; undecided names stay apart
"""

FINAL_SYSTEM = SYSTEM.split("Reply with ONE JSON")[0] + """\
You have finished looking. Decide now from what is shown.

Reply with ONE JSON object and nothing else:
{"same": ["..."], "different": ["..."], "reason": "<one sentence>"}
"""


def _tools():
    """Native tool schemas (llm.tool); same tools as the prompt."""
    from tools import llm
    t = llm.tool
    names = {"type": "array", "items": {"type": "string"}}
    return [
        t("variants", "Other spellings near a name: same postal code, or "
                      "the same first word.",
          {"name": {"type": "string"}}, ["name"]),
        t("awards", "What a name won: date, buyer, category, title.",
          {"name": {"type": "string"}}, ["name"]),
        t("decide", "Same company as the winner or not, names exactly as "
                    "shown, with a one-sentence reason.",
          {"same": names, "different": names,
           "reason": {"type": "string"}}, ["reason"]),
        t("finish", "Stop; undecided names stay apart."),
    ]


class AgentState(TypedDict):
    winner: str
    open_names: list
    same: list
    different: list
    reasons: dict
    history: list
    steps: int
    done: bool
    stop_reason: str


def render(state, index):
    left = MAX_STEPS - state["steps"]
    out = [f"WINNER: {index.describe(state['winner'])}",
           f"Steps left: {left}.", "", "Candidates not decided yet:"]
    out += [f"  {index.describe(n)}" for n in state["open_names"]]
    if state["same"]:
        out += ["", "Decided same: " + "; ".join(state["same"])]
    if state["different"]:
        out += ["Decided different: " + "; ".join(state["different"])]
    if state["history"]:
        out += ["", "What you did so far:"]
        for h in state["history"]:
            out.append(f"- step {h['step']}: {h['action']} "
                       f"{json.dumps(h['args'], ensure_ascii=False)[:160]}")
            out.append("  -> " + h["observation"][:1200]
                       .replace("\n", "\n     "))
    return "\n".join(out)


def make_nodes(index, ask, shown, accepted=()):
    """The graph's two nodes. `shown` = names the tools have shown;
    `accepted` = spellings already stored as the same company."""

    def one_reply(state, final=False):
        reply = agent_loop.ask_model(ask, FINAL_SYSTEM if final else SYSTEM,
                                     render(state, index))
        if "action" not in reply and ("same" in reply
                                      or "different" in reply):
            return "decide", reply                  # the final call
        return agent_loop.parse_action(reply)[:2]

    def decide(state):
        try:
            action, args = one_reply(state)
            if (state["steps"] + 1 >= MAX_STEPS and action != "decide"
                    and state["open_names"]):
                action, args = one_reply(state, final=True)
        except Exception as e:
            return {"done": True,
                    "stop_reason": f"model unavailable: {str(e)[:200]}"}
        return {"history": state["history"] + [{
                    "step": state["steps"] + 1, "action": action,
                    "args": args, "observation": ""}],
                "steps": state["steps"] + 1}

    def act(state):
        h = dict(state["history"][-1])
        action, args = h["action"], h["args"]
        same, different = list(state["same"]), list(state["different"])
        open_names = list(state["open_names"])
        reasons = dict(state["reasons"])
        done = False
        name = clean_name(args.get("name"))
        if action == "variants":
            found = index.variants(name)
            shown.update(found)
            obs = "\n".join(index.describe(n) for n in found) or \
                f"no other spelling near '{name}'"
        elif action == "awards":
            obs = index.awards(name)
        elif action == "decide":
            notes = []
            reason = str(args.get("reason", ""))[:300]
            for verdict, names in (("same", args.get("same")),
                                   ("different", args.get("different"))):
                for n in names if isinstance(names, list) else []:
                    n = clean_name(n)
                    if n not in shown:
                        notes.append(f"'{n}': refused - not a name the "
                                     f"tools showed; copy the name only, "
                                     f"without ' - city (codes)'")
                        continue
                    if n in same or n in different:
                        continue
                    if verdict == "same":
                        why = allowed_same(index, state["winner"], n,
                                           list(accepted) + same)
                        if why:
                            notes.append(f"'{n}': refused as same - {why}")
                            continue
                        same.append(n)
                    else:
                        different.append(n)
                    reasons[n] = reason
                    if n in open_names:
                        open_names.remove(n)
                    notes.append(f"'{n}': {verdict}")
            obs = "\n".join(notes) or "error: give 'same' and/or 'different'"
        elif action == "finish":
            obs, done = "finished", True
        elif not action:
            obs = "error: reply with one JSON object with an 'action'"
        else:
            obs = (f"error: unknown action '{action}'. Use one of: "
                   f"variants, awards, decide, finish")
        h["observation"] = obs
        stop = done or not open_names or state["steps"] >= MAX_STEPS
        reason = ("finished by the model" if done else
                  "every candidate decided" if not open_names else
                  "step budget used" if stop else "")
        return {"history": state["history"][:-1] + [h], "same": same,
                "different": different, "open_names": open_names,
                "reasons": reasons, "done": stop, "stop_reason": reason}

    return decide, act


# ------------------------------------------------------------------- entry
def aliases_for(winner, graph, ask=None, use_graph=True, path=None,
                store=True, only=None):
    """Spellings of `winner` (a normalised name) that are the same company.

    only: a test on a candidate name; the others are not offered (the
    caller keeps to spellings that would change its counts - tokens).

    Returns {"same": [...], "different": [...], "asked": [...],
             "steps", "stop_reason", "engine", "trace"}.
    Stored decisions are reused; only undecided candidates reach the model.
    """
    if "_m3_index" not in graph:          # built once per run
        graph["_m3_index"] = AwardIndex(graph)
    index = graph["_m3_index"]
    stored = load_aliases(path)
    cands = [n for n in index.variants(winner) if only is None or only(n)]
    members = group_of(winner, stored)
    known = {}
    for n in cands:
        if n in members:
            known[n] = {"verdict": "same"}
        else:
            row = next((stored[(m, n)] for m in members
                        if (m, n) in stored), None)
            if row:
                known[n] = row
    same = [n for n, r in known.items() if r["verdict"] == "same"]
    different = [n for n, r in known.items() if r["verdict"] == "different"]
    todo = [n for n in cands if n not in known]
    trace = [f"{len(cands)} candidate spelling(s), {len(known)} already "
             f"decided, {len(todo)} to ask"]
    out = {"same": same, "different": different, "asked": todo, "steps": 0,
           "stop_reason": "nothing to ask" if not todo else "",
           "engine": "none", "trace": trace}
    if not todo:
        return out

    if ask is None:
        from tools import llm
        ask = lambda system, user: llm.ask_json(
            system, user, strong=True, max_tokens=2000, tag="m3_agent",
            tools=None if system == FINAL_SYSTEM else _tools())
    shown = set(cands)
    state = AgentState(winner=winner, open_names=todo, same=[],
                       different=[], reasons={}, history=[], steps=0,
                       done=False, stop_reason="")
    decide, act = make_nodes(index, ask, shown, accepted=same)
    state, engine = agent_loop.run(state, decide, act, AgentState, MAX_STEPS,
                                   use_graph)

    today = time.strftime("%Y-%m-%d")
    rows = [{"winner": winner, "name": n, "verdict": v,
             "reason": state["reasons"].get(n, ""), "decided_by": "agent",
             "date": today}
            for v in ("same", "different") for n in state[v]]
    if store and rows:
        save_aliases(rows, path)
    trace += [f"step {h['step']}: {h['action']} "
              f"{json.dumps(h['args'], ensure_ascii=False)[:120]} -> "
              f"{h['observation'].replace(chr(10), ' | ')[:400]}"
              for h in state["history"]]
    out.update(same=same + state["same"],
               different=different + state["different"],
               steps=state["steps"], stop_reason=state["stop_reason"],
               engine=engine, trace=trace)
    return out
