"""
M2 AGENT - classifies the brand-like names the rules found in ONE tender.

The rules (tools/brands.py) are good at FINDING names next to a model
code, and bad at JUDGING them: on 4110, 9 of 11 rule findings were false
("Net 30", "Hastelloy C-276", microscope holders the buyer already owns).
Judging needs context - the paragraph, the Q&A, the general clauses - and
the right place to look differs from case to case. So this agent gets a goal
and tools, and decides itself what to read next:

    GOAL   give every candidate the auditor's letter, with a quote:
             a  not a brand (own acronym, ordinary word, standard, URL, date,
                payment term, material grade)
             b  a bidder is speaking (inside "Question N" ... "Answer N")
             c  the buyer's own equipment - the bidder does not supply it;
                the purchase must fit, carry, repair, connect to, be
                controlled by or be based on it
             d  one of several brands listed as alternatives / compatibility
             e  the tender accepts an equivalent for it
             f  the bidder must SUPPLY this brand and no equivalent is
                accepted
    SHOWN  before the first step, by code: what the tender buys (its title
           and documents) and every sentence about equipment the buyer
           already has - measured 2026-11-04, the agent called "an existing
           wind turbine generator previously procured by NRC" a lock-in
           because it never looked for that sentence.
    TOOLS  search, read, equivalents, wikidata, classify, finish - called
           through the provider's native tool calling (llm.tool schemas); a
           plain JSON reply still works.
    LOOP   decide -> act -> decide ... until every candidate has a letter,
           the model says finish, or MAX_STEPS is reached.

Only "f" becomes a finding. What stays in CODE, never left to the model:
  * every verdict must quote text that is really in the document it names
    (the nearest occurrence to the line it gives is recorded); an "f" quote
    must contain the brand. A verdict that fails is refused and the model is
    told why.
  * a candidate left without a valid letter is "unresolved": neither a
    finding nor a clearance.

The loop (decide -> act -> back) is agents/agent_loop.py, shared by the
three agents.

    from agents.m2_agent import classify_tender
    out = classify_tender(ref, candidates)        # candidates from the rules
"""


import json
import re
from typing import TypedDict

from tools import brands
from tools import documents
from tools import wikidata

from agents import agent_loop

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
HISTORY_KEPT = MAX_STEPS   # steps shown to the model: all of them (shown
                           # fewer, it forgot what it read and searched again)


# ------------------------------------------------------------ the tender text
class TenderText:
    """The tender's documents, line by line, with bidder questions marked."""

    def __init__(self, reference, text_dir=None):
        self.docs = {}
        self.bidder = {}
        self.pages = {}
        for d in documents.tender_documents(reference, text_dir):
            lines = d["text"].splitlines()
            self.docs[d["document"]] = lines
            # page of each line, the way brands.py counts it (form feeds)
            page, pages = 1, []
            for raw in d["text"].splitlines(keepends=True):
                pages.append(page)
                page += raw.count("\f")
            self.pages[d["document"]] = pages
            self.bidder[d["document"]] = documents.question_mask(lines)

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

    def owned(self, limit=8):
        """Sentences saying the buyer already has, uses or provides
        something - anywhere in the tender, bidders' questions left out.
        Each line is shown with the next one: pdfminer cuts sentences."""
        hits = []
        for doc, lines in self.docs.items():
            skip = -1
            for i, line in enumerate(lines):
                if i == skip or self.bidder[doc][i] or \
                        not RE_OWNED.search(line):
                    continue
                nxt = ""
                if i + 1 < len(lines) and not self.bidder[doc][i + 1]:
                    nxt, skip = lines[i + 1], i + 1
                hits.append(f"{doc} line {i + 1}: "
                            f"{_squash(line + ' ' + nxt)[:220]}")
        if not hits:
            return "(none found)"
        more = len(hits) - limit
        return "\n".join(hits[:limit]) + (
            f"\n... and {more} more (search 'existing')" if more > 0 else "")

    def equivalents(self):
        pats = (brands.RE_OR_EQUIV, brands.RE_DOC_CLAUSE,
                brands.RE_EQUIV_CLAUSE, brands.RE_EQUIV_TO)
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


RE_OWNED = re.compile(
    r"\b(?:existing|currently\s+(?:own\w*|use[sd]?|in\s+use|installed|in\s+"
    r"service)|already\s+(?:own\w*|ha[sv]e?|installed|in\s+place)|owned\s+by|"
    r"(?:previously\s+)?procured\s+by|in\s+our\s+possession|our\s+current|"
    r"government[\s-]+furnished|compatible\s+with\s+(?:our|the\s+existing)|"
    r"existant\w*|actuellement)\b", re.I)


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
    context: str            # what the tender buys + buyer's equipment
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
c  the buyer's own equipment: the bidder does NOT supply this product -
   the purchase must fit, carry, transport, repair, upgrade, connect to,
   be controlled by or be based on something the buyer already has or uses
