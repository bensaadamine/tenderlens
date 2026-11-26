# TenderLens — code guide for the technical review

How to present the code to the teacher, in order, split between 4 people. Each part lists the files, the **main functions** (explained) and the **secondary functions** (one line each), what to show live, and the questions the teacher is likely to ask.

**Order:** Person 1 → Person 2 → Person 3 → Person 4. About 10–12 minutes each.

| # | Person presents | Agent | Also covers |
|---|---|---|---|
| 1 | The big picture, then the **Spec auditor** | agent 1 (M2) | the one command, how one tender flows, the shared agent loop |
| 2 | The **Supplier identity** agent | agent 2 (M3) | the award file, how we call the model (Groq), the cache |
| 3 | The **Coordinator** | agent 4 (M4) | the notice rules (M1), the knowledge base, the auditor's page |
| 4 | The **Split-purchase** agent | agent 3 | the rules of law, the rules assistant (RAG), how we measure |

---

## Before you start: five things everyone must know

1. **What it does.** TenderLens reads Canadian federal tenders (notices, tender documents, award notices) and helps an auditor answer one question per contract: *was the competition real?* It clears most purchases with quoted evidence and builds a case file for the few worth a look. It never says "fraud"; a person decides.
2. **Hybrid design.** Rules where exactness matters (what a notice says, whether a quote is really in a document, scoring). Agents where judgement is needed (is this word a brand the bidder must supply? are two spellings the same company?). Every choice between the two was measured.
3. **What "agent" means here.** A model that chooses its own tools, step by step, toward a goal. The **code** checks everything it concludes: a verdict without a real quote is refused.
4. **Every model answer is cached.** `--offline` replays the cache: same result, no network, no quota. This is how we demo.
5. **One command runs everything:** `python tenderlens.py <command>`.

---

## Person 1 — The big picture and the Spec auditor (agent 1)

### What you say in one sentence
"The Spec auditor reads the tender documents and finds a brand the bidder must supply with no 'or equivalent'. That breaks the trade-agreement rule (Canadian Free Trade Agreement, art. 509)."

### Files
`tenderlens.py` · `pipeline/one_tender.py` · `tools/brands.py` · `tools/documents.py` · `agents/m2_spec_auditor.py` · `agents/m2_agent.py` · `agents/agent_loop.py`

### Main functions

**`tenderlens.main()`** — `tenderlens.py`
The single entry point. It reads the first word (`run`, `serve`, `splits`, `ask`, `eval`…), finds the module that handles it and calls that module's `main()`. `--offline` sets one environment variable so every model call is answered from the cache.

**`gate(ref, row, graph)`** — `pipeline/one_tender.py`
Decides **whether a tender is read at all**, cheaply, before any text is opened. A tender is read only if it was **awarded** and we **hold its documents**. Cancelled, still open, advance award notices (ACAN), and tenders with no award found are only recorded. Returns a label and a reason.

**`run_one(ref, shared, settings)`** — `pipeline/one_tender.py`
The chain for one tender, in order:
1. M1 reads the notice.
2. M2 reads the documents (the Spec auditor).
3. M3 reads the award file.
4. M4 builds the case (alert / watch / close).

Returns the case. Key design choice, in its docstring: M1 never decides whether a tender is read, because a rigged specification looks normal on the notice.

**`brands_without_equivalent(reference)`** — `tools/brands.py`
The brand **rules**. For one tender, it:
1. Collects candidate names in the requirements section only. A candidate needs a model code after it ("Fluke 87V"), a ™ or ®, or a sentence naming the maker ("manufactured by X").
2. Asks Wikidata whether the name is a company (cached).
3. Clears a name when the text decides it exactly: "or equivalent", the standard Equivalent Products clause (B3000T), the buyer's own equipment, a list of approved products, a bidder's question.

Returns `findings`, `ok` (cleared, with the reason), `unresolved` (left to the agent) and `dropped`.

**`classify_tender(reference, candidates)`** — `agents/m2_agent.py`
The **agent**. Its goal: give every remaining name one letter from the auditor's grid, with a quote.

| letter | meaning |
|---|---|
| a | not a brand |
| b | a bidder is speaking |
| c | the buyer's own equipment |
| d | one of several brands listed |
| e | an equivalent is accepted |
| f | brand required, no equivalent: **the only letter that becomes a finding** |

