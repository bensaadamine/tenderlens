# M4 — Case Builder

**Code:** `agents/m4_case_builder.py` 

## Role
Takes what M1, M2 and M3 found about one tender, checks that every claim can be verified, scores it, decides **alert / watch / close**, and writes the case page the auditor reads.

## Goal
A ranked queue an auditor can trust: every line in a case points to a source a person can open.

## Input
The three results: `m1` (notice signals), `m2` (document findings), `m3` (award and history).

## Tools
Rules (`m4_case_builder.py`): one check of its own, **is the brand the specification requires the company that won?** (`brand_matches_winner`), against the winner's name and the other spellings M3 accepted as the same company (`BRAND_VS_SPELLINGS = False` restores the winner's name only).

Checks (`m4_checks.py`, `--m4-checks`): before ranking a case **that has a brand finding**, three checks run in a fixed order, and `build_case` re-scores the case after them:

| check | asks | can change |
|---|---|---|
| 1. re-read | the M2 agent re-reads each finding it has not lettered yet (a–f letter, verified quote) | non-f letter → finding removed; no letter → unresolved |
| 2. spellings | the M3 agent checks the winner's other spellings; M3's counts redone (skipped when `--m3-agent` already did, or no brand finding is left) | brand matches a spelling → alert; recurring supplier (context) |
| 3. winner's awards | titles of the winner's other awards mentioning each brand | nothing — shown, never scored |

No brand finding → nothing runs. With `--agent` every finding is already lettered, so check 1 has nothing to do: it matters in the cheap setup (M2 on rules, the model only on flagged cases).

What stays in code: all scoring and routing (`build_case`, unchanged); M2's and M3's own checks; the case-page text ("Checked again before ranking", written from the check results). The checks add no claim and set no points.

**Fixed order vs a model choosing, measured** (`tests/m4_eval.py`, 73 labelled tenders, M2 on rules only, 2026-11-04):

| mode | flagged | real lock-ins flagged | false alarms | agent tokens |
|---|---|---|---|---|
| off (rules only) | 9 | 2 of 12 | 7 | 0 |
| **fixed** (every check, fixed order) | 2 | 2 of 12 | **0** | 70k |
| model (the model chooses) | 4 | 2 of 12 | 2 | 14k |

- Re-reading every finding removes all 7 false alarms and keeps both real lock-ins.
- Letting the model choose saves tokens but it skipped two re-reads, and those false alarms stayed. So the main path is the fixed order; the model version is kept as a measured experiment (`experiments/m4_model_coordinator.py`, `--m4-checks model`: decide → act → re-score loop, 4 steps, LangGraph or plain loop).
- No mode finds a lock-in the rules missed: M4 only re-checks what is already flagged. Finding the others is the M2 agent's job (8 of 8 on the held-out set).

## How it decides
| Claim | Points |
|---|---|
| brand without equivalent (documents) | 3 (max 2 brands counted) |
| brand = winner | 3 |
| advance award notice / limited method / limited reason (declared) | 2 each |
| short bidding window | 1 |
| no trade agreement (context) | 1 |
| recurring / dependent supplier (context) | 1 / 2, only if a document finding exists |
| level 2 (low confidence) | 1, max 2 |

- **Alert:** 5 points **from document findings alone** — two brands, or a brand that won.
- **Watch:** 2 points, and at least one claim that is neither declared by the buyer nor context.
- **Close:** everything else. The score is kept for ranking.
- A claim scores only if its source is **attached** (an origin and the words) **and checked** — the words found in the document by M2 (or by the M2 agent's quote test), or copied by code from the notice field (M1) or the award records (M3). Otherwise it is shown as unverified and scores 0 (`VERIFY = "attached"` restores the old rule). Each claim carries `source_attached` and `verified`.
- Brand = winner is checked against every supplier the award notices name for the tender, not only the newest.

Points are an ordering, not a probability — there is no ground truth yet to fit them.

## Output
`build_case(m1, m2, m3)` returns:
- `route`, `score`, `breakdown`
- `claims` — each with a plain sentence (`plain`), its nature (*Declared by the buyer / Found in the documents / Context / Award history*), points, source, verified or not
- `narrative` — the case page text: why it is listed, what could not be checked, the supplier's history
- `followups` — what M4 checked again; `m4_checks` — mode, number of checks, note
- `award`, `trace` (with `M4` when the checks ran)

The pipeline (`pipeline/one_tender.py`, `pipeline/stream.py`, through `tools/report.py`) writes all cases to `…_queue.html / .csv / .json`.

## Run it
Through the pipeline (`python tenderlens.py tender <ref>`, `python tenderlens.py run --group <code>`). Add `--m4-checks` for the checks (LLM, Windows), or `--m4-checks model` for the experiment. Without the flag, M4 is rules only. No model answering → the run stops and resumes from that tender (`m4_checks.AGENT_DOWN = "keep"` ranks the case as it stands).

Tests (no data, no network): `python -m tests.test_agents` (rules), `python -m tests.test_m4_checks` (the checks, fake M2/M3), `python -m tests.test_m4_model_coordinator` (the experiment, scripted fake model).

## Rules it must keep
- Never writes "fraud". Strongest wording: *"the competition on this contract does not appear to have been genuine, for these reasons, at these locations."*
- Declared signals and context never make an alert, and never make a watch on their own.
- Every change of routing gets a constant that restores the old rule (`ALERT_FROM`, `DECLARED_ALONE`, `CONTEXT_ALONE`, `NARRATIVE`, `BRAND_VS_SPELLINGS`).
- The checks only ask; code decides. A check's result changes the case only through `build_case`.
