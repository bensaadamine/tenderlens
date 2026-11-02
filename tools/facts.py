"""
FACTS - short, quoted facts about a tender, extracted by the LOCAL model from
the parts of the documents that are not the requirements section.

Why (2026-11-01, measured on the held-out set): the M2 agent's main error was
the buyer's own equipment described away from the brand - "The WTS must
utilize an existing wind turbine generator procured by NRC", pages before
the generator's brand appears. Reading whole tenders on the cloud model to
find such sentences costs tokens the free quota doesn't have. So the local
model (free, no daily limit) reads a few targeted passages once per tender
and returns facts the agent can ask for with its `facts` tool:

    buyer_answer   the buyer answers a bidder about the product (refuses or
                   allows an alternative, confirms the exact model)
    buyer_owns     equipment the buyer already has; the purchase must fit,
                   use, connect to or repair it
    equivalent     an equivalent is accepted for the product
    bidder_mention a bidder, not the buyer, names the product

Passages read: around every mention of a candidate name OUTSIDE the
requirements section, and around sentences that say the buyer already has
something ("existing", "currently in use", "procured by"...). Capped, so a
long tender costs the same as a short one.

What stays in CODE: every fact's quote must be found in the document it
names (documents.quote_in_documents); others are dropped. Facts are stored
in reports/facts/<reference>.json and reused. No local model configured ->
nothing is extracted and the tool says so; nothing is stored.

    from facts import facts_for
    print(facts_for("cb-407-22943643", ["Eocycle", "MitaTeknik"], "Eocycle"))
"""

import io
import json
import os
import re

import documents

FACTS_DIR = os.path.join("reports", "facts")
VERSION = 1
KINDS = ("buyer_answer", "buyer_owns", "equivalent", "bidder_mention")
RADIUS = 5               # lines kept on each side of a hit
MAX_PASSAGES = 10
MAX_CHARS = 7000         # what the local model is shown, at most
# "off" restores M2 without the facts tool.
FACTS = os.environ.get("TENDERLENS_FACTS", "on")
RE_OWNED = re.compile(
    r"\b(?:existing|currently\s+(?:own\w*|use[sd]?|in\s+use|installed|in\s+"
    r"service)|already\s+(?:own\w*|ha[sv]e?|installed|in\s+place)|owned\s+by|"
    r"procured\s+by|in\s+our\s+possession|our\s+current|compatible\s+with\s+"
    r"(?:our|the\s+existing)|existant\w*|actuellement)\b", re.I)

SYSTEM = """You read excerpts of one Canadian public tender. Extract facts
about the product names listed, and about equipment the buyer already owns.

Kinds of fact:
buyer_answer    the buyer answers a bidder about a product: refuses or
                allows an alternative, confirms the exact model required
buyer_owns      the buyer already owns or uses this equipment; the purchase
                must fit, use, connect to, carry or repair it (name "*" if
                the excerpt names no product)
equivalent      an equivalent product is accepted for it
bidder_mention  a bidder (in a question), not the buyer, names the product

Only facts the excerpts state. Copy each quote exactly (at most 25 words)
with the document and line number shown. None found: {"facts": []}

Reply with ONE JSON object and nothing else:
{"facts": [{"name": "...", "kind": "...", "quote": "...", "document": "...",
            "line": N}]}
"""


def _path(reference):
    return os.path.join(FACTS_DIR, re.sub(r"[^\w.-]", "_", reference)
                        + ".json")


