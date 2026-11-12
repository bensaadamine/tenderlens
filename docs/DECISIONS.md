# TenderLens — decisions

The code cites these by number ("decision 15"). Each is settled and measured; the numbers never change. Measurements behind them are summarised in the component pages (`docs/agents/`).

## Scope and output
1. The audience is the state auditor.
2. After the award only. Bids are confidential and not in our data.
3. Never output "fraud". Output: the competition does not appear genuine, for these reasons, at these locations. Every finding has a source line; a person decides.
4. Market concentration (M3) is context, not evidence.
5. No signal is absolute — each is measured against its own group.
6. Scope: the top 20 UNSPSC-4 goods categories (`reports/selected_unspsc4.json`).
7. Ground truth: synthetic rigged tenders measure recall; real complaints and cancellations prove relevance (not collected yet).
8. The headline metric is precision@50.
9. Part-number / size-code categories (e.g. 2520 aircraft tires) are out of scope.
10. The specification is in the solicitation body, in a named section (usually `ANNEX A`).
11. Rules find candidates; the M2 agent judges — except where a thing sits (25) and the exact text clearances (44).

## Level 2 — numbers against peer tenders
12. Level 2 is built depth-first on 2510 (motor vehicles); 4110 was rejected for it on measurement.
13. Peers are grouped by UNSPSC-8; level 2 stays silent for an attribute until it has 8 peers. Never widen the group for one finding.
14. Store observations, never statistics: medians and spreads are computed on read.
15. Units: convert or drop, never guess. Resolving from context is not guessing (24).
16. Track token usage (`reports/llm_usage.csv`); cache hits are logged at zero.
17. Exact-value comparators don't exist at scale (0.0% of 2510).
18. In a unit pattern, compound units come before their prefixes (`km/h` before `km`).
19. A bare dimension word (`length`) is never a canonical attribute name.
20. Two labels with different values in the same tender can never be merged.
21. Tender folders hold only buyer documents. Extraction is scoped by section, never by filename.
22. One observation per tender per attribute per value; in peer statistics a tender counts once (its median).
23. Parse numbers properly, never `replace(",", ".")`: three digits after a comma are thousands.
24. Every observation carries its section and ±3 lines of context, used to resolve ambiguity.
25. Where a requirement sits is decided by rule, not prompt: packaging, shipping, warranty sections are dropped.
26. Peer comparison finds nothing usable today; level 2 ships as the within-document test, low confidence.

## Routing and scoring
27. Only a verified brand finding can raise an alert: one brand → watch; two, or one that is the winner → alert, from document findings alone. What the buyer declared (advance award notice, limited tendering) only ranks a case. "No trade agreement" is context. M3 and level 2 add to a case, never make one. A claim scores only if its words were checked against its source. The weights are orderings, not probabilities.
28. M1 (rules) never decides whether a tender is read: every tender with documents goes to M2. A buyer's single-supplier justification is not checked by an agent.
29. Brand names are looked for in the requirements section only (whole document when none is found); the model code must follow the name; bidders' questions are skipped; in an amendment only change clauses, answers and restated requirements are read. "Or equivalent" and the Equivalent Products clause (its text, or its number B3000T alone) are checked against the whole text. An operating system or browser is the buyer's environment, never a finding.
30. Level 3 (a spec copied from a supplier datasheet) is dropped — there is no open datasheet corpus.

## How the system runs
31. Tender by tender against a growing knowledge base, in publication order: close → base (a peer for later tenders); alert or watch → quarantine (never a peer). The auditor's `clean` returns a tender to the base; `confirmed` keeps it out as a real test case. Each case is logged before its register line (crash-safe).
32. The agents (M2, M3) each have a goal, tools the model chooses, a loop with a step budget and a trace — one shared loop (`agents/agent_loop.py`). Safety rules stay in code. With no model answering, a run stops and the next run resumes there.
33. Tenders never awarded (cancelled, still open, RFI, supply arrangement) are read by M1 and M2 only and closed as "not an audit case". Several award rows: the newest is the award, every supplier listed.
34. One pinned model: Groq gpt-oss-120b answers every agent call. A run uses only answers from the models it may call (Groq; Gemini too with `TENDERLENS_SHARE=1`), cached ones included; an offline replay reuses any pinned model's answer, and every agent result says which model answered.
35. Interface: one command with subcommands, and a local page for the auditor (`python tenderlens.py serve`, FastAPI, built 2026-11-12): queue with clean / confirm, each case's evidence and agent steps, families to start, rules vs agents, agent activity, synthetic test. Its commands run in one of three modes: rules only, hybrid (agents live, uses quota) or hybrid replayed from the cache.
36. Share package (deprioritised): one zip of caches, knowledge bases and labels.
37. Evaluation reports a tuning set (cases rules or prompts were fixed on — flattered) and a held-out set (never used for tuning — the score to quote). Every run writes its own dated folder; never overwrite.
38. M3 agent: "same company" needs a postal code shared with the winner itself (no chains); a decision applies to the whole group of spellings.
39. M4's checks run in a fixed order on cases with a brand finding: the M2 agent re-reads each finding, the M3 agent checks the winner's spellings, the winner's other award titles are shown. A model choosing the checks was measured worse (2 false alarms vs 0) and is kept as an experiment.
40. M2 agent, the supply test: "f" only if the bidder must supply that brand's product; if the purchase must connect to it, be controlled by it or be based on it, it is "c". Before its first step the agent is shown what the tender buys and every sentence about equipment the buyer already has.
41. A hybrid multi-agent workflow: agents where judgement is needed (M2, M3), rules where exactness matters (M1, scoring, routing, quote checks), each choice measured. No model call is added just to make a component an agent.
42. Level 2 is kept, dormant, behind one switch (`--no-level2` skips its extraction and comparison).
43. The demo: seven chosen tenders, three setups side by side, replayable offline.
44. Who settled each brand name is recorded (`decided_by`): a rule, Wikidata, or the M2 agent. Rules keep the exact text clearances. One brand written several ways is one finding: a spelling joins a shorter finding whose words it begins with.
45. One command (`python tenderlens.py`), normal packages; what M2 and M3 hand to M4 is defined once (`agents/evidence.py`).
46. An offline replay never calls a provider: a missing answer is reported, and no key is needed.
47. One run's choices are one object (`pipeline/settings.py`), passed down as parameters; settled switches are removed.
48. Brand = winner needs every word of the brand, any length, in the winner's name (or an accepted spelling): a shared word is not identity ("General Electric" is not "General Motors", "GE Healthcare" is not "Siemens Healthcare").
49. Names are read one way (`tools/names.py`: words, any length, case and punctuation ignored), and each comparison states its own rule: merging spellings (a spelling begins with a shorter finding's words), M2's quote check (an "f" quote holds the whole name, word after word), brand = winner (48). No fuzzy matching.
50. The synthetic test (`eval synth`) runs M2 once per copy and reads each brand's outcome from that run; it reports its false-alarm guards apart (D: brand with "or equivalent", E: brand only in a bidder's question, F: untouched copy, judged on its route); its ranking is "rigged copies in the top 50", never called precision; each run keeps its own folder with settings and fingerprints. Whether a base already accepts equivalents can be decided by hand (`--review`) instead of by the detector's own patterns. The verdicts are kept when `--review` is run again.
