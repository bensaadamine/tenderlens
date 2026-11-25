"""
THE RULES ASSISTANT - answers an auditor's question about the procurement
rules, only from the official texts, with checked quotes. It runs on a
LOCAL model (Gemma 4B through Ollama, tools/local_llm.py): no quota, nothing
leaves the computer.

A small model is unreliable in a free tool loop, so the code drives three
steps (retrieval-augmented generation):
    1. PLAN    the model rewrites the question as search words, in the
               texts' own vocabulary ("split" -> "divide procurement")
    2. SEARCH  the code searches the law texts (tools/law_index.py): hybrid,
               keywords (BM25) and meaning (embeddings) fused (keywords only
               if the embedding model is missing), with the question and
               with those words; the best passages go on
    3. ANSWER  the model answers from those passages only:
               {"answer", "citations": [{"id", "quote"}]} or {"not_found"}

What stays in CODE, never left to the model:
  * every quote must appear, word for word, in the passage it cites, and
    that passage must be one of those shown; otherwise the answer is
    refused and the model gets ONE more try, told why;
  * an answer needs at least one valid citation;
  * no model (Rules mode, Ollama not running, replay without the answer):
    the passages the search finds are returned instead.
It explains what the texts say; it never gives legal advice.

    python tenderlens.py ask "Can a buyer split a purchase?"
    python tenderlens.py ask --search-only "..."
"""

import argparse
import sys

from tools import law_index, names

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SHOWN = 6               # passages given to the model
PASSAGE_CHARS = 1200

PLAN = """You turn an auditor's question about Canadian federal procurement
into search words for the official texts (regulations, trade agreements,
Supply Manual). Use the texts' own vocabulary: e.g. "split a purchase" ->
"divide procurement requirements valuation"; "brand" -> "trademark trade
name technical specifications"; "sole source" -> "limited tendering without
soliciting bids". Reply with JSON only: {"search": "<8-15 words>"}"""

ANSWER = """You help a Canadian government auditor understand federal
procurement rules. Answer ONLY from the PASSAGES below. Explain what they
say, in plain words, in 2-5 sentences; never give legal advice; never cite
an article the passages do not show. Every point needs a quote: exact
words copied from one passage (5-25 words), with that passage's id.
If the passages do not answer the question, say so.

Reply with JSON only, either
{"answer": "...", "citations": [{"id": "<passage id>", "quote": "<exact words>"}]}
or {"not_found": "<one sentence: what is missing>"}"""


def show(p):
    return (f"[{p['id']}] {p['title']}" + (f" - {p['section']}"
                                           if p["section"] else "")
            + f"\n{p['text'][:PASSAGE_CHARS]}")


def quote_in(quote, text):
    """The quote's words appear in the passage, in order, as words."""
    return len(names.words(quote)) >= 3 and names.says(text, quote)


def check_citations(cits, index, shown):
    """(valid citations, notes for the model)."""
    ok, notes = [], []
    for c in cits if isinstance(cits, list) else []:
        if not isinstance(c, dict):
            continue
        pid, q = str(c.get("id", "")).strip().strip("[]"), str(c.get("quote", ""))
        p = index.get(pid)
        if p is None or pid not in shown:
            notes.append(f"'{pid}' is not one of the passages shown")
        elif not quote_in(q, p["text"]):
            notes.append(f"the quote for '{pid}' is not in that passage, "
                         f"word for word")
        else:
            ok.append({"id": pid, "quote": q[:400], "title": p["title"],
                       "section": p["section"], "url": p["url"]})
    return ok, notes


def merge(*lists, k=SHOWN):
    """Passages from several searches, best first, each once."""
    seen, out = set(), []
    for p in sorted((p for l in lists for p in l), key=lambda p: -p["score"]):
        if p["id"] not in seen:
            seen.add(p["id"])
            out.append(p)
    return out[:k]


