# TenderLens

Reads Canada's published procurement records (CanadaBuys) and returns a **ranked audit queue**: which contracts a state auditor should look at first, with the evidence already gathered.

One question per contract: **was the competition real, or staged?**

TenderLens never says "fraud". Every case is a hypothesis with a source a person can open; a person decides.

---

## How it works

A **hybrid multi-agent workflow**: four components look at one tender at a time, each from a different angle, and the last one writes the case. Two of them are **agents** — a model chooses its own tools and steps, inside its specialty, where judgement is needed: is this name a brand the bidder must supply? are these two spellings the same company? The rest are **rules**, where exactness matters: what the notice declares, whether a quote is really in the document, scoring and routing. Every choice between the two was measured (`docs/agents/`).

```
 tender notice ──► M1 Screener ──┐
 tender documents ► M2 Spec Auditor ┼──► M4 Case Builder ──► audit queue (alert / watch / close)
 award notices ──► M3 Investigator ┘
```

| Component | Kind | Looks at | Finds | Doc |
|---|---|---|---|---|
| **M1 Screener** | rules | the notice | what the buyer declared (limited tendering), a short bidding window, whether a trade agreement applies | [docs/agents/M1_screener.md](docs/agents/M1_screener.md) |
| **M2 Spec Auditor** | rules + **agent** (`--agent`) | the specification | a brand required without "or equivalent" — the rules find the names and settle what the text decides exactly; the agent letters the rest a–f with a checked quote | [docs/agents/M2_spec_auditor.md](docs/agents/M2_spec_auditor.md) |
| **M3 Investigator** | rules + **agent** (`--m3-agent`) | award history | who won, whether this buyer keeps choosing the same supplier — the agent decides which spellings are the same company | [docs/agents/M3_investigator.md](docs/agents/M3_investigator.md) |
| **Split-purchase agent** | rules + **agent** (`splits --agent`) | the award file alone (no documents) | one need cut into several contracts, each under the $25,000 limit for buying without competition: the rules find the groups, the agent judges split / separate / unclear with a checked quote | [docs/agents/split_agent.md](docs/agents/split_agent.md) |
| **Rules assistant** | local model (Gemma 4B, Ollama) + code | the official law texts (`prepare laws`) | answers the auditor's questions about the rules, with quotes checked word for word; search only without a model | [docs/agents/rules_agent.md](docs/agents/rules_agent.md) |
| **M4 Case Builder** | rules + fixed checks (`--m4-checks`) | M1 + M2 + M3 | checks every claim has a source, scores it, decides alert / watch / close; before ranking a brand case it sends the finding back to the M2 agent and the winner's spellings to the M3 agent | [docs/agents/M4_case_builder.md](docs/agents/M4_case_builder.md) |

M4's checks run in a fixed order: a version where a model chose them was measured and not chosen (0 false alarms with the fixed order, 2 with the model choosing, on 73 labelled tenders). What M2 and M3 hand to M4 is defined once, in `agents/evidence.py`.

**Routing in one line:** only something found **in the documents** can raise an **alert**; what the buyer **declared** on the notice only ranks a case; award history and "no trade agreement" are **context** and never put a case in the queue.

**Split purchases** are a separate list (the award file alone, every goods category): a split goes to watch, never alert; the auditor's verdict decides.

**Not audit cases:** an advance contract award notice (the supplier was announced in advance and others could object), and anything cancelled, still open or not a purchase. In selective tendering (only pre-qualified suppliers bid), a short window is normal and doesn't count.

---

## Folder layout