def passages(docs, names):
    """[(document, first_line, text with line numbers)] to show the model."""
    keys = [n.split()[0].lower() for n in names if n.strip()]
    found = []                       # (priority, doc, i)
    for d in docs:
        lines = d["text"].split("\n")
        mask, mode = documents.requirement_mask(lines)
        for i, line in enumerate(lines):
            if mode == "section" and mask[i]:
                continue             # the requirements: the agent reads them
            low = line.lower()
            if any(k in low for k in keys):
                found.append((0, d["document"], i))
            elif RE_OWNED.search(line):
                found.append((1, d["document"], i))
    found.sort()
    spans = []                       # merged windows, name hits first
    for _p, doc, i in found:
        lo, hi = max(0, i - RADIUS), i + RADIUS + 1
        for s in spans:
            if s[0] == doc and lo <= s[2] and hi >= s[1]:
                s[1], s[2] = min(s[1], lo), max(s[2], hi)
                break
        else:
            if len(spans) < MAX_PASSAGES:
                spans.append([doc, lo, hi])
    lines_of = {d["document"]: d["text"].split("\n") for d in docs}
    out, used = [], 0
    for doc, lo, hi in spans:
        ls = lines_of[doc][lo:hi]
        text = "\n".join(f"{lo + k + 1}: {l}" for k, l in enumerate(ls)
                         if l.strip())
        if used + len(text) > MAX_CHARS:
            break
        out.append((doc, lo + 1, text))
        used += len(text)
    return out


def extract(reference, names, ask=None, docs=None):
    """Ask the local model; keep the facts whose quote is in the document.
    Returns {"status", "facts", "model"}; raises nothing."""
    docs = docs if docs is not None else documents.tender_documents(reference)
    ps = passages(docs, names)
    if not ps:
        return {"status": "nothing outside the requirements to read",
                "facts": [], "model": None}
    user = ("Product names: " + ", ".join(names) + "\n\n" + "\n\n".join(
        f"=== {doc} (from line {first})\n{text}" for doc, first, text in ps))
    model = None
    if ask is None:
        import llm
        if not llm.available("ollama"):
            return {"status": "no local model configured "
                              "(TENDERLENS_LOCAL_MODEL)", "facts": [],
                    "model": None}
        model = os.environ.get("TENDERLENS_LOCAL_MODEL")
        ask = lambda system, u: llm.ask_json(
            system, u, provider="ollama", max_tokens=1500, tag="facts")
    try:
        reply = ask(SYSTEM, user)
    except Exception as e:           # not JSON, model down: no facts
        return {"status": f"local model failed: {str(e)[:120]}",
                "facts": [], "model": model}
    facts = []
    for f in (reply or {}).get("facts", []) if isinstance(reply, dict) else []:
        if not isinstance(f, dict) or f.get("kind") not in KINDS:
            continue
        quote = str(f.get("quote") or "").strip()
        if documents.quote_in_documents(docs, f.get("document"), quote):
            facts.append({"name": str(f.get("name") or "*").strip() or "*",
                          "kind": f["kind"], "quote": quote,
                          "document": f.get("document"),
                          "line": f.get("line")})
    return {"status": "extracted", "facts": facts, "model": model,
            "passages": len(ps)}


def get(reference, names, ask=None, docs=None):
    """Stored facts if they cover these names, else extracted and stored."""
    path = _path(reference)
    want = sorted({n for n in names if n.strip()})
    if os.path.exists(path):
        try:
            with io.open(path, encoding="utf-8") as f:
                kept = json.load(f)
            if kept.get("version") == VERSION and set(want) <= set(
                    kept.get("names", [])):
                return kept
        except (OSError, ValueError):
            pass
    out = extract(reference, want, ask, docs)
    if out["status"] == "extracted":      # never store "no model" or errors
        out.update(reference=reference, version=VERSION, names=want)
        os.makedirs(FACTS_DIR, exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return out


def facts_for(reference, names, name, ask=None, docs=None):
    """What the agent's `facts` tool shows for one name."""
    got = get(reference, names, ask, docs)
    if got["status"] != "extracted":
        return f"no facts: {got['status']} - use search and read"
    key = (name or "").split()[0].lower() if (name or "").strip() else ""
    mine = [f for f in got["facts"]
            if (key and key in f["name"].lower()) or f["name"] == "*"]
    if not mine:
        return (f"no fact about '{name}' outside the requirements section "
                f"({got.get('passages', 0)} passage(s) read)")
    return "\n".join(f"{f['kind']}: \u201c{f['quote']}\u201d "
                     f"({f['document']} line {f['line']})" for f in mine)