d  one of several brands listed as alternatives or for compatibility
e  the tender accepts an equivalent for it: "or equivalent" near it, above
   its table, or a general clause ("Products that are equivalent in form,
   fit, function and quality ... will be considered")
f  the bidder must SUPPLY this brand's product and no equivalent is
   accepted

Check before you answer: is this product what the tender BUYS (shown
below), or something the purchase must work with? Read the sentences about
equipment the buyer already has (shown below), read around the line, look at
the equivalence sentences, search other mentions. Give "f" only when the
bidder must supply that brand's product and no equivalent is accepted.

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
    if state.get("context"):
        out += ["", state["context"]]
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


def _tools():
    from tools import llm
    t = llm.tool
    out = [
        t("search", "Lines of the tender containing a text.",
          {"text": {"type": "string"}}, ["text"]),
        t("read", "The lines around one line of one document.",
          {"document": {"type": "string"}, "line": {"type": "integer"}},
          ["document", "line"]),
        t("equivalents", "Every sentence of the tender accepting an "
                         "equivalent product."),
        t("wikidata", "Is this name a company on Wikidata.",
          {"name": {"type": "string"}}, ["name"]),
        t("classify", "Give candidates their letter a-f, each with a quote "
                      "copied exactly from the tender and its document and "
                      "line. Several at once is fine.",
          {"verdicts": {"type": "array", "items": {
              "type": "object",
              "properties": {"id": {"type": "integer"},
                             "letter": {"type": "string",
                                        "enum": list("abcdef")},
                             "quote": {"type": "string"},
                             "document": {"type": "string"},
                             "line": {"type": "integer"}},
              "required": ["id", "letter", "quote", "document", "line"]}}},
          ["verdicts"]),
        t("finish", "Stop; candidates without a letter stay unresolved."),
    ]
    return out


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
        reply = agent_loop.ask_model(
            ask, FINAL_SYSTEM if final else SYSTEM, render(state))
        if "action" not in reply and "verdicts" in reply:  # the final call
            reply = {"action": "classify",
                     "args": {"verdicts": reply["verdicts"]}}
        return agent_loop.parse_action(reply)

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
            v = wikidata.verify_brand(name, offline=not online)
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


def context(text, title=""):
    """What the model sees before its first step: what the tender buys
    and the buyer's-equipment sentences. Found by code, free."""
    return ("What this tender buys: " + (title or "(title not given)")
            + "\nIts documents: " + ", ".join(list(text.docs)[:6])
            + "\n\nSentences about equipment the buyer already has or uses:\n"
            + text.owned())


def classify_tender(reference, candidates, ask=None, online=False,
                    text_dir=None, use_graph=True, title=""):
    """Letter every candidate of one tender.

    candidates: [{"name", "document", "line_no", "evidence"}] from the rules.
    title: the notice's title - what the tender buys, shown to the model.
    ask: (system, user) -> dict; defaults to the shared LLM (strong model).
    Returns {"verdicts": [...], "unresolved": [...], "steps", "stop_reason",
             "trace"}.
    """
    if ask is None:
        from tools import llm
        # native tools on investigating steps; the final call has none
        ask = lambda system, user: llm.ask_json(
            system, user, strong=True, max_tokens=3000, tag="m2_agent",
            tools=None if system == FINAL_SYSTEM else _tools())
    cands = [dict(c, id=i + 1) for i, c in enumerate(candidates)]
    if not cands:
        return {"verdicts": [], "unresolved": [], "steps": 0,
                "stop_reason": "no candidates", "engine": "none", "trace": []}
    text = TenderText(reference, text_dir)
    state = AgentState(reference=reference, candidates=cands,
                       open_ids=[c["id"] for c in cands], verdicts={},
                       context=context(text, title), history=[], steps=0,
                       done=False, stop_reason="")
    decide, act = make_nodes(text, ask, online)
    state, engine = agent_loop.run(state, decide, act, AgentState, MAX_STEPS,
                                   use_graph)

    by_id = {c["id"]: c for c in cands}
    verdicts = [dict(by_id[int(i)], **v) for i, v in state["verdicts"].items()]
    for v in verdicts:
        v["page"] = text.page_of(v["document"], v["line"])
    trace = [f"step {h['step']}: {h['action']} "
             f"{json.dumps(h['args'], ensure_ascii=False)[:120]} -> "
             f"{h['observation'].replace(chr(10), ' | ')[:400]}"
             for h in state["history"]]
    return {"verdicts": verdicts,
            "unresolved": [by_id[i] for i in state["open_ids"]],
            "steps": state["steps"], "stop_reason": state["stop_reason"],
            "engine": engine,
            "trace": trace}