```
tenderlens.py          the one command: demo, tender, run, test, eval, awards, prepare, llm
pipeline/              one_tender.py (the four components on one tender), stream.py (a category,
                       tender by tender, against its knowledge base), demo.py, settings.py (one run's choices)
agents/                M1–M4; m4_checks.py = M4's checks; split_agent.py = split purchases;
                       agent_loop.py = the loop the M2, M3 and split agents share
tools/                 documents.py (a tender's text), sections.py (where the requirements section is),
                       brands.py (brand rules), wikidata.py, level2.py (peer comparison), llm.py (LLM access),
                       report.py (the queue pages), names.py (how brand and company names are compared)
prepare/               data preparation, in order: profile → docs → text → labels
tests/                 offline tests (test_*.py), evaluations (m2_eval.py, m4_eval.py), synth_rig.py
docs/agents/           one page per component
docs/DECISIONS.md      the numbered decisions the code cites ("decision 15")
data/                  not in git: tenders.csv, awards.csv, downloaded documents
reports/               not in git: extracted text, knowledge bases, audit queues, caches
```

Everything starts from `python tenderlens.py <command>` (`-h` for the list); it always works from the repository root. Folders are normal Python packages (`from tools import brands`); a single module runs with `python -m tools.brands ...` from the root.

---

## Setup

```bash
pip install -r requirements.txt     # requests, pdfminer.six, langgraph, fastapi, uvicorn
```

No `pandas` needed: the pipeline is the standard library plus those packages. (Smart App Control is off on our Windows machines, so compiled packages install.)