Its tools (the model picks one per step): `search`, `read`, `equivalents`, `wikidata`, `classify`, `finish`. Before step 1 the code shows it what the tender buys and every sentence about equipment the buyer already has. Up to 8 steps.

**`TenderText.quote_found(document, line, quote)`** — `agents/m2_agent.py`
**The safety check.** A verdict is accepted only if its quote is really in the document. An "f" quote must also contain the brand. Otherwise the verdict is refused and the model is told why.

**`agent_loop.run(state, decide, act, ...)`** — `agents/agent_loop.py`
The loop all agents share: **decide** (the model picks a tool) → **act** (the code runs it and checks the result) → decide… until done or out of steps. It is a LangGraph graph; without LangGraph installed, the same two functions run in a plain loop.

**`audit(reference, ...)`** — `agents/m2_spec_auditor.py`
Puts the rules and the agent together for one tender and returns the findings, each with its source (document, line, quote) and who settled each name (`decided_by`).

### Secondary functions (just say what they do)
- `documents.tender_documents` — loads a tender's extracted text files.
- `documents.requirement_view` / `amendment_view` — keeps only the requirements section, or only the change clauses of an amendment.
- `documents.question_mask` — marks the lines inside a bidders' "Question / Answer" block.
- `brands._candidates` — the scan that finds brand-like names in one document.
- `brands._text_clearance` — the clearances that need no model.
- `brands.is_measure` — "350W" or "610MM" is a measurement, not a model code.
- `brands._in_approved_list` / `_in_make_list` — a list of several makes is a choice, not a lock-in.
- `m2_spec_auditor.merge_spellings` — "FormLabs Form" and "Formlabs" are one finding.
- `m2_spec_auditor.names_checked` — the table "Brand names checked" on the case page.
- `wikidata.verify_brand` — is this name a company? (cached)
- `agent_loop.parse_action` — reads the model's reply as a tool and its arguments.

### Show live
`python tenderlens.py demo --offline`: 7 tenders, three setups side by side (rules only, rules + re-check, every agent). Point at the Gamry case: the rules miss it, the agent finds it.

### Likely questions
- **"Why not let the model do everything?"** It is good at judging, bad at being exact. The rules find names and settle what the text decides exactly. The agent judges the rest, and code checks every quote.
- **"Isn't `brands.py` tuned to your data?"** We tested it on a family it had never seen (pumps and compressors, 50 tenders, labelled blind). It behaved the same: it caught 1 of 2 real lock-ins, the miss was a name with no model number (a known, accepted gap), and it wrongly cleared nothing.
- **"How good is the agent?"** On 35 names never used for tuning: 34 right. Rules alone found 1 of 8 lock-ins; with the agent, 8 of 8.

---

## Person 2 — The Supplier identity agent (agent 2) and how we call the model

### What you say in one sentence
"The award file writes one company many ways. The Supplier identity agent decides which spellings of the winner are the same company, so we can see whether a buyer keeps choosing the same supplier."

### Files
`agents/m3_investigator.py` · `agents/m3_agent.py` · `tools/names.py` · `tools/llm.py`

### Main functions

**`load_graph(path)`** — `agents/m3_investigator.py`
Reads the award file once and builds in-memory indexes: by buyer and category, by buyer and supplier, by supplier, by postal code. No database is needed.

**`find_award(reference, notice_row, graph)`** — `agents/m3_investigator.py`
Finds the award for a tender, by solicitation number. Buyers reuse their numbers, so a match counts only if the award's title shares a meaningful word with the tender's (`same_purchase`). When several rows match, the newest is the award.

**`investigate(reference, notice_row, graph, agent)`** — `agents/m3_investigator.py`
1. **Depth 1:** who won, and how many of this buyer's awards in this category went to that winner.
2. **Depth 2,** only if the winner got at least half of at least 3 awards: does it win everywhere (a market leader) or live off this buyer (a dependent supplier)?

The result is always **context, never evidence**: it can add to a case, never make one.

**`aliases_for(winner, graph)`** — `agents/m3_agent.py`
The **agent**. For the winner, it decides which other spellings are the same company. Tools: `variants` (names at the same postal code or with the same first word), `awards` (what a name won), `decide`, `finish`. Up to 6 steps.

