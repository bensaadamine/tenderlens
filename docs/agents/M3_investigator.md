# M3 — Investigator

**Code:** `agents/m3_investigator.py`, agent `agents/m3_agent.py`

## Role
Reads the **award notices** (who won each contract) and answers, for one tender: who won it, and is that a pattern for this buyer?

## Goal
Add the second half of the classic picture: a specification narrowed to one product (M2) **and** a buyer who keeps choosing the same supplier.

## Input
- `data/awards.csv` — CanadaBuys award notices, downloaded once with `--fetch` (on Windows).
- The tender's notice row (to find its solicitation number).

## Tools
An in-memory graph built once from the award file: buyer + category → awards, buyer + supplier + category → awards, supplier → buyers. No database.

## Steps
1. **Depth 1, always:** find the award; count this buyer's awards in the category and the winner's share.
2. **Depth 2, only if** the winner got **≥ 50 % of ≥ 3** awards: look at the supplier's other buyers.
   - wins everywhere → market leader (`recurring_supplier`)
   - lives off this buyer → relationship (`dependent_supplier`)

## The M3 agent (`--m3-agent`, 2026-10-26)
The award file writes one company several ways ("Agilent Technologies" / "Agilent Technologies Canada", "Olympus NDT Canada" / "Evident Canada" after a rebrand), and different companies share one address. Counting spellings apart hides a recurring winner; merging two companies invents one. The agent decides, for the winner, which other spellings are the same company.

- **Tools:** `variants` (other names at the same postal code or with the same first word, with city and award count), `awards` (what a name won), `decide`, `finish`. LangGraph loop, 6 steps; the last call has no tools.
- **Only when it matters:** offered spellings are those that also won from this buyer in this category (they change the share); at depth 2, all spellings. Measured: 18 winners in 2510, 37 in 4110.
- **Kept in code:** only names the tools showed can be decided; "same" needs a shared postal code with the winner (or an accepted spelling), or names equal once generic words are dropped ("Canada", "Group"…); a joint venture is never the same as a partner; the counts are computed in code.
- **Stored:** every decision goes to `reports/supplier_aliases.csv` (`winner, name, verdict, reason, decided_by, date`) and is never asked again. **The auditor can overrule** by adding a row with `decided_by = auditor`.
- The case page says which spellings were counted together.
- No model (quota used): the run stops and resumes later, as for M2.

## Output
`investigate(reference, notice_row, graph)` returns:
- `award` — supplier, buyer, buyer's awards in the category, winner's count and share, number of suppliers
- `findings` — `recurring_supplier` / `dependent_supplier`, always labelled **context**
- `trace`

## Run it alone
```bash
python agents/m3_investigator.py --fetch      # once, on Windows
python agents/m3_investigator.py --columns    # check the detected columns
python agents/m3_investigator.py --tender cb-425-92729935
```

## Rules it must keep
- Market concentration is **context, never evidence**: it adds to a case with a document finding and adds nothing alone.
- Column names are detected from the file header, never assumed.

## Open work
- Only 32 % of notices match an award; the rest not investigated.
- Spelling variants: handled by the M3 agent (above); its decisions need an auditor's look the first time (`reports/supplier_aliases.csv`).
