# M1 — Screener

**Code:** `agents/m1_screener.py` 

## Role
Reads only the **tender notice** (one row of the CanadaBuys feed) — never a document — and lists what the buyer itself declared about competition. It is cheap and runs on every tender.

## Goal
Say, in seconds, which tenders deserve the expensive work (reading documents, asking the LLM), and why.

## Input
- `data/tenders.csv` — the notice feed (one row per tender).
- `reports/text/` — only to know whether documents exist for the tender.

## What it looks for

| Signal | Meaning | Kind |
|---|---|---|
| Advance Contract Award Notice (ACAN) | the buyer announces in advance the supplier it will choose. Since 2026-11-24 an ACAN is **not an audit case**: the run records it and does not read it (`is_acan`, used by the gate) | declared |
| Limited-tendering method | the procedure itself skips open competition | declared |
| Short bidding window | days to respond, compared with **tenders run the same way** (not an absolute number). In **selective tendering** (only pre-qualified suppliers bid) it is shown but does not count | from the notice dates |
| No trade agreement | the rule against naming brands does not apply. Ranks a case, never puts it in the queue | context |

## Tools
None external. Statistics on bidding windows are computed once over the whole feed (`method_windows`).

## Output
`screen(reference)` returns a dict:
- `signals` — each with `signal`, `weight` (strong / weak), `why`, `evidence` (the notice field)
- `route` — `deep` (send to M2 with the LLM), `watch`, `close`, or `deep_unreadable` (no documents)
- `documents`, `title`, `entity`, `method`, `published`, `window_days`

The route only decides **how deep M2 goes**. Every tender with documents is read by M2 anyway.

## Run it alone
```bash
python -m agents.m1_screener --tender cb-425-92729935
python -m agents.m1_screener --all --group 2510 --limit 25
```

## Rules it must keep
- Never judges a tender rigged — only lists declared facts with their source.
- A bidding window is always compared with its own procurement method.

## Open work
- The limited-tendering reason field is not read: limited tendering never carries a filed reason in the data (0 of 232), so its absence says nothing (decision 51).
- The `watch` route has no effect on the final score.
