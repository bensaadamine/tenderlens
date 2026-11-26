# M4 — Coordinator (agent 4)

**Code:** `agents/m4_case_builder.py` 

## Role
The coordinator of the chain. Takes what M1, M2 and M3 found about one tender, has agent 2 check the winner's other spellings, shows the winner's other awards, checks that every claim can be verified, scores it, decides **alert / watch / close**, and writes the case the auditor reads. It has no model of its own.

## Goal
A ranked queue an auditor can trust: every line in a case points to a source a person can open.

## Input
The three results: `m1` (notice signals), `m2` (document findings), `m3` (award and history).

## Tools
Rules (`m4_case_builder.py`): one check of its own, **is the brand the specification requires the company that won?** (`brand_matches_winner`), against the winner's name and the other spellings M3 accepted as the same company.

Checks (`m4_checks.py`, `--m4-checks`): before ranking a case **that has a brand finding**, two checks run in a fixed order, and `build_case` re-scores the case after them:

| check | asks | can change |
|---|---|---|
| 1. spellings | the M3 agent checks the winner's other spellings; M3's counts redone (skipped when `--m3-agent` already did) | brand matches a spelling → alert; recurring supplier (context) |
| 2. winner's awards | titles of the winner's other awards mentioning each brand | nothing — shown, never scored |

No brand finding → nothing runs.

What stays in code: all scoring and routing (`build_case`); M2's and M3's own checks; the case-page text ("Checked again before ranking", written from the check results). The checks add no claim and set no points.

**History of the checks.**
- Until 2026-11-26 a first check sent each brand finding back to the M2 agent for a second read. The system always runs with the M2 agent, which already letters every finding, so that re-read had nothing to do: removed (user decision).
- It mattered only with M2 on rules alone. Measured there (`tests/m4_eval.py`, 73 labelled tenders, 2026-11-04): rules only 9 flagged, 7 false; every check in a fixed order 2 flagged, 0 false; a model choosing the checks 4 flagged, 2 false (it skipped two re-reads). That is why the order is fixed and not chosen by a model (removed 2026-11-24).
- M4 does not find lock-ins the rules missed; that is the M2 agent's job (8 of 8 on the held-out set).

## How it decides
| Claim | Points |
|---|---|
| brand without equivalent (documents) | 3 (max 2 brands counted) |
| brand = winner | 3 |
| advance award notice / limited method (declared) | 2 each |
| short bidding window | 1 |
| no trade agreement (context) | 1, ranks only |
| recurring / dependent supplier (context) | 1 / 2, only if a document finding exists |
| level 2 (low confidence) | 1, max 2 |

- **Alert:** 5 points **from document findings alone** — two brands, or a brand that won.
- **Watch:** 2 points **without counting context**, and at least one claim that is neither declared by the buyer nor context.
- **Not an audit case:** an advance contract award notice closes, whatever its score (user decision 2026-11-24).
- **Close:** everything else. The score is kept for ranking.
- A claim scores only if its source is **attached** (an origin and the words) **and checked** — the words found in the document by M2 (or by the M2 agent's quote test), or copied by code from the notice field (M1) or the award records (M3). Otherwise it is shown as unverified and scores 0. Each claim carries `source_attached` and `verified`.
- Brand = winner is checked against every supplier the award notices name for the tender, not only the newest.

Points are an ordering, not a probability — there is no ground truth yet to fit them.

## Output
`build_case(m1, m2, m3)` returns:
- `route`, `score`, `breakdown`
- `claims` — each with a plain sentence (`plain`), its nature (*Declared by the buyer / Found in the documents / Context / Award history*), points, source, verified or not; the auditor's page links each claim's kind to its rule of law (`tools/laws.py`)
- `narrative` — the case page text: why it is listed, what could not be checked, the supplier's history
- `followups` — what M4 checked again; `m4_checks` — mode, number of checks, note
- `award`, `trace` (with `M4` when the checks ran)

The pipeline (`pipeline/one_tender.py`, `pipeline/stream.py`, through `tools/report.py`) writes all cases to `…_queue.html / .csv / .json`.

## Run it
Through the pipeline (`python tenderlens.py tender <ref>`, `python tenderlens.py run --group <code>`). Add `--m4-checks` for the checks (LLM, Windows). Without the flag, M4 is rules only. No model answering → the run stops and resumes from that tender (`Settings(on_outage="keep")` ranks the case as it stands).

Tests (no data, no network): `python -m tests.test_agents` (rules), `python -m tests.test_m4_checks` (the checks, fake M2/M3).

## Rules it must keep
- Never writes "fraud". Strongest wording: *"the competition on this contract does not appear to have been genuine, for these reasons, at these locations."*
- Declared signals and context never make an alert, and never make a watch on their own.
- A change of routing gets a switch restoring the old rule until it is measured; settled switches are removed (git keeps them).
- The checks only ask; code decides. A check's result changes the case only through `build_case`.
