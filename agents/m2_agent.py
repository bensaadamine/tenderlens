"""
M2 AGENT - classifies the brand-like names the rules found in ONE tender.

The rules (tools/spec_tools.py) are good at FINDING names next to a model
code, and bad at JUDGING them: on 4110, 9 of 11 rule findings were false
("Net 30", "Hastelloy C-276", microscope holders the buyer already owns).
Judging needs context - the paragraph, the Q&A, the general clauses - and
the right place to look differs from case to case. So this agent gets a goal
and tools, and decides itself what to read next:

    GOAL   give every candidate the auditor's letter, with a quote:
             a  not a brand (own acronym, ordinary word, standard, URL, date,
                payment term, material grade)
             b  a bidder is speaking (inside "Question N" ... "Answer N")
             c  the buyer's own equipment - the purchase must fit, carry,
                repair or connect to it
             d  one of several brands listed as alternatives / compatibility
             e  the tender accepts an equivalent for it
             f  the brand is REQUIRED and no equivalent is accepted
    TOOLS  search, read, equivalents, wikidata, classify, finish
    LOOP   decide -> act -> decide ... until every candidate has a letter,
           the model says finish, or MAX_STEPS is reached.

Only "f" becomes a finding. What stays in CODE, never left to the model:
  * every verdict must quote text that is really in the document it names
    (the nearest occurrence to the line it gives is recorded); an "f" quote
    must contain the brand. A verdict that fails is refused and the model is
    told why.
  * a candidate left without a valid letter is "unresolved": neither a
    finding nor a clearance.

The loop is a LangGraph graph (decide -> act -> back). If langgraph is not
installed, the same two functions run in a plain loop - same result.

    from m2_agent import classify_tender
    out = classify_tender(ref, candidates)        # candidates from the rules
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

import json
import re
from typing import TypedDict

import spec_tools

try:
    from langgraph.graph import END, StateGraph
except ImportError:          # same loop, run by hand (see run_loop)
    StateGraph = None

LETTERS = {
    "a": "not a brand",
    "b": "a bidder is speaking",
    "c": "the buyer's own equipment",
    "d": "one of several brands listed",
    "e": "an equivalent is accepted",
    "f": "brand required, no equivalent",
}
MAX_STEPS = 8          # model calls per tender
READ_RADIUS = 6        # lines either side for the read tool
SEARCH_HITS = 8        # hits returned by the search tool
QUOTE_WINDOW = 3       # look for the quote here first, then in the document
OBS_CHARS = 1500       # longest observation kept in the prompt
HISTORY_KEPT = MAX_STEPS   # steps shown to the model: all of them (was 6;
                           # 2026-10-22 the model forgot and searched again)


# ------------------------------------------------------------ the tender text
class TenderText:
    """The tender's documents, line by line, with bidder questions marked."""

    def __init__(self, reference, text_dir=None):
        self.docs = {}
        self.bidder = {}
        self.pages = {}
        for d in spec_tools.tender_documents(reference, text_dir):
            lines = d["text"].splitlines()
            self.docs[d["document"]] = lines
            # page of each line, the way spec_tools counts it (form feeds)
            page, pages = 1, []
            for raw in d["text"].splitlines(keepends=True):
                pages.append(page)
                page += raw.count("\f")
            self.pages[d["document"]] = pages
            self.bidder[d["document"]] = spec_tools.question_mask(lines)

    def page_of(self, document, line):
        pages = self.pages.get(document) or []
        return pages[line - 1] if 0 < line <= len(pages) else None

    def _tag(self, doc, i):
        return " [bidder question]" if self.bidder[doc][i] else ""

    def search(self, text):
        needle = (text or "").strip().lower()
        if len(needle) < 2:
            return "error: give at least 2 characters to search"
        hits = []
        for doc, lines in self.docs.items():
            for i, line in enumerate(lines):
                if needle in line.lower():
                    hits.append(f"{doc} line {i + 1}{self._tag(doc, i)}: "
                                f"{_squash(line)[:200]}")
        if not hits:
            return f"no line contains '{text}'"
        more = len(hits) - SEARCH_HITS
        return "\n".join(hits[:SEARCH_HITS]) + (
            f"\n... and {more} more" if more > 0 else "")

    def read(self, document, line):
        lines = self.docs.get(document)
        if lines is None:
            return (f"error: no document '{document}'. Documents: "
                    + ", ".join(self.docs))
        try:
            n = int(line)
        except (TypeError, ValueError):
            return "error: 'line' must be a number"
        lo, hi = max(1, n - READ_RADIUS), min(len(lines), n + READ_RADIUS)
        return "\n".join(f"{i}{self._tag(document, i - 1)}: "
                         f"{_squash(lines[i - 1])[:220]}"
                         for i in range(lo, hi + 1))

    def equivalents(self):
        pats = (spec_tools.RE_OR_EQUIV, spec_tools.RE_DOC_CLAUSE,
                spec_tools.RE_EQUIV_CLAUSE, spec_tools.RE_EQUIV_TO)
        hits = []
        for doc, lines in self.docs.items():
            for i, line in enumerate(lines):
                if any(p.search(line) for p in pats):
                    hits.append(f"{doc} line {i + 1}: {_squash(line)[:200]}")
        if not hits:
            return "no sentence in this tender accepts an equivalent"
        return "\n".join(hits[:12]) + (
            f"\n... and {len(hits) - 12} more" if len(hits) > 12 else "")

    def quote_found(self, document, line, quote):
        """Line number where the quote really is, near `line`, else None."""
        lines = self.docs.get(document)
        # "first words ... last words": every piece must be there, in order
        parts = [_norm(p) for p in re.split(r"\.\.\.+|\u2026", quote or "")]
        parts = [p for p in parts if p]
        q = parts[0] if parts else ""
        if not lines or len(q) < 4:
            return None
        try:
            n = int(line)
        except (TypeError, ValueError):
            n = None
        spans = ([range(max(0, n - 1 - QUOTE_WINDOW),
                        min(len(lines), n + QUOTE_WINDOW))]
                 if n else []) + [range(len(lines))]
        for span in spans:
            idx = [i for i in span if _norm(lines[i])]
            if not _in_order(parts, _norm(" ".join(lines[i] for i in idx))):
                continue
            for i in idx:                 # a line holding the whole quote
                if q in _norm(lines[i]):
                    return i + 1
            for i in idx:                 # else the first line it covers
                if _norm(lines[i]) in q:
                    return i + 1
            return idx[0] + 1
        return None