LLM keys go in `.env` (never committed): `GROQ_API_KEY=...`, optionally `GROQ_API_KEY2=...` (a second Groq account: same model, so verdicts don't change; each call goes to the key that can take it soonest, which doubles the calls a minute and the daily quota), and optionally `GEMINI_API_KEY=...`. The models are pinned (`tools/llm.py`): Groq's gpt-oss-120b answers every agent call. When its daily quota is spent the run stops, and the next run resumes where it stopped — every verdict comes from the same model. `TENDERLENS_SHARE=1` also sends calls to Gemini (twice the calls a minute, but verdicts can differ between runs). `python tenderlens.py --offline <command>` takes model answers from the cache only: a missing one is reported, never paid, and no key is needed. `python tenderlens.py llm` shows the models; `--usage` the tokens spent.

**Network is needed only on Windows**: downloads (`prepare profile`, `prepare docs`, `awards --fetch`), Wikidata (`--online`), the LLM (`--agent`, `--m3-agent`, `--m4-checks`, `prepare labels`).

---

## Running

**The demo** — seven chosen tenders (real lock-ins, false alarms, an ordinary tender), each run three ways side by side: rules only; rules + M4 sending flagged findings back to the M2 agent; every agent.

```bash
python tenderlens.py demo               # Windows, online: the first run fills the caches
python tenderlens.py demo --offline     # anywhere: replays from the caches, no network
```

Writes `reports/demo/<stamp>/summary.txt` (the table) and one queue page per setup.

First time, for one category (example 2510, motor vehicles):

```bash
python tenderlens.py prepare profile                  # notices feed (reuses data/tenders.csv)
python tenderlens.py prepare docs --only 2510 --per-cat 0
python tenderlens.py prepare text                     # extract text -> reports/text/
python tenderlens.py awards --fetch                   # award notices, once
python tenderlens.py run --group 2510 --install --online                          # the base: oldest 70%, rules only
python tenderlens.py run --group 2510 --online --agent --m3-agent --m4-checks    # the newest 30%, hybrid
```

Open `reports/kb/2510/batches/<date>_queue.html`.

**Which tenders are processed.** Only a tender that was **awarded** (it has a row in the award file) and **whose documents we hold** goes through M1–M4 and can reach the queue. A cheap check decides it first, from the notice and the award file, before any text is read. The others are only recorded: cancelled tenders, requests for information and supply arrangements are not audit cases; open tenders are checked again on every run; a tender that closed with documents but **no award found** goes on the family's **Award not found** list, for the inspector to check the award by hand (sorted by date, or with the tenders where the rules find a required brand first). When its award appears in the file, the next run processes it. An award found by solicitation number counts only if its title shares a word with the tender's: buyers reuse their numbers.

**Knowledge base: installation, then new tenders.** A category's base is started once with its **oldest 70%** of tenders (by publication date), **rules only** — no model, 0 tokens. The newest 30% then play the new tenders: they go through the **hybrid chain** (agents included), as every later tender will, and the base grows with them.

```bash
python tenderlens.py run --group 2510 --install --online   # once: the oldest 70%, rules only
python tenderlens.py run --group 2510 --online --agent --m3-agent --m4-checks   # the rest
```

**Weekly update** — one command downloads the latest notices, awards and documents, installs any category that has no base yet, then runs only the tenders not yet in each base, with the agents (`--rules-only` leaves them out):

```bash
python tenderlens.py update                    # every category with a base; --groups to choose
```

A broken download never replaces the current file; the replaced one is kept in `data/previous/`. To run it every Monday at 08:00 on Windows (once, in a command prompt):

```bat
schtasks /Create /SC WEEKLY /D MON /ST 08:00 /TN TenderLens-update /TR "cmd /c cd /d C:\Users\bensa\dev\tenderlens && python tenderlens.py update >> reports\update.log 2>&1"
```

| flag | effect |
|---|---|
| `--online` | check brand names on Wikidata (free, cached) |
| `--agent` | the M2 agent letters every brand candidate a–f with a quote (LLM) |
| `--m3-agent` | the M3 agent decides which spellings of the winner are the same company (LLM) |
| `--m4-checks` | before ranking a case with a brand finding, M4 sends each finding back to the M2 agent and the winner's spellings to the M3 agent, in a fixed order (LLM) |
| `--install` | start a category's base: its oldest 70% of tenders, rules only (refuses the agents and an existing base) |
| `--reprocess REF… / quarantine / all` | run tenders again with today's code (auditor-judged tenders are kept) |
| `--queue [STAMP]` | rebuild a queue from the case log |
| `--until YYYY-MM-DD` | only tenders published up to that date |
| `--no-level2` | skip level 2 (numbers compared with peer tenders): dormant today, never changes a route. Also on `tender` and `demo` |
| `--status` | what is in the knowledge base |
| `--feedback` | apply the auditor's verdicts from `reports/kb/<group>/feedback.csv` |

One tender, with every agent's reasoning: `python tenderlens.py tender cb-425-92729935`

**Rules assistant:** `python tenderlens.py prepare laws` (once, Windows: the official texts) · `ollama pull gemma3:4b` (once: the local model) · `python tenderlens.py ask "Can a buyer split a purchase?"` (`--search-only` without a model).

**Split purchases** (award file only, every goods category): `python tenderlens.py splits` (groups and the rules' verdict) · `splits --agent --labels tests/split_labels.csv` (with the agent, compared with blind labels).

Tests (no data, no network): `python tenderlens.py test` runs every `tests/test_*.py`; one alone: `python -m tests.test_agents`.
Measurements: `python tenderlens.py eval synth --run` (synthetic rigged tenders over 6 families; each run in its own folder `reports/synth/families/runs/<stamp>/`; `--review` lists the bases' "equivalent" lines to check by hand) · `python tenderlens.py eval m2 [--agent --online] [--split tuning|holdout|sample]` (M2 against labelled names: tuning, held-out, and a random sample of 102 real tenders) · `python tenderlens.py eval m4 --online` (M4: no checks / fixed checks, 73 labelled tenders).

**Long runs** (agents over a whole category) are done **once, at the end of a category** — not after each fix. Fixes are checked offline on the affected tenders and on the synthetic set.

---

## The auditor's page

```bash
pip install fastapi uvicorn
python tenderlens.py serve          # opens http://127.0.0.1:8000 (this computer only)
```

A top bar leads to four places; the model mode (Rules, Hybrid, Replay) sits on its right.

- **Mode** for every command: **Rules** (no model called); **Hybrid**: rules + the agents, live (the Groq quota; the rules assistant uses the local model); **Replay**: the agents' answers from the cache (no network, no quota: the demo).
- **To decide:** every flagged purchase in one list, most serious first: cases from every family, and split purchases waiting for your verdict.
- **Case:** the tender's own words, marked as with a highlighter, opening the document at that line; the rule each reason rests on; the brand names checked and who settled each; your verdict (Mark clean / Confirm); how each component reached it (folded).
- **Families:** one row per family: to review, confirmed, cleared, awards not found (check by hand), re-run, all tenders; recent commands with their live output.
- **Split purchases:** each group under the $25,000 limit as a table of contracts, the rules', the agent's and your verdict; **Split** / **Separate**.
- **Rules:** a question box (the rules assistant) and the rules of law behind each signal, with their sources.
- **How it works:** the four agents (what each decides, its tools, what the code keeps, what was measured), with links to agent activity and the synthetic test.

Fonts (Atkinson Hyperlegible Next for the interface, Source Serif 4 for documents and laws, IBM Plex Mono for raw text; SIL Open Font License) are bundled in `interface/static/fonts/`, so the page works offline.

A verdict is applied at once and kept in `feedback.csv`, as `run --feedback` does.

## The auditor's feedback

Each case in the queue is checked by a person:

| letter | meaning | verdict |
|---|---|---|
| a | not a brand (the tender's own acronym, an ordinary word, a standard, a web address, a date) | `clean` |
| b | a bidder is speaking (inside "Question N" … "Answer N") | `clean` |
| c | the buyer's own equipment — the purchase must fit, carry or repair it | `clean` |
| d | a list of several brands | `clean` |
| e | "or equivalent" exists somewhere, including the standard "Equivalent Products … form, fit, function" clause | `clean` |
| f | the brand is required with no equivalent | `confirmed` |

One line per case in `feedback.csv` (`reference,verdict,note,reviewer,date`), then `--feedback`. `clean` cases return to the knowledge base; `confirmed` cases stay apart as real test cases.

---

## Where things stand (2026-11-25)

Six families, each with a base (oldest 70%, rules) and its newest 30% read with the agents:

| family | in the base | to review |
|---|---|---|
| 2510 motor vehicles | 105 | 1 |
| 4110 laboratory equipment | 208 | 0 |
| 5610 furniture | 184 | 0 |
| 4111 measuring instruments | 104 | 1 |
| 4014 valves | 120 | 0 |
| 2410 lifting equipment | 59 | 0 |

- **Real lock-ins are rare.** In 102 tenders drawn at random (17 per family), we found 2 by reading the requirements. The rules now raise one of them for the agent ("approved distributor of Applied Test Systems"); the other names a model by part number only.
- **M2 on the held-out names** (never used for tuning): with the agent, 34 of 35 right, 7 of 7 lock-ins found (2 names wait for a fresh agent answer).
- **Synthetic test (6 families, 148 rigged copies):** with Wikidata online, the brand is found in 77 (52%), 0 false alarms on the 111 copies that should close. Brands Wikidata doesn't know need the agent.
- **Measured errors, fixed as general rules (2026-11-24):** furniture call-ups flagged on deadline + "no trade agreement"; French words and measurements read as brands ("ENVIRON 610MM"); equivalents accepted in other words; awards matched by a reused solicitation number.
- **Split purchases (agent 3, 2026-11-25):** 15 groups in all goods. Against blind labels (2 split, 10 separate, 3 unclear), the rules alone get 10 of 15, with 3 false splits; the agent is to be measured.
- **A list the buyer calls approved products** naming two or more makes now clears a brand (the 4111 blood-alcohol kits: six containers approved by law, written "(a)…(f)").
- **Peer comparison (level 2)** stays dormant: few product groups have 8 comparable tenders. `--no-level2` skips it.

---

## Data source

CanadaBuys open data, no key, English and French, republished daily. We keep a snapshot (`data/tenders.csv`, 2026-10-05).

- Only attachments on `canadabuys.canada.ca` are real files; identify them by their first bytes, not their declared type.
- No contract value in the feed, and the contracts file does not join to it.
