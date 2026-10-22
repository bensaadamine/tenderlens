# TenderLens

Reads Canada's published procurement records (CanadaBuys) and returns a **ranked audit queue**: which contracts a state auditor should look at first, with the evidence already gathered.

One question per contract: **was the competition real, or staged?**

TenderLens never says "fraud". Every case is a hypothesis with a source a person can open; a person decides.

---

## How it works

Four agents look at one tender at a time, each from a different angle, and the last one writes the case:

```
 tender notice ──► M1 Screener ──┐
 tender documents ► M2 Spec Auditor ┼──► M4 Case Builder ──► audit queue (alert / watch / close)
 award notices ──► M3 Investigator ┘
```

| Agent | Looks at | Finds | Doc |
|---|---|---|---|
| **M1 Screener** | the notice | what the buyer declared: advance award notice, limited tendering, short bidding window | [docs/agents/M1_screener.md](docs/agents/M1_screener.md) |
| **M2 Spec Auditor** | the specification | a brand required without "or equivalent"; requirements tighter than similar tenders | [docs/agents/M2_spec_auditor.md](docs/agents/M2_spec_auditor.md) |
| **M3 Investigator** | award history | who won, and whether this buyer keeps choosing the same supplier | [docs/agents/M3_investigator.md](docs/agents/M3_investigator.md) |
| **M4 Case Builder** | M1 + M2 + M3 | checks every claim has a source, scores it, decides alert / watch / close | [docs/agents/M4_case_builder.md](docs/agents/M4_case_builder.md) |

**Routing in one line:** only something found **in the documents** can raise an **alert**; what the buyer **declared** on the notice only ranks a case; award history is **context**.

---

## Folder layout

```
tenderlens.py          run the agents on a category, all at once
tenderlens_stream.py   run tender by tender against a growing knowledge base (normal mode)
agents/                the four agents (M1–M4)
tools/                 shared tools: spec_tools.py (brands), level2.py (peer comparison), llm.py (LLM access)
prepare/               data preparation, run in order 01 → 05
tests/                 test_agents.py (rules), synth_rig.py (synthetic rigged tenders), llm_test.py
docs/agents/           one page per agent
data/                  not in git: tenders.csv, awards.csv, downloaded documents
reports/               not in git: extracted text, knowledge bases, audit queues, caches
```

Run every command **from the repository root**.

---

## Setup

```bash
pip install -r requirements.txt     # requests, pdfminer.six, langgraph
```

No `pandas` (blocked on our Windows machines) — standard library plus those two.

LLM keys go in `.env` (never committed): `GROQ_API_KEY=...`, `GEMINI_API_KEY=...`. With two keys the calls are shared between providers.

**Network is needed only on Windows**: downloads (`prepare/01`, `prepare/02`, `agents/m3_investigator.py --fetch`), Wikidata (`--online`), the LLM (`--llm`, `prepare/05`).

---

## Running

First time, for one category (example 2510, motor vehicles):

```bash
python prepare/01_profile_tenders.py                  # notices feed (reuses data/tenders.csv)
python prepare/02_fetch_docs.py --only 2510 --per-cat 0
python prepare/03_inspect_specs.py                    # extract text -> reports/text/
python agents/m3_investigator.py --fetch              # award notices, once
python tenderlens_stream.py --group 2510 --online     # the four agents, tender by tender
```

Open `reports/kb/2510/batches/<date>_queue.html`.

| flag | effect |
|---|---|
| `--online` | check brand names on Wikidata (free, cached) |
| `--agent` | the M2 agent letters every brand candidate a–f with a quote (LLM; replaces `--llm`) |
| `--llm` | older path: the LLM triages unconfirmed names and judges findings yes/no |
| `--until YYYY-MM-DD` | only tenders published up to that date |
| `--status` | what is in the knowledge base |
| `--feedback` | apply the auditor's verdicts from `reports/kb/<group>/feedback.csv` |

One tender, with every agent's reasoning: `python tenderlens.py --tender cb-425-92729935`

Tests: `python tests/test_agents.py` and `python tests/test_m2_agent.py` (no data, no network) · `python tests/synth_rig.py --run` (synthetic rigged tenders) · `python tests/m2_eval.py [--agent --online]` (M2 against the 36 cases judged by hand, `tests/m2_labels.csv`).

**Long runs** (`--llm` over a whole category, about an hour) are done **once, at the end of a category** — not after each fix. Fixes are checked offline on the affected tenders and on the synthetic set.

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

## Where things stand (2026-10-22)

| category | tenders | alert | watch | note |
|---|---|---|---|---|
| 2510 motor vehicles | 613 (193 with documents) | 0 | 9 | offline run; with the LLM: Bendix AD9 air dryer found in 2 tenders (genuine) |
| 4110 lab equipment | 957 | 1 | 13 | offline run; **9 of the 11 brand findings checked by hand are false** |

- **The brand check is the weak point.** On the 36 cases judged by hand, the rules alone give 11 findings of which **10 are false**, and find 1 of the 4 real lock-ins. The M2 agent (`--agent`) is built to fix this: the rules only collect candidates, and the agent investigates each one and gives it a letter a–f with a quote. Its score on the same 36 cases is the next measurement.
- **Peer comparison (level 2)** stays dormant: few product groups have 8 comparable tenders yet.
- **Synthetic test (2510):** of 160 rigged copies, the brand is found in 64; 17 reach alert; 0 of 120 clean copies are flagged. Brands Wikidata knows are found 48/60; others 20/140.

---

## Data source

CanadaBuys open data, no key, English and French, republished daily. We keep a snapshot (`data/tenders.csv`, 2026-10-05).

- Only attachments on `canadabuys.canada.ca` are real files; identify them by their first bytes, not their declared type.
- No contract value in the feed, and the contracts file does not join to it.
