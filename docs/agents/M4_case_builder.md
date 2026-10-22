# M4 — Case Builder

**Code:** `agents/m4_case_builder.py` · **Owner:** _(to assign)_

## Role
Takes what M1, M2 and M3 found about one tender, checks that every claim can be verified, scores it, decides **alert / watch / close**, and writes the case page the auditor reads.

## Goal
A ranked queue an auditor can trust: every line in a case points to a source a person can open.

## Input
The three results: `m1` (notice signals), `m2` (document findings), `m3` (award and history).

## Tools
None external. One extra check of its own: **is the brand the specification requires the company that won?** (`brand_matches_winner`).

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
- A claim without a source a person can open is shown as unverified and scores 0.

Points are an ordering, not a probability — there is no ground truth yet to fit them.

## Output
`build_case(m1, m2, m3)` returns:
- `route`, `score`, `breakdown`
- `claims` — each with a plain sentence (`plain`), its nature (*Declared by the buyer / Found in the documents / Context / Award history*), points, source, verified or not
- `narrative` — the case page text: why it is listed, what could not be checked, the supplier's history
- `award`, `trace`

The orchestrators (`tenderlens.py`, `tenderlens_stream.py`) write all cases to `…_queue.html / .csv / .json`.

## Run it
Through the orchestrators (`python tenderlens.py --tender <ref>`). Rules tested by `python tests/test_agents.py`.

## Rules it must keep
- Never writes "fraud". Strongest wording: *"the competition on this contract does not appear to have been genuine, for these reasons, at these locations."*
- Declared signals and context never make an alert, and never make a watch on their own.
- Every change of routing gets a constant that restores the old rule (`ALERT_FROM`, `DECLARED_ALONE`, `CONTEXT_ALONE`, `NARRATIVE`).
