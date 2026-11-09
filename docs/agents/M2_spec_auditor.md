# M2 — Spec Auditor

**Code:** `agents/m2_spec_auditor.py` (the auditor) · `agents/m2_agent.py` (its agent) · `tools/brands.py` (brand rules) · `tools/documents.py` (the tender's text) · `tools/wikidata.py`

## Role
Reads the **specification documents** of one tender and looks for requirements written so that only one product can win.

## Goal
Find, with a quoted line, a departure from the trade-agreement rule: *a specification may name a brand only if it also accepts an equivalent.*

## Input
- `reports/text/<group>/<reference>__<document>.txt` — text extracted by `prepare/extract_text.py`.
- The knowledge base of past tenders (for level 2).
- The run flags `--online`, `--agent`.

## How it works
1. **Rules find candidates** (`tools/brands.py`): brand-like names next to a model code, **in the requirements section only** (whole document when no section is found), outside bidders' questions. Measured on the 76 labelled cases: 0 of 12 lock-ins lost, 10 of 46 junk names removed; synthetic set unchanged (64/160, 17 alerts, 0/120 wrong); on 4110 offline only the two "Net 30" false findings went away. Names Wikidata knows are not companies are dropped. A name is cleared for good only when the text accepts an equivalent ("or equivalent", or the standard "Equivalent Products … form, fit, function" clause).
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
3. **Level 2** compares numeric requirements with the same product group (8-digit code); it speaks only when an attribute has 8 comparable tenders. Dormant today (finds nothing usable); `--no-level2` skips it everywhere — no numbers extracted, no peers compared — and changes no route.

## Tools the agent chooses from
| Tool | Gives |
|---|---|
| `search` | every line containing a text, bidders' questions marked |
| `read` | the lines around a line |
| `equivalents` | every sentence of the tender accepting an equivalent |
| `wikidata` | whether a name is a company (cached) |
| `classify` | letters + quotes for one or several candidates |
| `finish` | stop; unlettered candidates stay unresolved |

**Shown before its first step, by code:** what the tender buys (the notice title, the document names) and every sentence saying the buyer already has or uses something ("existing", "procured by NRC"…). Its instructions carry the supply test: "f" only if the bidder must supply that brand's product; if the purchase must connect to it, be controlled by it or be based on it, that is "c".

**How the model calls them:** native tool calling, one schema per tool (`llm.tool`); a plain JSON reply still works.

**Who settles each name is recorded** (`decided_by`, shown on the case page under "Brand names checked"). The rules settle what the text decides exactly: a name dropped as the tender's own acronym, part of a web address, or not a brand on Wikidata; a name cleared by "or equivalent", the Equivalent Products clause, the buyer's own equipment, a list of makes, or the buyer's computer environment. Everything else is for the agent. The rule clearances stay with the rules on measurement: when the agent re-checked the buyer's-equipment clearances, it turned 3 correct ones into false findings (2026-10-22).

**One brand, several spellings, one finding:** a spelling joins a shorter finding whose words it begins with ("FormLabs Form" joins "Formlabs"; "General Electric" and "General Motors" stay two). The others are listed as `also_written`, and each keeps its own place and quote in the finding's evidence. Measured 2026-11-08: only the Formlabs demo case changes (alert → watch); synthetic set, 73 labelled tenders and 4110 unchanged.

**Kept in code, never left to the model:** every verdict must quote text really in the named document (the line recorded is where the quote is); an "f" quote must contain the brand; a refused verdict is explained back to the model; a candidate without a valid letter is **unresolved** — neither a finding nor a clearance.

## Output
`audit(reference, ...)` returns:
- `findings` — each with `kind` (`brand_without_equivalent`, `narrow_band`, …), `claim`, `confidence`, `source` {document, location, quoted line}
- `level1` — counts: findings, cleared, unresolved
- `names` — every brand name the tender raised: `result` (finding / open / cleared / dropped) and `decided_by`
- `level2` — score, attributes without enough peers
- `readable` — False if no text exists
- `trace` — every step, including each agent step (tool, arguments, result)

## Run it alone
```bash
python -m agents.m2_spec_auditor --tender cb-425-92729935 --online --agent
python -m tools.brands --tender cb-425-92729935 --offline          # rules only
```

## How it is checked
- `python -m tests.test_m2_agent` — the agent's rules with a scripted fake model (no network).
- `python tenderlens.py eval m2 [--agent --online] [--split holdout]` — the labelled names: 36 tuning (`tests/m2_labels.csv`) + 40 held-out (`tests/m2_holdout.csv`). Held-out (37 names, 2026-11-08, Groq only): rules find 1 of 8 lock-ins; with the agent 8 of 8, 8 of 10 findings real, 35/37 right. (The earlier 36/37 mixed two models: 17 of its answers came from Gemini.)
- `python tenderlens.py eval synth --run [--agent]` — synthetic rigged tenders.

## Rules it must keep
- Every finding quotes its line; no quote, no finding.
- An unconfirmed name is neither a finding nor a clearance.
- A new detection rule gets a switch restoring the old behaviour until it is measured; settled switches are removed, their history is in git.

## Open work
- Run `tests/m2_eval.py --agent --online` on Windows and tune the agent's instructions on its mistakes — not the rules.
- Cost: about 2–4 model calls per tender with candidates; tenders with the general equivalence clause need none.
