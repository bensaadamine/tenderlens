# Rules assistant

**Code:** `agents/rules_agent.py` (the assistant) · `tools/local_llm.py` (the local model) · `tools/law_index.py` (the search) · `tools/laws.py` (the rules the system checks) · `prepare/fetch_laws.py` (the official texts) · tests `tests/test_rules_agent.py`

## Role
Answers the auditor's questions about the procurement rules ("Can a buyer split a purchase?", "When may a contract be given without bids?"), **only from the official texts**, with quotes the code checks. It explains what the texts say; it never gives legal advice.

## The texts
`python tenderlens.py prepare laws` (Windows, once) downloads the official pages and keeps their text in `reports/laws/`:
Government Contracts Regulations · Treasury Board Directive on the Management of Procurement · Canadian Free Trade Agreement ch. 5 · Supply Manual ch. 3 and its annexes · CUSMA ch. 13 · CETA ch. 19 · WTO Agreement on Government Procurement · the Treasury Board guide on ACANs · the CITT procurement inquiries guide.
The rules page itself (`tools/laws.py`) is always in the index, so it works before the download.

## The model: local, through Ollama
Gemma 4B runs on the auditor's computer: no key, no quota, nothing leaves the machine.
```
ollama pull gemma3:4b          # once (Ollama installed and running)
```
`TENDERLENS_LOCAL_MODEL` (default `gemma3:4b`) and `OLLAMA_URL` (default `http://localhost:11434`) can be set in `.env`. Answers are cached in `reports/local_llm_cache.json`, so Replay needs no model.

## How it works (three steps the code drives)
A 4B model is unreliable in a free tool loop, so the code drives the steps (retrieval-augmented generation):
1. **Plan:** the model rewrites the question as search words in the texts' own vocabulary ("split a purchase" → "divide procurement requirements valuation").
2. **Search (no model):** keyword search (BM25, light stem, a short list of the auditor's words → the texts' words) with the question and those words; the 6 best passages go on. A passage is a few paragraphs under one heading, so it keeps its "Article 509".
3. **Answer:** the model answers from those passages only, `{"answer", "citations": [{"id", "quote"}]}` or `{"not_found"}`.

## Kept in code, never left to the model
- A quote must appear **word for word** in the passage it cites, and that passage must be one of those shown; otherwise the answer is refused and the model gets **one** more try, told why.
- An answer needs at least one valid citation.
- No model (Rules mode, Ollama not running, replay without the answer): the passages the search finds are shown instead.

## Where
- The auditor's page, **Rules**: the question box. Mode **Rules**: passages only, no model; **Hybrid**: Gemma, live; **Replay**: Gemma's cached answers only.
- A terminal: `python tenderlens.py ask "Can a buyer split a purchase?"` (`--search-only`, or `--offline ask ...`).

## How it is checked
`python -m tests.test_rules_agent`: the index (headings, synonyms, neighbours) and the code's checks with a scripted fake model: plan → search → answer; an exact quote is accepted; an invented quote gets one retry; a passage not shown or no citation is refused; no model → passages; replay from the cache.

## Known limits
- Keyword search: a question in words the texts never use can miss; the agent rephrases its searches, the search-only mode cannot.
- Amounts in the trade agreements are adjusted over time; the official page is the reference.
- English texts only.
