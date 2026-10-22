# M3 — Investigator

**Code:** `agents/m3_investigator.py` · **Owner:** _(to assign)_

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
- Supplier names are merged only by legal suffix; spelling variants ("Tyco" written 5 ways) split the counts. Planned: narrow merging, proposed by the LLM, approved by a person.
