# TenderLens — Canada

Reads published federal procurement records and returns a ranked audit queue:
which contracts a state auditor should inspect first, with the evidence already
assembled.

One question per contract: **was the competition real, or staged?**

TenderLens never says "fraud". Every case is a hypothesis with a quoted,
checkable source; a human decides.

---

## Requirements

```bash
pip install requests pdfminer.six
```

`pandas` is **not** used — Application Control blocks it on our Windows
machines. Everything runs on the standard library plus those two packages.

**Network access is needed only on Windows**, for: downloads (`01`, `02`,
`m3_investigator.py --fetch`), the LLM (`05`, `--llm`) and Wikidata
(`--online`). Everything else runs offline.

---

## Running the system

```bash
python 03_inspect_specs.py                       # text cache, if not built
python 04_census.py --group 2510 --map           # level-2 knowledge base
python m3_investigator.py --fetch                # award notices, once
python m3_investigator.py --columns              # check what was detected
python tenderlens.py --group 2510 --online       # run the four agents
```

Open **`reports/audit_queue.html`**. Also written: `audit_queue.csv` (one row
per tender, for scoring precision@50 later) and `audit_queue.json` (every
claim, source and agent trace).

One tender, with the full reasoning of every agent:

```bash
python tenderlens.py --tender cb-258-72766150
```

Point the downloader at the tenders the screener wants read:

```bash
python tenderlens.py --group 2510 --deep-refs reports/m1_deep_refs.txt
python 02_fetch_docs.py --refs reports/m1_deep_refs.txt
python 03_inspect_specs.py
```

Check the agents enforce their own rules (no data needed):

```bash
python test_agents.py
```

| flag | effect |
|---|---|
| `--online` | verify brand names on Wikidata. **Needed in practice** — offline, the brand cache resolves almost nothing outside 4110 |
| `--llm` | let M2's LLM judge each suspected lock-in on tenders M1 routes deep. Spends tokens |
| `--limit N` | first N tenders only |

---

## The four agents

Each works on one tender at a time, calls tools, and chooses its next step
from what the tools return. Every step is written to a trace.

| | file | reads | decides |
|---|---|---|---|
| M1 Screener | `m1_screener.py` | the notice only | how deep the expensive work goes |
| M2 Spec Auditor | `m2_spec_auditor.py` | the specification | whether to verify online / ask the LLM |
| M3 Investigator | `m3_investigator.py` | award notices | whether to look at the supplier's other buyers |
| M4 Case Builder | `m4_case_builder.py` | M1 + M2 + M3 | alert / watch / close |

M2's tools: `spec_tools.py` (level 1 — brand named without "or equivalent")
and `level2.py` (level 2 — requirements bounded tighter than peers).

**What can raise an ALERT:** only a level-1 finding quoted from the document —
a concrete departure from the CFTA/CETA duty to write a functional
specification. Notice signals (ACAN, a limited-tendering reason) are things the
buyer *declared*, so they make a case worth **watching**. A buyer that keeps
awarding to the same supplier is **context**: it strengthens a case that has
document evidence and adds nothing to one that does not. Level 2 can add to a
case but never make one.

**Scores are uncalibrated orderings, not probabilities.** The weights live in
one table in `m4_case_builder.py`; there is no ground truth yet to fit them.

---

## Where things stand

On UNSPSC-4 2510 (motor vehicles, 613 notices, 193 with documents):
**0 alert, 16 watch, 597 close** — without award data or `--online`, which
are the next two runs.

- **Level 1 is now precise but sparse.** Its rules used to accept a "model
  number" anywhere within 25 characters, so `Square`, `Ensure` and
  `Joint Venture` passed on a nearby clause number — every rule-based finding
  on 400 tenders was false. It now needs the model code as the next token
  (`Michelin XZL2`, `Palfinger model PK11001`). The only two genuine brands in
  2510 both say "or equivalent", so they are correctly cleared.
- **Level 2's peer comparison was measured not to work on this corpus.** Clean
  peer groups (UNSPSC-8) leave no attribute with 8 peers; wider ones (UNSPSC-6)
  mix vehicle classes, so its top "finding" was a semi-trailer compared with
  pickup trucks. It ships as the within-document test, reported low-confidence.
- **The 16 watch cases are all buyer-declared limited competition**, and the
  documents for 15 of them were never downloaded. `--deep-refs` fixes that.

---

## Pipeline scripts

Every script is resumable and never re-downloads.

| script | does |
|---|---|
| `01_profile_tenders.py` | downloads the tender feed (185 MB), profiles methods, windows, limited tendering, coverage; writes the project scope |
| `02_fetch_docs.py` | downloads specification attachments — by category (`--only 2510 --per-cat 0`) or by name (`--refs FILE`) |
| `03_inspect_specs.py` | extracts text (pdfminer, `zipfile`), caches it in `reports/text/`. **`--refresh`** after changing extraction settings |
| `04_census.py` | extracts numeric requirements scoped to the requirement section, resolves units, dedups — the level-2 knowledge base. `--map` applies the attribute map |
| `05_attribute_map.py` | LLM canonicalisation of requirement labels. `--recheck-unknown` re-asks only failed labels |
| `llm.py` | LLM access (Groq → Cerebras → Mistral → Gemini), caching, token log |

---

## Data source

CanadaBuys open data, no API key, bilingual EN/FR, updated daily.

- Attachments on `sscp2pspc.ssc-spc.gc.ca` return an HTML login page, not a
  file. Filter to `canadabuys.canada.ca`.
- `Content-Type` is `application/octet-stream` for everything. Identify files by
  magic bytes, not by header.
- The notice feed has no contract value, and the contracts file does not join
  to it — so value is not a signal anywhere in the system.

---

## Layout

```
01 .. 05_*.py         pipeline
m1_screener.py        agents
m2_spec_auditor.py
m3_investigator.py
m4_case_builder.py
tenderlens.py         orchestrator + dashboard
spec_tools.py         M2 tool: level 1
level2.py             M2 tool: level 2
test_agents.py        the agents' rules, as tests
data/     not in git — tenders.csv, awards.csv, docs/
reports/  not in git, except selected_unspsc4.json
```
