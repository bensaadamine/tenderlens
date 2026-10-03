# TenderLens

**Was the competition real?**

An audit triage system for public procurement. It reads published tender records and returns a ranked queue telling auditors which contracts to inspect first — with the evidence already assembled.

It does **not** decide who should win a contract, and it never accuses anyone of fraud. It flags contracts where the competition does not appear to have been genuine, cites the exact source of every claim, and leaves the decision to a human.

Agentic AI project — Option Data Science, 2026/2027. Theme: Legal / Contract Analysis.

---

## Status

| Phase | State |
|---|---|
| 1 — Business understanding | ✅ done |
| 2 — Data acquisition & understanding | ✅ done (structured data) |
| 3 — Modeling | 🔜 next |
| 4 — Deployment & dashboard | ⬜ |

10,000 tenders cached. Project scope fixed at 20 product categories.
Current work: opening the specification documents.

---

## Setup

```bash
git clone <repo-url>
cd tenderlens

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

No API key needed. The Prozorro API is public and free.

---

## Rebuilding the dataset

The dataset is not in git — the scripts regenerate it. Run them in order from `scripts/`:

```bash
python 01_sample_feed.py --pages 50        # walk the feed, cheap fields only
python 02_fetch_tenders.py --max 300       # fetch full records, resumable
python 03_profile.py                       # what fields exist, how often filled
python 04_diagnose.py                      # documents, baselines, labels, depth
python 05_harvest.py --target 10000        # harvest at scale (~45 min)
python 06_pick_categories.py               # choose the 20 working categories
python 07_fetch_docs.py --max 60           # download specification files
```

Every script is **resumable**. Stop any of them with Ctrl-C and run it again — nothing is re-downloaded.

Expect about an hour for a full rebuild, almost all of it in `05_harvest.py`.

---

## Layout

```
CLAUDE.md                  project context: decisions, data facts, conventions
docs/
  data-understanding.md    what we learned from 10,000 real tenders
  Phase1_Theme_and_Objectives.md
scripts/
  prozorro.py              API client
  01_ … 07_                the pipeline above
reports/
  selected_cpv4.json       the 20 categories in scope  (the only committed report)
data/                      not in git — regenerate with the scripts
```

---

## Data source

[Prozorro](https://prozorro-api-docs.readthedocs.io/) — Ukraine's national public procurement system.
Public API, free, no key, OCDS-compatible, records from February 2015.

`https://public-api.prozorro.gov.ua/api/2.5`

Two properties worth knowing before reading the code:

- It is a **change log**, not a database. You walk it by date; you cannot query by category.
- The list view carries only id, date, procedure type and status. Everything else needs the full record — which is why we download broadly and filter afterwards.

Specification documents are in Ukrainian. Most of the analysis is language-independent (numbers, units, operators, Latin-script brand names); the rest relies on a small term dictionary.

Please keep the ~0.25 s delay between requests in `prozorro.py`. It is a free public service.

---

## Team

Four members, one agent each:

| | Agent | Role |
|---|---|---|
| M1 | Screener | filters the volume using cheap signals |
| M2 | Spec Auditor | reads the tender document for tailored requirements |
| M3 | Investigator | checks whether the bidders were truly rivals |
| M4 | Case Builder | scores, verifies the evidence, writes the case file |

The agents are chained, so the parts have to work together.

---

## Reading order for someone new

1. This file
2. `CLAUDE.md` — the decisions and why they were made
3. `docs/data-understanding.md` — what the data actually looks like
