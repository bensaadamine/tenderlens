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
| **M1 Screener** | rules | the notice | what the buyer declared: advance award notice, limited tendering, short bidding window | [docs/agents/M1_screener.md](docs/agents/M1_screener.md) |
| **M2 Spec Auditor** | rules + **agent** (`--agent`) | the specification | a brand required without "or equivalent" — the rules find the names and settle what the text decides exactly; the agent letters the rest a–f with a checked quote | [docs/agents/M2_spec_auditor.md](docs/agents/M2_spec_auditor.md) |
| **M3 Investigator** | rules + **agent** (`--m3-agent`) | award history | who won, whether this buyer keeps choosing the same supplier — the agent decides which spellings are the same company | [docs/agents/M3_investigator.md](docs/agents/M3_investigator.md) |
| **M4 Case Builder** | rules + fixed checks (`--m4-checks`) | M1 + M2 + M3 | checks every claim has a source, scores it, decides alert / watch / close; before ranking a brand case it sends the finding back to the M2 agent and the winner's spellings to the M3 agent | [docs/agents/M4_case_builder.md](docs/agents/M4_case_builder.md) |

M4's checks run in a fixed order: a version where a model chose them was measured and not chosen (0 false alarms with the fixed order, 2 with the model choosing, on 73 labelled tenders) — it is kept in `experiments/`. What M2 and M3 hand to M4 is defined once, in `agents/evidence.py`.

**Routing in one line:** only something found **in the documents** can raise an **alert**; what the buyer **declared** on the notice only ranks a case; award history is **context**.

---

## Folder layout

```
tenderlens.py          the one command: demo, tender, run, test, eval, awards, prepare, llm
pipeline/              one_tender.py (the four components on one tender), stream.py (a category,
                       tender by tender, against its knowledge base), demo.py, settings.py (one run's choices)
agents/                M1–M4; m4_checks.py = M4's checks; agent_loop.py = the loop the M2 and M3 agents share
tools/                 documents.py (a tender's text), brands.py (brand rules), wikidata.py,
                       level2.py (peer comparison), llm.py (LLM access), report.py (the queue pages)
prepare/               data preparation, in order: profile → docs → text → census → labels
tests/                 offline tests (test_*.py), evaluations (m2_eval.py, m4_eval.py), synth_rig.py
experiments/           measured and not chosen: m4_model_coordinator.py (a model choosing M4's checks)
docs/agents/           one page per component
docs/DECISIONS.md      the numbered decisions the code cites ("decision 15")
data/                  not in git: tenders.csv, awards.csv, downloaded documents
reports/               not in git: extracted text, knowledge bases, audit queues, caches
```

Everything starts from `python tenderlens.py <command>` (`-h` for the list); it always works from the repository root. Folders are normal Python packages (`from tools import brands`); a single module runs with `python -m tools.brands ...` from the root.

---

## Setup

```bash
pip install -r requirements.txt     # requests, pdfminer.six, langgraph
```

No `pandas` (blocked on our Windows machines) — standard library plus those two.

LLM keys go in `.env` (never committed): `GROQ_API_KEY=...`, and optionally `GEMINI_API_KEY=...`. The models are pinned (`tools/llm.py`): Groq's gpt-oss-120b answers every agent call. When its daily quota is spent the run stops, and the next run resumes where it stopped — every verdict comes from the same model. `TENDERLENS_SHARE=1` also sends calls to Gemini (twice the calls a minute, but verdicts can differ between runs). `python tenderlens.py --offline <command>` takes model answers from the cache only: a missing one is reported, never paid, and no key is needed. `python tenderlens.py llm` shows the models; `--usage` the tokens spent.

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
python tenderlens.py run --group 2510 --online        # the four components, tender by tender
```

Open `reports/kb/2510/batches/<date>_queue.html`.

| flag | effect |
|---|---|
| `--online` | check brand names on Wikidata (free, cached) |
| `--agent` | the M2 agent letters every brand candidate a–f with a quote (LLM) |
| `--m3-agent` | the M3 agent decides which spellings of the winner are the same company (LLM) |
| `--m4-checks` | before ranking a case with a brand finding, M4 sends each finding back to the M2 agent and the winner's spellings to the M3 agent, in a fixed order (LLM). `model` lets a model choose the checks — an experiment, measured worse |
| `--reprocess REF… / quarantine / all` | run tenders again with today's code (auditor-judged tenders are kept) |
| `--queue [STAMP]` | rebuild a queue from the case log |
| `--until YYYY-MM-DD` | only tenders published up to that date |
| `--no-level2` | skip level 2 (numbers compared with peer tenders): dormant today, never changes a route. Also on `tender` and `demo` |
| `--status` | what is in the knowledge base |
| `--feedback` | apply the auditor's verdicts from `reports/kb/<group>/feedback.csv` |

One tender, with every agent's reasoning: `python tenderlens.py tender cb-425-92729935`

Tests (no data, no network): `python tenderlens.py test` runs every `tests/test_*.py`; one alone: `python -m tests.test_agents`.
Measurements: `python tenderlens.py eval synth --run` (synthetic rigged tenders) · `python tenderlens.py eval m2 [--agent --online] [--split holdout]` (M2 against the labelled names: 36 tuning + 40 held-out) · `python tenderlens.py eval m4 --online` (M4: no checks / fixed / model).

**Long runs** (agents over a whole category) are done **once, at the end of a category** — not after each fix. Fixes are checked offline on the affected tenders and on the synthetic set.

---

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

## Where things stand (2026-11-08)

| category | tenders | alert | watch | note |
|---|---|---|---|---|
| 2510 motor vehicles | 613 (193 with documents) | 0 | 3 | offline, rules only |
| 4110 lab equipment | 957 | 2 | 10 | offline, rules only; to audit |

- **M2 on the 37 held-out names** (never used for tuning): rules alone find **1 of 8** real lock-ins; with the M2 agent **8 of 8**, 8 of its 9 findings real, 36 of 37 names right.
- **M4's checks on 73 labelled tenders** (M2 on rules): 7 false alarms without them, 0 with them (both real lock-ins kept).
- **The demo** shows each on chosen cases: the rules' false alarm cleared by the re-check (Hitachi/JEOL), a lock-in only the agent finds (Gamry), the real lock-ins kept.
- **Synthetic test (2510):** of 160 rigged copies, the brand is found in 64 offline; 17 reach alert; 0 of 120 clean copies are flagged.
- **Peer comparison (level 2)** stays dormant: few product groups have 8 comparable tenders yet. It runs by default; `--no-level2` skips it (same routes on the demo and on 120 tenders of 2510).

---

## Data source

CanadaBuys open data, no key, English and French, republished daily. We keep a snapshot (`data/tenders.csv`, 2026-10-05).

- Only attachments on `canadabuys.canada.ca` are real files; identify them by their first bytes, not their declared type.
- No contract value in the feed, and the contracts file does not join to it.
