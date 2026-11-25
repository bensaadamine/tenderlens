"""
The rules assistant: the law index and the assistant's code checks, with a
small made-up corpus and a scripted fake model - no network, no Ollama.

    python -m tests.test_rules_agent
"""

import io
import os
import sys
import tempfile

from agents import rules_agent as ra
from tools import law_index, local_llm

FAILS = 0
TEXT = """TITLE: Made-up Agreement, Chapter 5
URL: https://example.org/ch5
FETCHED: 2026-11-25

## Article 503: General Rules

1. A procuring entity shall not prepare, design, or otherwise structure a procurement, or divide procurement requirements in order to avoid the obligations of this Agreement.

## Article 509: Technical Specifications

1. A procuring entity should avoid the use of technical specifications that require or refer to a particular trademark or trade name. In such cases the entity shall include words such as or equivalent in the tender documentation.
"""


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def script(*replies):
    it = iter(replies)
    return lambda system, user: next(it)


def main():
    tmp = tempfile.mkdtemp()
    with io.open(os.path.join(tmp, "agr.txt"), "w", encoding="utf-8") as f:
        f.write(TEXT)
    idx = law_index.load(tmp)
    ids = [p["id"] for p in idx.passages if p["source"] == "agr"]
    check("the text is cut under its headings",
          [p["section"] for p in idx.passages if p["source"] == "agr"]
          == ["Article 503: General Rules",
              "Article 509: Technical Specifications"])
    check("the rules page is always in the index",
          any(p["source"] == "rules" for p in idx.passages))
    top = idx.search("can a buyer split a purchase", 2, source="agr")
    check("the auditor's word 'split' finds the text's 'divide'",
          top and top[0]["section"].startswith("Article 503"))
    top = idx.search("brand name", 1, source="agr")
    check("'brand' finds 'trademark'", top
          and top[0]["section"].startswith("Article 509"))
    check("read gives a passage and its neighbours in the same text",
          [p["id"] for p in idx.read(ids[0])] == ids)

    plan = {"search": "divide procurement requirements"}
    good = {"answer": "No: dividing a purchase to avoid the rules is barred.",
            "citations": [{"id": ids[0], "quote": "divide procurement "
                           "requirements in order to avoid the obligations"}]}
    r = ra.ask_rules("Can a buyer split a purchase?", idx,
                     ask=script(plan, good))
    check("plan, search, answer: a quote found word for word is accepted",
          r["answer"].startswith("No") and r["citations"][0]["id"] == ids[0]
          and r["search"] == plan["search"] and r["steps"] == 2
          and r["stop_reason"] == "answered")
    check("the planned words reach the search",
          ids[0] in [p["id"] for p in r["passages"]])

    bad = {"answer": "Yes.", "citations": [
        {"id": ids[0], "quote": "a buyer may always split a purchase"}]}
    r = ra.ask_rules("Can a buyer split?", idx, ask=script(plan, bad, good))
    check("an invented quote is refused; the second try, told why, passes",
          r["answer"].startswith("No") and "refused" in r["trace"][2]
          and "not in that passage" in r["trace"][2])

    r = ra.ask_rules("Can a buyer split?", idx, ask=script(
        plan, {"answer": "No.", "citations": [{"id": "rules:99",
                                               "quote": "x y z"}]},
        {"answer": "No.", "citations": []}))
    check("a passage not shown, then no citation: no answer after two tries",
          not r["answer"] and r["stop_reason"] ==
          "no answer with checked quotes"
          and "not one of the passages shown" in r["trace"][2])

    r = ra.ask_rules("What colour is the sky?", idx, ask=script(
        {"search": "sky colour"}, {"not_found": "the texts are silent"}))
    check("not_found is kept", r["not_found"] == "the texts are silent")

    def down(system, user):
        raise local_llm.Unavailable("Ollama not reachable")
    r = ra.ask_rules("Can a buyer split?", idx, ask=down)
    check("no model: no answer, the search's passages are still returned",
          not r["answer"] and r["passages"]
          and "Ollama" in r["stop_reason"])
    r = ra.ask_rules("Can a buyer split?", idx, model=False)
    check("search only: passages, no model call", r["passages"]
          and r["steps"] == 0)

    old = local_llm.CACHE
    local_llm.CACHE = os.path.join(tmp, "cache.json")
    try:
        local_llm.ask_json("s", "u", cache_only=True)
        check("replay without the answer raises Unavailable", False)
    except local_llm.Unavailable:
        check("replay without the answer raises Unavailable", True)
    local_llm._save({local_llm._key("s", "u"): {"search": "x"}})
    check("replay returns the cached answer",
          local_llm.ask_json("s", "u", cache_only=True) == {"search": "x"})
    local_llm.CACHE = old

    print(f"\n{FAILS} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
