# M2 — Spec Auditor

**Code:** `agents/m2_spec_auditor.py` 

## Role
Reads the **specification documents** of one tender and looks for requirements written so that only one product can win.

## Goal
Find, with a quoted line, a departure from the trade-agreement rule: *a specification may name a brand only if it also accepts an equivalent.*

## Input
- `reports/text/<group>/<reference>__<document>.txt` — text extracted by `prepare/03_inspect_specs.py`.
- The knowledge base of past tenders (for level 2).
- `depth` from M1 and the run flags `--online` / `--llm`.

## Tools
| Tool | File | Does |
|---|---|---|
| Level 1 — brand without equivalent | `tools/spec_tools.py` → `brands_without_equivalent()` | finds brand-like names next to a model code, checks Wikidata, checks for "or equivalent" anywhere in the tender, optionally asks the LLM |
| Level 2 — tighter than peers | `tools/level2.py` → `tight_requirements()` | compares numeric requirements with the same product group (8-digit code); speaks only when an attribute has 8 comparable tenders |
| Wikidata | inside `spec_tools.verify_brand()` | is this name a company? cached in `reports/brand_cache.json` |
| LLM | `tools/llm.py` | triage of unconfirmed names, then a judge reading every mention in context |

## Steps
1. Level 1 offline (rules + cache).
2. With `--online`: same again, checking names on Wikidata.
3. With `--llm`: if names are still unconfirmed or a brand was found, ask the LLM.
4. Level 2 against the knowledge base.

A brand is **cleared** (not a finding) when the tender accepts an equivalent: next to it, above its table, or through Canada's standard "Equivalent Products … form, fit, function" clause; also when it is the buyer's own equipment, a list of several makes, the tender's own acronym, or text written by a bidder.

## Output
`audit(reference)` returns:
- `findings` — each with `kind` (`brand_without_equivalent`, `narrow_band`, …), `claim`, `confidence`, and `source` {document, location, quoted line}
- `level1` — counts: findings, cleared, unresolved
- `level2` — score, attributes without enough peers
- `readable` — False if no text exists
- `trace` — every step taken and why

## Run it alone
```bash
python agents/m2_spec_auditor.py --tender cb-425-92729935 --depth standard
python tools/spec_tools.py --tender cb-425-92729935 --offline
```

## Rules it must keep
- Every finding quotes its line; no quote, no finding.
- An unconfirmed name is neither a finding nor a clearance.
- Every change to detection gets a flag that restores the old behaviour (`--l1-...` flags in `spec_tools.add_l1_args`).

## Open work — **the priority of the project**
Rules + Wikidata produce too many false findings (4110: 9 of 11 false). Planned: rules only collect candidates; the LLM classifies each into the auditor's categories a–f (see README), measured against the cases already checked by hand.
