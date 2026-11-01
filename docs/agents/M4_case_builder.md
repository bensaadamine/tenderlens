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

Agent (`m4_agent.py`, `--m4-agent`): before ranking a case **that has a brand finding**, it chooses follow-up questions:

| tool | asks | can change |
|---|---|---|
| `recheck {"brands": [...]}` | the M2 agent re-reads those findings (a–f letter, verified quote) | non-f letter → finding removed; no letter → unresolved |
| `spellings {}` | the M3 agent checks the winner's other spellings; M3's counts redone | brand matches a spelling → alert; recurring supplier (context) |
| `winner_awards {"text"}` | titles of the winner's other awards containing the text | nothing — shown, never scored |
| `finish {}` | stop | — |

Loop: decide → act → re-score with `build_case` → decide; stops on finish, after 4 steps, or when no follow-up is left. Each follow-up runs once; findings already lettered by `--agent` and spellings already checked by `--m3-agent` are not offered. No brand finding → no model call. LangGraph, plain loop as fallback.

What stays in code: all scoring and routing (`build_case`, unchanged); M2's and M3's own checks; the case-page text ("Checked again before ranking", written from the follow-up results). The model adds no claim and sets no points.

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
- `followups` — what the M4 agent checked again; `m4_agent` — mode, steps, stop reason
- `award`, `trace` (with `M4` when the agent ran)

The orchestrators (`tenderlens.py`, `tenderlens_stream.py`) write all cases to `…_queue.html / .csv / .json`.

## Run it
Through the orchestrators (`python tenderlens.py --tender <ref>`). Add `--m4-agent` for the agent (LLM, Windows), or `--m4-agent fixed` to run every follow-up without the model choosing (baseline). Without the flag, behaviour is unchanged. No model answering → the run stops and resumes from that tender (`m4_agent.AGENT_DOWN = "keep"` ranks the case as it stands).

Tests (no data, no network): `python tests/test_agents.py` (rules), `python tests/test_m4_agent.py` (agent, scripted fake model).

## Rules it must keep
- Never writes "fraud". Strongest wording: *"the competition on this contract does not appear to have been genuine, for these reasons, at these locations."*
- Declared signals and context never make an alert, and never make a watch on their own.
- Every change of routing gets a constant that restores the old rule (`ALERT_FROM`, `DECLARED_ALONE`, `CONTEXT_ALONE`, `NARRATIVE`, `BRAND_VS_SPELLINGS`).
- The agent only asks; code decides. A follow-up result changes the case only through `build_case`.