def _squash(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _in_order(parts, text):
    """Every piece found in `text`, each after the one before."""
    at = 0
    for p in parts:
        at = text.find(p, at)
        if at < 0:
            return False
        at += len(p)
    return True


# ----------------------------------------------------------------- the state
class AgentState(TypedDict):
    reference: str
    candidates: list        # [{id, name, document, line_no, evidence}]
    open_ids: list          # candidates still without a letter
    verdicts: dict          # id -> {letter, quote, document, line}
    history: list           # [{step, action, args, observation}]
    steps: int
    done: bool
    stop_reason: str


SYSTEM = """You check public tenders for brand lock-in. Canadian trade
agreements allow a tender to name a brand only if it also accepts an
equivalent product. Rules found brand-like names in one tender; give each
one exactly one letter:

a  not a brand: the tender's own acronym, an ordinary word, a standard
   (ANSI, CSA, ISO, SAE...), part of a web address, a date, a payment term
   ("Net 30"), a material grade ("Hastelloy C-276"), a unit or abbreviation
b  a bidder is speaking: the text is inside a bidder's question
   (lines marked [bidder question])
c  the buyer's own equipment: the purchase must fit, carry, transport,
   repair, upgrade or connect to something the buyer already has
d  one of several brands listed as alternatives or for compatibility
e  the tender accepts an equivalent for it: "or equivalent" near it, above
   its table, or a general clause ("Products that are equivalent in form,
   fit, function and quality ... will be considered")
f  the tender REQUIRES this brand and accepts no equivalent

Check before you answer: read around the line, look at the equivalence
sentences of the tender, search other mentions. Give "f" only when you have
checked that no equivalent is accepted and it is not the buyer's equipment.

Steps are few. Letter the obvious candidates in your FIRST reply (several at
once), then investigate only the uncertain ones. On your last step you must
classify: anything without a letter stays unresolved.

Every verdict needs a quote copied exactly from the tender (a few words of
the line that justifies the letter) and the document and line it is on.

Reply with ONE JSON object and nothing else:
{"thought": "<one sentence>", "action": "<tool>", "args": {...}}

Tools:
search       {"text": "..."}                 lines containing the text
read         {"document": "...", "line": N}  the lines around line N
equivalents  {}                              every sentence accepting an equivalent
wikidata     {"name": "..."}                 is this name a company on Wikidata
classify     {"verdicts": [{"id": N, "letter": "a".."f", "quote": "...",
                            "document": "...", "line": N}]}
             several candidates at once is fine; obvious ones first
finish       {}                              stop; unlettered ones stay unresolved
"""


def render(state):
    """The prompt for one decision: what is open and what was seen so far."""
    by_id = {c["id"]: c for c in state["candidates"]}
    left = MAX_STEPS - state["steps"]
    out = [f"Tender {state['reference']}. Steps left: {left}."]
    if left == 1:
        # Measured 2026-10-22: on Bendix the model spent all 8 steps reading
        # and never gave a letter. The last step is for deciding.
        out.append("LAST STEP: reply with 'classify' for every candidate "
                   "below, using what you have already read.")
    out += ["", "Candidates without a letter:"]
    for i in state["open_ids"]:
        c = by_id[i]
        out.append(f"  id {i}: \"{c['name']}\" - {c['document']} line "
                   f"{c['line_no']}: {_squash(c['evidence'])[:200]}")
    done = [f"  id {i} \"{by_id[int(i)]['name']}\": {v['letter']}"
            for i, v in state["verdicts"].items()]
    if done:
        out += ["", "Already lettered:"] + done
    hist = state["history"][-HISTORY_KEPT:]
    if hist:
        out += ["", "What you did so far (most recent last):"]
        for h in hist:
            out.append(f"- step {h['step']}: {h['action']} "
                       f"{json.dumps(h['args'], ensure_ascii=False)[:200]}")
            out.append("  -> " + h["observation"][:OBS_CHARS]
                       .replace("\n", "\n     "))
    return "\n".join(out)


# The last call has no tool menu at all. Measured 2026-10-22: with the menu,
# gpt-oss kept searching even when told the step was its last.
FINAL_SYSTEM = SYSTEM.split("Check before you answer:")[0] + """You have finished investigating. Decide now from the lines already shown.
Give "f" only if nothing shown accepts an equivalent for it and it is not the
buyer's equipment.
Every verdict needs a quote copied exactly from those lines and the document
and line it is on. A candidate you cannot decide: leave it out.

Reply with ONE JSON object and nothing else:
{"verdicts": [{"id": N, "letter": "a".."f", "quote": "...",
               "document": "...", "line": N}]}
"""


# ----------------------------------------------------------- decide and act
def make_nodes(text, ask, online=False):
    """The graph's two nodes, bound to one tender's text and one model."""

    def one_reply(state, final=False):
        """One model call -> (action, args, thought). Raises if no model."""
        try:
            reply = ask(FINAL_SYSTEM if final else SYSTEM, render(state))
        except ValueError:            # not JSON: a wasted step, told in act
            reply = {}
        except RuntimeError as e:     # an empty answer is a wasted step too;
            if type(e).__name__ != "EmptyAnswer":   # anything else: outage
                raise
            reply = {}
        if not isinstance(reply, dict):
            reply = {}
        if "action" not in reply and "verdicts" in reply:  # the final call
            reply = {"action": "classify",
                     "args": {"verdicts": reply["verdicts"]}}
        if "action" not in reply and "name" in reply:   # a native tool call
            reply = {"action": reply.get("name"),
                     "args": reply.get("arguments") or {}}
        # gpt-oss prefixes the tools it was trained on: "repo_browser.search"
        action = str(reply.get("action", "")).strip().lower().split(".")[-1]
        args = reply.get("args") if isinstance(reply.get("args"), dict) else {}
        return action, args, str(reply.get("thought", ""))[:300]

    def decide(state):
        try:
            action, args, thought = one_reply(state)
            if (state["steps"] + 1 >= MAX_STEPS and action != "classify"
                    and state["open_ids"]):
                # Measured 2026-10-22: the model ignored "LAST STEP" and
                # searched again. One more call, asking only for letters.
                action, args, thought = one_reply(state, final=True)
        except Exception as e:        # no provider, every provider failed
            return {"done": True, "stop_reason": f"model unavailable: "
                                                 f"{str(e)[:200]}"}
        return {"history": state["history"] + [{
                    "step": state["steps"] + 1, "action": action,
                    "args": args, "thought": thought, "observation": ""}],
                "steps": state["steps"] + 1}

    def act(state):
        h = dict(state["history"][-1])
        action, args = h["action"], h["args"]
        verdicts = dict(state["verdicts"])
        open_ids = list(state["open_ids"])
        done = False
        same = [p["step"] for p in state["history"][:-1]
                if p["action"] == action and p["args"] == args
                and action in ("search", "read", "equivalents", "wikidata")]
        if same:
            obs = (f"already done at step {same[0]} - same result as there. "
                   f"Do not repeat: classify with what you know.")
        elif action == "search":
            obs = text.search(args.get("text") or args.get("query"))
        elif action == "read":
            obs = text.read(args.get("document") or args.get("path"),
                            args.get("line"))
        elif action == "equivalents":
            obs = text.equivalents()
        elif action == "wikidata":
            name = str(args.get("name") or "")
            v = spec_tools.verify_brand(name, offline=not online)
            obs = {True: "a company on Wikidata", False: "not a company on "
                   "Wikidata", None: "unknown to Wikidata"}[v]
        elif action == "classify":
            obs, verdicts, open_ids = _classify(
                text, state["candidates"], args.get("verdicts"),
                verdicts, open_ids, h["step"])
        elif action == "finish":
            obs, done = "finished", True
        elif not action:
            obs = ("error: your reply was not one JSON object with an "
                   "'action' - reply with JSON only")
        else:
            obs = (f"error: unknown action '{action}'. Use one of: search, "
                   f"read, equivalents, wikidata, classify, finish")
        h["observation"] = obs
        stop = done or not open_ids or state["steps"] >= MAX_STEPS
        reason = ("finished by the model" if done else
                  "every candidate lettered" if not open_ids else
                  "step budget used" if stop else "")
        return {"history": state["history"][:-1] + [h],
                "verdicts": verdicts, "open_ids": open_ids,
                "done": stop, "stop_reason": reason}

    return decide, act


def _classify(text, candidates, items, verdicts, open_ids, step):
    """Validate the model's verdicts; keep the good ones, explain the rest."""
    by_id = {c["id"]: c for c in candidates}
    if not isinstance(items, list) or not items:
        return ("error: 'verdicts' must be a non-empty list", verdicts,
                open_ids)
    notes = []
    for v in items:
        if not isinstance(v, dict):
            notes.append("refused: a verdict must be an object")
            continue
        try:
            cid = int(v.get("id"))
        except (TypeError, ValueError):
            notes.append(f"refused: bad id {v.get('id')!r}")
            continue
        letter = str(v.get("letter", "")).strip().lower()
        quote = str(v.get("quote", "")).strip()
        doc = str(v.get("document", "")).strip()
        if cid not in open_ids:
            notes.append(f"id {cid}: refused - not an open candidate")
            continue
        if letter not in LETTERS:
            notes.append(f"id {cid}: refused - letter must be a to f")
            continue
        line = text.quote_found(doc, v.get("line"), quote)
        if line is None:
            notes.append(f"id {cid}: refused - the quote is not in "
                         f"'{doc}' near line {v.get('line')}; copy it exactly")
            continue
        name = by_id[cid]["name"]
        if letter == "f" and _norm(name).split()[0] not in _norm(quote):
            notes.append(f"id {cid}: refused - an 'f' quote must contain "
                         f"'{name}'")
            continue
        verdicts[str(cid)] = {"letter": letter, "quote": quote,
                              "document": doc, "line": line, "step": step}
        open_ids = [i for i in open_ids if i != cid]
        notes.append(f"id {cid} \"{name}\": {letter} accepted")
    return "\n".join(notes), verdicts, open_ids


# ------------------------------------------------------------------ the loop
def _route(state):
    return "end" if state["done"] else "decide"


def build_graph(decide, act):
    g = StateGraph(AgentState)
    g.add_node("decide", decide)
    g.add_node("act", act)
    g.set_entry_point("decide")
    g.add_conditional_edges("decide", lambda s: "end" if s["done"] else "act",
                            {"act": "act", "end": END})
    g.add_conditional_edges("act", _route, {"decide": "decide", "end": END})
    return g.compile()


def run_loop(state, decide, act):
    """The same loop without langgraph."""
    while not state["done"]:
        state = {**state, **decide(state)}
        if state["done"]:
            break
        state = {**state, **act(state)}
    return state


def classify_tender(reference, candidates, ask=None, online=False,
                    text_dir=None, use_graph=True):
    """Letter every candidate of one tender.

    candidates: [{"name", "document", "line_no", "evidence"}] from the rules.
    ask: (system, user) -> dict; defaults to the shared LLM (strong model).
    Returns {"verdicts": [...], "unresolved": [...], "steps", "stop_reason",
             "trace"}.
    """
    if ask is None:
        import llm
        ask = lambda system, user: llm.ask_json(
            system, user, strong=True, max_tokens=3000, tag="m2_agent")
    cands = [dict(c, id=i + 1) for i, c in enumerate(candidates)]
    if not cands:
        return {"verdicts": [], "unresolved": [], "steps": 0,
                "stop_reason": "no candidates", "engine": "none", "trace": []}
    state = AgentState(reference=reference, candidates=cands,
                       open_ids=[c["id"] for c in cands], verdicts={},
                       history=[], steps=0, done=False, stop_reason="")
    text = TenderText(reference, text_dir)
    decide, act = make_nodes(text, ask, online)
    if use_graph and StateGraph is not None:
        state = build_graph(decide, act).invoke(
            state, {"recursion_limit": 4 * MAX_STEPS + 4})
    else:
        state = run_loop(state, decide, act)

    by_id = {c["id"]: c for c in cands}
    verdicts = [dict(by_id[int(i)], **v) for i, v in state["verdicts"].items()]
    for v in verdicts:
        v["page"] = text.page_of(v["document"], v["line"])
    trace = [f"step {h['step']}: {h['action']} "
             f"{json.dumps(h['args'], ensure_ascii=False)[:120]} -> "
             f"{h['observation'].splitlines()[0][:120] if h['observation'] else ''}"
             for h in state["history"]]
    return {"verdicts": verdicts,
            "unresolved": [by_id[i] for i in state["open_ids"]],
            "steps": state["steps"], "stop_reason": state["stop_reason"],
            "engine": "langgraph" if (use_graph and StateGraph) else "loop",
            "trace": trace}