**`allowed_same(index, winner, name, accepted)`** — `agents/m3_agent.py`
**The safety check.** "Same company" is accepted only if the name shares a postal code with the winner itself (no chains A→B→C), or the names are equal once generic words ("Canada", "Group") are dropped. A joint venture is never the same as one of its partners. Every decision is stored in `reports/supplier_aliases.csv` and never asked again. The auditor can overrule.

**`llm.ask(system, user, tools=...)`** — `tools/llm.py`
How every agent calls the model (Groq, gpt-oss-120b):
1. First, look in the cache (`reports/llm_cache.json`, keyed by model + prompt).
2. Offline: a missing answer raises `NotCached`; it is never paid.
3. Otherwise: send to the Groq key with room (we use 2 keys), wait when the per-minute budget is full, retry on errors, log tokens and errors.

**`llm.tool(name, description, properties)`** — `tools/llm.py`
Describes a tool to the model in the provider's native format ("function calling"), so the model answers with a tool name and arguments instead of free text.

### Secondary functions
- `m3_investigator.norm_name` / `norm_postal` / `norm_sol` — clean names, postal codes, solicitation numbers before comparing.
- `pick_award` — the newest of several award rows.
- `detect_columns` — finds the award file's columns from its header, never assumed.
- `m3_agent.AwardIndex.variants` / `.awards` — the agent's two look-up tools.
- `group_of` — all spellings already judged the same company.
- `load_aliases` / `save_aliases` — the stored decisions; an auditor's row wins.
- `names.words` / `says` / `part_of` — one way to compare names, with no fuzzy matching.
- `llm.json_asker` — an agent's call that returns JSON and counts which model answered.
- `llm.usage_summary` — tokens spent, per agent (`python tenderlens.py llm --usage`).

### Show live
`python tenderlens.py --offline tender <ref>` on a tender with a known winner: the trace shows the award found, the counts, and the agent's spelling decisions.

### Likely questions
- **"Why an agent for names?"** Rebrands and typos have no fixed pattern ("Olympus NDT" → "Evident"), and two companies can share an office tower. A string rule either merges too much or too little.
- **"Why is history only context?"** Many markets have one or two suppliers. Winning often is not wrongdoing.
- **"What if Groq is down or out of quota?"** The run stops cleanly and the next run resumes from that tender. Every verdict comes from the same pinned model.

---

## Person 3 — The Coordinator (agent 4), the knowledge base and the auditor's page

### What you say in one sentence
"The Coordinator gathers what the others found, sends brand findings back to agents 1 and 2 for a second look, checks every quote, and decides alert, watch or close. We tested letting a model choose those checks: it was worse, so the order is fixed."

### Files
`agents/m1_screener.py` · `agents/m4_case_builder.py` · `agents/m4_checks.py` · `pipeline/stream.py` · `interface/app.py` · `interface/data.py` · `interface/jobs.py`

### Main functions

**`screen(reference, notices)`** — `agents/m1_screener.py`
The **notice rules** (M1). It lists what the buyer declared: an advance award notice, a limited-tendering procedure, a short bidding window (compared with tenders run the same way), no trade agreement. Each signal comes with the notice field as evidence. These signals **rank** a case; they never put it in the queue.

**`check_case(reference, m1, m2, m3, hooks)`** — `agents/m4_checks.py`
Runs only when a case has a brand finding, in a **fixed order**:
1. Agent 1 re-reads each finding.
2. Agent 2 checks the winner's spellings.
3. The winner's other award titles are shown.

Then it calls `build_case`. The checks only ask; the code decides.

**`build_case(m1, m2, m3)`** — `agents/m4_case_builder.py`
Builds the case in three steps:
- **`collect_claims`:** every finding becomes a claim, with its source.
- **`score_claims`:** points per claim (a brand: 3, at most 2 brands; brand = winner: 3; declared signals: 2; context: 1). A claim scores only if its words were checked against its source.
- **`decide_route`:** **alert** needs 5 points from document findings alone (two brands, or a brand that won). **Watch** needs a real finding, not only declared signals or context. Otherwise **close**.

The case also gets a narrative in plain words. It never says "fraud".

