# Rules assistant

**Code:** `agents/rules_agent.py` (the assistant) · `tools/local_llm.py` (the local models) · `tools/law_index.py` (the search) · `tests/law_eval.py` (its measurement) · `tools/laws.py` (the rules the system checks) · `prepare/fetch_laws.py` (the official texts) · tests `tests/test_rules_agent.py`

## Role
Answers the auditor's questions about the procurement rules ("Can a buyer split a purchase?", "When may a contract be given without bids?"), **only from the official texts**, with quotes the code checks. It explains what the texts say; it never gives legal advice.

## The texts
`python tenderlens.py prepare laws` (Windows, once) downloads 9 official pages and keeps their text in `reports/laws/` (about 117,000 words, 917 passages): Government Contracts Regulations · Treasury Board Directive on the Management of Procurement · Canadian Free Trade Agreement ch. 5 · Supply Manual ch. 3 and its annexes · CUSMA ch. 13 · CETA ch. 19 · WTO Agreement on Government Procurement · the CITT procurement inquiries guide. The 9 rules of the rules page (`tools/laws.py`) are always in the index too.

## The models: local, through Ollama
Nothing leaves the computer, no quota.
```
ollama pull gemma4:e4b        # answers
ollama pull embeddinggemma    # embeddings for the search
```
`TENDERLENS_LOCAL_MODEL`, `TENDERLENS_EMBED_MODEL` and `OLLAMA_URL` can be set in `.env`. Answers are cached in `reports/local_llm_cache.json`, embeddings in `reports/laws/embeddings.json` (each passage is embedded once), so Replay needs no model.

## How it works (three steps the code drives)
A small model is unreliable in a free tool loop, so the code drives the steps (retrieval-augmented generation):
1. **Plan:** Gemma rewrites the question as search words in the texts' own vocabulary ("split a purchase" → "divide procurement requirements valuation").
2. **Search (hybrid):** each text is cut into passages of a few paragraphs under one heading, so a passage keeps its "Article 509". Two rankings:
   - **keywords** (BM25, light stem, a short list of the auditor's words → the texts' words): exact terms, article numbers, "B3000T";
   - **meaning** (cosine between EmbeddingGemma vectors): a passage worded unlike the question.

   **Hybrid is the default** (user decision): the two rankings fused by **Reciprocal Rank Fusion** (each passage scores 1/(60 + rank) in each ranking, summed: ranks are fused, never the two scales). Keywords alone are the fallback when the embedding model is missing (said in the trace). The search runs on the question and on the planned words; the 6 best passages go on, each saying how it was found (keywords, meaning, or both).
3. **Answer:** Gemma answers from those passages only, `{"answer", "citations": [{"id", "quote"}]}` or `{"not_found"}`.

## How the search is measured
`python tenderlens.py eval laws`: 15 questions in an auditor's words (`tests/law_questions.csv`), each with what a relevant passage is (the texts' own headings: "Valuation", "Time Periods", "3.15.5 Advance Contract Award Notice"…, and/or a phrase), written before any search was run; 1 to 24 relevant passages per question out of 917. Reported per mode (keywords, meaning, hybrid): hit@6 (a relevant passage among the 6 Gemma is given) and MRR.

| mode | hit@6 | MRR |
|---|---|---|
| keywords | 8 of 15 | 0.25 |
| meaning | 14 of 15 | 0.70 |
| **hybrid** (default) | **13 of 15** | **0.53** |

Measured 2026-11-25 (EmbeddingGemma, 926 passages embedded in 84 s). Keywords fail where the question is worded unlike the law; meaning fixes that. Hybrid keeps both: exact terms (article numbers, "B3000T") from keywords, paraphrases from meaning; on one question the keywords pulled the right article down ("cut one purchase into smaller contracts": meaning 2nd, hybrid 13th). The one question every mode misses: "Can the buyer talk with only one bidder before the deadline?" (equal treatment of suppliers).

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
- Rules mode (no model) searches by keywords only.
- Amounts in the trade agreements are adjusted over time; the official page is the reference.
- English texts only.
