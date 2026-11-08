# TenderLens — work plan (agreed 2026-11-01)

Hybrid of the old and new designs: keep the code, fix what the audit and the
design review found. Every behaviour change gets a flag restoring the old.

| # | Step | What | Status |
|---|---|---|---|
| 1 | Tool calling (all agents) | The model calls tools through the provider's native tool calling, with a schema per tool; plain JSON stays the fallback. Measured: about half the agent steps were lost on tools that don't exist or wrong arguments. | done 2026-11-01 |
| 2a | M2: names from the requirements section | Candidate brand names are found only in the requirements section (whole document when none is detected). Judging still reads the whole tender. Measured on 76 labelled cases: 0 of 12 lock-ins lost, 10 of 46 junk names removed. | done 2026-11-01 |
| 2b | M2: the buyer's own equipment | ~~Local-model facts from outside the requirements section~~ — built, then removed 2026-11-04: the deciding sentences sit inside the requirements section, and the agent did not call the tool. Replaced by code-shown context (what the tender buys, the buyer's-equipment sentences) and the supply test in the agent's instructions. | done 2026-11-04 |
| 3 | M3: two fixes | Stored decisions apply to the whole group of spellings (no re-asking when a merged spelling wins). A merge must share a postal code with the winner itself (no chains). | done 2026-11-01 |
| 4 | M4: measurement | `tests/m4_eval.py` on 73 labelled tenders: `fixed` 0 false alarms (70k tokens), `model` 2 (14k), rules only 7. `fixed` is the default since 2026-11-04. | done 2026-11-04 |
| 5 | Reprocessing (audit fix 4) | `--reprocess` re-runs named tenders, or all of quarantine; each batch records its settings. Tenders with an auditor verdict are not re-judged. | done 2026-11-01 |
| 6 | Local model | Ollama provider in `tools/llm.py`, used only when asked (fact extraction), never in the shared pool unless enabled. | done 2026-11-01 |
| 7 | Windows runs | `tests/m2_eval.py --online` and `--agent --online` (held-out score); `tests/m4_eval.py`. | user |

Not changing: M1 (rules only), routing and weights, the knowledge base.

## Done 2026-11-01 — what was checked offline
- Tests: `tests/test_redesign.py` (23 checks, new) and every existing test file pass.
- Synthetic set with names from the requirements section: unchanged (64/160 found, 17 alerts, 0/120 clean copies flagged).
- 4110 offline, rules: 2 alert / 10 watch / 12 findings (was 2 / 12 / 14); the two removed are "Net 30" payment terms.
- Not measurable here (needs network or the local model): native tool calls against the providers, fact extraction quality, the agents' held-out score. Run step 7 on Windows.

## Settings (all default to the new behaviour)
| Setting | Old behaviour |
|---|---|
| `TENDERLENS_NATIVE_TOOLS=off` | tools described in the prompt only |
| `--l1-scope all` | brand names looked for everywhere |
| ~~`TENDERLENS_LOCAL_MODEL=gemma4:e4b`~~ | local model removed 2026-11-08 (no caller) |
| `m3_agent.CHAIN = True` / `GROUPS = False` | chained merges / per-winner decisions |
| `--reprocess REF… / quarantine / all` | (new option) |
