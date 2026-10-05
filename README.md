# TenderLens — Canada

Reads published federal procurement records and returns a ranked audit queue:
which contracts a state auditor should inspect first, with the evidence already
assembled.

One question per contract: **was the competition real, or staged?**

This folder is the Canadian data pipeline. It feeds the four-agent system.

---

## Requirements

```bash
pip install requests pdfminer.six
```

`pandas` is **not** used — Application Control blocks it on our Windows
machines. Everything runs on the standard library plus those two packages.
`pdfminer.six` is pure Python, so it is not affected.

---

## Pipeline

Run in order. Every script is resumable and never re-downloads.

### `01_profile_tenders.py` — profile the source

```bash
python 01_profile_tenders.py
```

Downloads the CanadaBuys tender notice feed (185 MB) if `data/tenders.csv` is
missing, then reports what is being bought, bidding windows per procurement
method, limited-tendering justifications, attachment coverage, and category
depth.

Writes `reports/selected_unspsc4.json` — the project scope, the top 20 UNSPSC-4
goods categories ranked by *usable* tender count.

### `02_fetch_docs.py` — download the specifications

```bash
python 02_fetch_docs.py --dry-run     # see the plan first
python 02_fetch_docs.py               # top 5 categories, 60 tenders each
```

Re-ranks the scope groups, takes the top N, samples tenders inside each, and
downloads every direct attachment.

| flag | default | note |
|---|---|---|
| `--top` | 5 | how many UNSPSC-4 groups |
| `--per-cat` | 60 | tenders per group; `0` = all |
| `--max-att` | 0 | attachments per tender; `0` = all |
| `--order` | recent | `recent`, `oldest` or `spread` |
| `--include-zip` | off | also fetch `.zip` appendix bundles |
| `--jobs` | 4 | parallel downloads |

Writes `data/docs/<unspsc4>/<reference>/`, `reports/docs_manifest.csv` and
`reports/category_counts.json`.

### `03_inspect_specs.py` — can the specs be parsed?

```bash
python 03_inspect_specs.py
python 03_inspect_specs.py --refresh    # after changing extraction settings
```

Extracts text (pdfminer for PDF, stdlib `zipfile` for `.docx`/`.xlsx`), counts
numeric requirement lines and constraints, previews both brand-naming and
exact-value signals, and prints a go/no-go verdict per category.

Caches text under `reports/text/`, so re-measuring is instant. **Pass
`--refresh` whenever extraction settings change**, or it re-reads stale text.

Writes `reports/spec_inspection.csv`, `reports/spec_inspection_summary.txt` and
`reports/spec_samples.txt` — read the samples file, it is the evidence that
parsing works.

---

## Where things stand

Phase 2 is closed. Measured on 930 attachments from 300 tenders:

| group | spec-rich rate | projected comparable peers | |
|---|---|---|---|
| 2510 motor vehicles | 75.0% | ~145 | PASS |
| 4110 lab & scientific equipment | 63.3% | ~292 | PASS |
| 4111 spectroscopic equipment | 38.3% | ~82 | PASS |
| 5610 furniture | 18.3% | ~81 | PASS |
| 2520 aircraft tires | 15.0% | ~26 | out of scope |

**The Spec Auditor agent is buildable.** Only 0.8% of documents are scans, so
OCR is not needed. 2520 is excluded because tires are specified by standardized
size codes, not numeric thresholds — there is nothing for a within-category
outlier test to compare.

Two findings that shape the agent design:

- **The specification lives in the solicitation body, not in an annex file.**
  Documents named `spec`/`annex` are 10.3% spec-rich; those named
  `rfp`/`rfq`/`itq` are 32.6%.
- **Never cap pdfminer pages.** The technical requirements sit at the *back* of
  a solicitation. A 40-page cap truncated 18% of documents and made every
  category look unbuildable.

---

## Data source

CanadaBuys open data, no API key, bilingual EN/FR.
Updated **daily** — new notices every 2 hours (06:15–22:15 EST), full files
refreshed each morning 07:00–08:30 EST.

Two gotchas worth knowing before you parse anything:

- Attachments on `sscp2pspc.ssc-spc.gc.ca` return an 86 KB HTML login page, not
  a file. Filter to `canadabuys.canada.ca`.
- `Content-Type` is `application/octet-stream` for everything. Identify files by
  magic bytes (`%PDF`, `PK`), not by header.

---

## Layout

```
01_profile_tenders.py
02_fetch_docs.py
03_inspect_specs.py
data/        not in git — tenders.csv (185 MB), docs/
reports/     not in git, except selected_unspsc4.json
```

`data/` and `reports/` are gitignored. Nobody commits a 185 MB CSV.
