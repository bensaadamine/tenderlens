# TenderLens — plan (updated 2026-11-25, deadline 2026-11-26)

## What we present
An assistant for a federal auditor: it clears most purchases with quoted evidence and builds a case file for the few worth a look, each reason tied to its rule of law. Four agents, one presenter each:

| Agent | Decides | Measured |
|---|---|---|
| 1. Spec auditor (M2) | a brand required without "or equivalent"? | held-out 34 of 35 |
| 2. Supplier identity (M3) | are these spellings one company? | stored decisions, auditor can overrule |
| 3. Split-purchase agent | one need cut into contracts under $25,000? | rules alone 10 of 15 against blind labels; agent to measure |
| 4. Coordinator (M4) | alert / watch / close, after the winner's spellings are checked | routing rules from measured errors; a model choosing the checks was worse (2 false alarms vs 0) |

Around them: M1 (rules, the notice) and the rules assistant (local Gemma, the law texts).

## Done 2026-11-25
- The claim changed: not "too many tenders for a human" (about 5 readable a week in our families) but clearing with evidence; rare signals are a finding.
- Limited tendering: only the procedure counts; the reason field is not read (decision 51).
- Split-purchase agent, its tests, blind labels and page (decision 52).
- Lists the buyer calls approved products clear a brand; a hit's line is found from its position in the raw text (decision 29).
- The rules of law behind each signal, linked from every case; the rules assistant on a local model (decision 53).
- The auditor's page redesigned: top bar with four places (To decide, Families, Rules, How it works), one list of everything to decide, the tender's own words marked as evidence.

## Left (needs the Groq quota, run on Windows)
1. `python tenderlens.py splits --agent --redo --labels tests/split_labels.csv` — agent 3 again, with the specific-quote rule (first run: 9 of 12, 2 false splits).
2. `python tenderlens.py run --group 4111 --reprocess cb-981-22811944 --online --agent --m3-agent --m4-checks` — the 4111 false alert should close.
3. `python tenderlens.py eval synth --run --online --agent --variants A,G,D` — the agent's 5 wrong clearances.
4. `python tenderlens.py eval m2 --agent --online --split sample` — agent 1 on the random sample.

Report and slides: another team member.

## Dropped, with reasons
- Lead and critic agents: the lead's planning would be fake (always run every check), and a model choosing the checks was already measured worse; a critic built to clear findings would clear real ones (the agent already wrongly cleared 5 inserted brands).
- Contract value growth after the award: the award file keeps one row per contract, earlier values are overwritten.
- Complaints to the trade tribunal as ground truth: about 70 a year for all federal purchasing, mostly services; too few in our goods families. Future work.
- Rebuilding the knowledge bases with the new agents: they feed only level 2 (dormant); about a week of quota.
- A database for the knowledge bases: 21 MB for 6 families (about 900 cases), every page in 0.15–0.3 s, append-only files that a crash cannot corrupt, and verdicts already wait while a run writes. SQLite would pay off only at a much larger scale. Future work.

## Earlier plan (2026-11-01), all done
Native tool calling for the agents; brand names looked for in the requirements section only; the buyer's own equipment shown to the agent before it starts; M3 decisions per group of spellings, no chained merges; M4 measured (fixed order kept); `--reprocess`. The local model added then for fact extraction was removed on 2026-11-08 (no caller); the rules assistant brings a local model back for a different job.