def ask_rules(question, index=None, ask=None, model=True, cache_only=False,
              embed=None):
    """Answer one question. Returns {"answer", "citations", "not_found",
    "passages", "search", "steps", "stop_reason", "trace", "model"}.
    ask(system, user) -> dict is the model (default: the local one);
    embed(texts, kind, titles) -> vectors makes the search hybrid (default:
    the local embedding model; unavailable -> keywords only);
    model=False: keywords only, no model of any kind (Rules mode)."""
    index = index or law_index.load()
    out = {"answer": "", "citations": [], "not_found": "", "search": "",
           "passages": [], "steps": 0, "trace": [], "model": "",
           "stop_reason": "search only (no model)"}
    if not model:
        out["passages"] = index.search(question, SHOWN)
        return out
    if ask is None or embed is None:
        from tools import local_llm
        if ask is None:
            ask = lambda system, user: local_llm.ask_json(
                system, user, cache_only=cache_only)
            out["model"] = local_llm.model()
        if embed is None:
            embed = lambda *x: local_llm.embed(*x, cache_only=cache_only)

    def find(text):
        try:
            index.attach_vectors(embed)
            qvec = embed([text], "query", None)[0]
        except Exception as e:
            out["trace"].append(f"search: keywords only ({str(e)[:120]})")
            qvec = None
        return index.search(text, SHOWN, qvec=qvec)

    first = find(question)
    out["passages"] = first
    try:
        plan = ask(PLAN, f"QUESTION: {question}")
        words = str(plan.get("search", ""))[:200]
        out.update(search=words, steps=1)
        out["trace"].append(f"plan: search words \u201c{words}\u201d")
        shown = merge(first, find(words) if words else [])
        out["passages"] = shown
        out["trace"].append("search: " + ", ".join(
            f"{p['id']} ({p.get('how', 'keywords')})" for p in shown))
        user = (f"QUESTION: {question}\n\nPASSAGES:\n\n"
                + "\n\n".join(show(p) for p in shown))
        ids = {p["id"] for p in shown}
        for attempt in (1, 2):
            reply = ask(ANSWER, user)
            out["steps"] += 1
            if reply.get("not_found") and not reply.get("answer"):
                out.update(not_found=str(reply["not_found"])[:300],
                           stop_reason="the texts do not say")
                out["trace"].append("answer: not found in the texts")
                return out
            ok, notes = check_citations(reply.get("citations"), index, ids)
            text = str(reply.get("answer", "")).strip()
            if text and ok and not notes:
                out.update(answer=text[:2000], citations=ok,
                           stop_reason="answered")
                out["trace"].append(f"answer: accepted, {len(ok)} quote(s) "
                                    f"checked")
                return out
            why = "; ".join(notes) or "no citation given"
            out["trace"].append(f"answer {attempt}: refused - {why}")
            user += (f"\n\nYOUR PREVIOUS ANSWER WAS REFUSED: {why}. Copy "
                     f"quotes exactly from the passages, or reply not_found.")
        out["stop_reason"] = "no answer with checked quotes"
    except Exception as e:
        out["stop_reason"] = f"model unavailable: {str(e)[:200]}"
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question", nargs="+")
    ap.add_argument("--search-only", action="store_true",
                    help="no model: the passages the search finds")
    a = ap.parse_args()
    import os
    r = ask_rules(" ".join(a.question), model=not a.search_only,
                  cache_only=os.environ.get("TENDERLENS_OFFLINE") == "1")
    if r["answer"]:
        print(r["answer"], "\n")
        for c in r["citations"]:
            print(f"  “{c['quote']}”\n    {c['title']}"
                  f"{' - ' + c['section'] if c['section'] else ''}")
    elif r["not_found"]:
        print("The texts do not answer this:", r["not_found"])
    else:
        print(f"({r['stop_reason']}) Passages that match:\n")
        for p in r["passages"]:
            print(f"[{p['id']}] {p['title']} - {p['section']}\n"
                  f"  {p['text'][:300]}\n")
    for t in r["trace"]:
        print("  " + t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
