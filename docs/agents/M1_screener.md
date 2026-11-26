# M1 — Screener

**Code:** `agents/m1_screener.py` 

## Role
Reads only the **tender notice** (one row of the CanadaBuys feed) — never a document — and lists what the buyer itself declared about competition. It is cheap and runs on every tender.

## Goal
List, in seconds and with the notice field as evidence, what the buyer declared: these signals **rank** a case, they never put it in the queue. M1 is rules, not an agent: the notice says exactly what it says.

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
- `strong`, `weak` — how many signals of each weight (they only rank a case)
- `documents`, `title`, `entity`, `method`, `published`, `window_days`

M1 gives no verdict of its own: every awarded tender with documents is read anyway (decision 28). Its old route (deep / watch / close), which nothing used, was removed on 2026-11-26.

Each signal links, on the case page, to its rule of law: limited tendering to the Government Contracts Regulations s. 5–6 and the Canadian Free Trade Agreement art. 513; the bidding window to art. 511; "no trade agreement" to art. 504 (`tools/laws.py`).

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
