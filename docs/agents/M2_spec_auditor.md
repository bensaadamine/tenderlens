# M2 — Spec Auditor

**Code:** `agents/m2_spec_auditor.py` (the auditor) · `agents/m2_agent.py` (its agent) · **Owner:** _(to assign)_

## Role
Reads the **specification documents** of one tender and looks for requirements written so that only one product can win.

## Goal
Find, with a quoted line, a departure from the trade-agreement rule: *a specification may name a brand only if it also accepts an equivalent.*

## Input
- `reports/text/<group>/<reference>__<document>.txt` — text extracted by `prepare/03_inspect_specs.py`.
- The knowledge base of past tenders (for level 2).
- The run flags `--online`, `--agent` (or the older `--llm`).

## How it works
1. **Rules find candidates** (`tools/spec_tools.py`): brand-like names next to a model code, outside bidders' questions. Names Wikidata knows are not companies are dropped. A name is cleared for good only when the text accepts an equivalent ("or equivalent", or the standard "Equivalent Products … form, fit, function" clause).
2. **The agent judges the rest** (`--agent`, `agents/m2_agent.py`). Goal: give every remaining candidate the auditor's letter, with a quote:

   | | |
   |---|---|
   | a | not a brand (acronym, ordinary word, standard, URL, date, payment term, material grade) |
   | b | a bidder is speaking |
   | c | the buyer's own equipment |
   | d | one of several brands listed |
   | e | an equivalent is accepted |
   | **f** | **brand required, no equivalent → the only letter that becomes a finding** |

   It runs a loop (LangGraph): **decide** (the LLM picks a tool) → **act** (the tool runs) → decide… until every candidate has a letter, it says `finish`, or 8 steps are used.
3. **Level 2** compares numeric requirements with the same product group (8-digit code); it speaks only when an attribute has 8 comparable tenders.

## Tools the agent chooses from
| Tool | Gives |
|---|---|
| `search` | every line containing a text, bidders' questions marked |
| `read` | the lines around a line |
| `equivalents` | every sentence of the tender accepting an equivalent |
| `wikidata` | whether a name is a company (cached) |
| `classify` | letters + quotes for one or several candidates |
| `finish` | stop; unlettered candidates stay unresolved |

**Kept in code, never left to the model:** every verdict must quote text really in the named document (the line recorded is where the quote is); an "f" quote must contain the brand; a refused verdict is explained back to the model; a candidate without a valid letter is **unresolved** — neither a finding nor a clearance.

## Output
`audit(reference, ...)` returns:
- `findings` — each with `kind` (`brand_without_equivalent`, `narrow_band`, …), `claim`, `confidence`, `source` {document, location, quoted line}
- `level1` — counts: findings, cleared, unresolved
- `level2` — score, attributes without enough peers
- `readable` — False if no text exists
- `trace` — every step, including each agent step (tool, arguments, result)

## Run it alone
```bash
python agents/m2_spec_auditor.py --tender cb-425-92729935 --online --agent
python tools/spec_tools.py --tender cb-425-92729935 --offline      # rules only
```

## How it is checked
- `python tests/test_m2_agent.py` — the agent's rules with a scripted fake model (no network).
- `python tests/m2_eval.py [--agent --online]` — the 36 cases judged by hand (`tests/m2_labels.csv`). Rules alone: **10 of 11 findings false, 1 of 4 real lock-ins found**.
- `python tests/synth_rig.py --run [--agent]` — synthetic rigged tenders.

## Rules it must keep
- Every finding quotes its line; no quote, no finding.
- An unconfirmed name is neither a finding nor a clearance.
- Every change to detection gets a flag that restores the old behaviour (`--l1-...` flags in `spec_tools.add_l1_args`; `--agent` off = rules only).

## Open work
- Run `tests/m2_eval.py --agent --online` on Windows and tune the agent's instructions on its mistakes — not the rules.
- Cost: about 2–4 model calls per tender with candidates; tenders with the general equivalence clause need none.