**`run(args)`** — `pipeline/stream.py`
Runs a family tender by tender, against its **knowledge base** (`reports/kb/<family>/`):
- **Installation:** the oldest 70% of tenders, rules only, no model.
- **New tenders:** everything after that, with the agents.
- A **closed** case joins the base. An **alert** or **watch** goes to quarantine until the auditor says *clean* (back to the base) or *confirmed* (kept as a real case).
- Each case is written to `cases.jsonl` before its register line, so a crash loses nothing.

**`app.py` (FastAPI)** — the auditor's page (`python tenderlens.py serve`). Four places in the top bar:
- **To decide:** every flagged case and split purchase, one list.
- **Families:** one row per family.
- **Rules:** the rules of law and the rules assistant.
- **How it works:** the 4 agents.

A case shows the tender's own words marked as evidence, the rule of law behind each reason, and **Mark clean / Confirm** buttons.

### Secondary functions
- `m1_screener.is_acan` — is this an advance contract award notice?
- `m1_screener.method_windows` — typical bidding windows per procurement method.
- `m4_case_builder.winner_claims` — is the required brand the company that won?
- `m4_case_builder.plain_text` — one plain sentence per claim.
- `narrative_plain` — the case page text.
- `stream.KB` — the knowledge base's files (cases, register, feedback).
- `install_cut` — the 70% cut.
- `todo_list` — tenders not yet processed.
- `apply_verdict` — applies the auditor's clean / confirmed.
- `pipeline/update.py` — the weekly update: download, then run the new tenders.
- `data.tenders` / `summary` / `ranked` — read the knowledge base for the page.
- `jobs.start` — runs a command from the page, as if typed, with its output live.
- `app.evidence` / `rule_links` — the marked quote and the "Rule:" link under each reason.

### Show live
`python tenderlens.py serve`, then:
1. **To decide** → open the Axis camera case: the marked quote, the rule link, who decided.
2. Fold open **How it was reached** to see the agent's steps.
3. Show **Mark clean / Confirm**.

### Likely questions
- **"Why fixed checks and not an agent choosing them?"** Measured on 73 labelled tenders: fixed order 0 false alarms, the model choosing 2. Cheaper is not better if it skips a re-read.
- **"Are the points probabilities?"** No. They only order the queue; there is no ground truth to fit them yet.
- **"Why files, not a database?"** 21 MB for 6 families, every page loads in under a third of a second, and append-only files cannot be corrupted by a crash. A database would pay off only at a much larger scale.

---

## Person 4 — The Split-purchase agent (agent 3), the rules of law and the rules assistant

### What you say in one sentence
"A goods contract can be given without competition only up to $25,000. The Split-purchase agent looks for one need cut into several contracts to stay under that limit. It works from the award file alone, for every goods category."

### Files
`agents/split_agent.py` · `tools/laws.py` · `tools/law_index.py` · `tools/local_llm.py` · `agents/rules_agent.py` · `prepare/fetch_laws.py` · `tests/*_eval.py`

### Main functions

**`find_groups(records)`** — `agents/split_agent.py`
The **rule** that finds candidates: same buyer, same supplier, non-competitive goods contracts of $25,000 or less each, within 30 days, together over $25,000. It finds 15 groups. The code also computes the facts: total, days, same-day awards, values just under the limit, a reference code shared by two titles.

**`judge(group, index)`** — `agents/split_agent.py`
The **agent**. It decides *split*, *separate* or *unclear*. Tools: `history` (everything this buyer bought from this supplier), `habit` (does this buyer do this with everyone?), `detail` (one contract in full), `decide`, `finish`. Up to 5 steps.

**`quote_holds(group, quote, index)`** — `agents/split_agent.py`
**The safety check.** A "split" must quote words that appear in two or more of the group's own titles, and that are **specific** to them: they must appear in fewer than 1 in 10 of this buyer and supplier's other contracts. A shared reference number passes; "Office Seating" does not. A split never backed this way ends as *unclear*. A split can only reach *watch*, never *alert*.

**`tools/laws.py`** — the **9 rules of law** the system checks. Each has a plain explanation, a short official quote, its source (Government Contracts Regulations, Canadian Free Trade Agreement, Supply Manual) and which agent uses it. Every reason on a case links to its rule.

**`ask_rules(question)`** — `agents/rules_agent.py`
The **rules assistant**, a retrieval-augmented generation (RAG). It runs on a **local** model (Gemma, through Ollama), so there is no quota and nothing leaves the computer. A small model is unreliable when left to plan alone, so the code drives three steps:
1. **Plan:** Gemma turns the question into words the law uses ("split a purchase" → "divide procurement requirements").
2. **Search:** hybrid search over the official texts (below).
3. **Answer:** Gemma answers from those passages only, with quotes.

Every quote must be word for word in its passage, or the answer is refused and Gemma gets one more try.

**`Index.search(query, k, qvec)`** — `tools/law_index.py`
The **hybrid search** over 917 passages of 9 official texts. Each passage is a few paragraphs under one heading, so it keeps its "Article 509".
- **Keywords** (BM25): exact terms, article numbers.
- **Meaning** (embeddings from EmbeddingGemma, cosine similarity): passages worded unlike the question.
- The two rankings are **fused by rank** (Reciprocal Rank Fusion).

Without the embedding model, it falls back to keywords.

### Secondary functions
- `split_agent.load_records` — one record per contract, its newest amendment.
- `money` — reads "33,175.49" correctly.
- `rule_verdict` — the rules' own answer, to compare with the agent's.
- `Index.history` / `habit` / `detail` — the agent's look-up tools.
- `by_who` / `save_verdict` — stored verdicts; the auditor's decides.
- `law_index.passages_of` — cuts a text into passages under headings.
- `stem` / `SYNONYMS` — divide = dividing; "split" → "divide".
- `rules_agent.check_citations` — the quote check.
- `local_llm.ask_json` / `embed` — call Gemma and EmbeddingGemma through Ollama, with a cache.
- `prepare/fetch_laws.py` — downloads the official texts once and keeps their text.

### How we measure (say this; it is our strongest point)
- **Held-out sets:** names never used for tuning (`eval m2 --split holdout`).
- **Blind labels:** a separate agent labels the data without ever seeing the system's answers, so nothing grades itself (split purchases, the unseen family).
- **Synthetic test:** brands planted in copies of real tenders, 7 ways. Some copies should be flagged, some should not ("or equivalent" nearby, a bidder's question, untouched): `eval synth`.
- **An unseen family** to check the rules are not tuned to our data (pumps and compressors).
- **The law search** on 15 auditor questions: `eval laws`.

### Show live
1. The **Split purchases** page: a group as a table, the rules', the agent's and the auditor's verdicts.
2. The **Rules** page: ask "Can a buyer split a purchase to stay under the limit?" and show the answer with checked quotes.

### Likely questions
- **"Is the split agent better than the rules?"** First run: same score (9 of the 12 it decided), but it fixed one rules mistake and the quote check stopped 2 false splits. Its 2 errors used vague quotes, which led to the "specific quote" rule. We say plainly that this rule was set on the same 15 groups.
- **"Why hybrid search?"** On 15 questions, keywords alone found the right passage in the top 6 for 8; meaning 14; hybrid 13. Keywords miss questions worded unlike the law; hybrid keeps exact terms like article numbers.
- **"Can the assistant invent a law?"** It can only answer with quotes the code finds word for word in the passages it was given. Otherwise the answer is refused.

---

## Numbers to know (everyone)

| What | Number |
|---|---|
| Notices per year, our 6 families | about 1,000 (about 5 readable tenders a week) |
| Real brand lock-ins in a random sample | 2 in 102 tenders (they are rare) |
| Spec auditor, held-out names | 34 of 35 right |
| Synthetic test | brand found in 83 of 148 rigged copies, 0 false alarms in 111 copies that should close |
| Coordinator: fixed checks vs model choosing | 0 vs 2 false alarms (73 tenders) |
| Unseen family (pumps), rules | 1 of 2 lock-ins caught, 0 wrongly cleared |
| Split purchases | 15 groups; rules 10 of 15; agent 9 of the 12 it decided |
| Law search, top 6 | keywords 8, meaning 14, hybrid 13 (of 15) |

## Limits we say before the teacher does

- Real rigging is rare, so we cannot measure precision on real fraud; we have no real ground truth yet (complaints to the trade tribunal are future work).
- A model named without its brand ("Nano25"), or a brand with no model number ("Elektronikon"), is missed by the rules.
- Synthetic brands are inserted mechanically, so that score is an upper bound.
- The split-purchase rule was tuned on its own 15 groups, with no separate test set.
- Groq's free quota limits how often we can re-measure the agents.
