# Split-purchase agent (agent 3)

**The rule:** a buyer may not divide procurement requirements to avoid the obligations of the trade agreement (Canadian Free Trade Agreement, art. 503); a goods contract may be given without bids only up to $25,000 (Government Contracts Regulations, s. 6).

**Code:** `agents/split_agent.py` · tests `tests/test_split_agent.py` · labels `tests/split_labels.csv`

## Role
Reads the **award file** and looks for one need cut into several contracts, so that each piece stays under the limit for buying without competition.

## Goal
A goods contract may be given to a supplier without bids only if it is **$25,000 or less** (Government Contracts Regulations, s. 6(b)(i)). Cutting one need into pieces under that limit skips the competition the rule requires. The agent decides, for each group of contracts, whether it is one need cut into pieces.

## Why it matters
- It needs **no tender documents**: it works for every goods category, including the non-competitive contracts M2 never reads.
- It is real judgement: two furniture contracts a week apart can be one office fit-out cut in two, or two unrelated orders. Titles, reference codes, end users and the buyer's habits decide it; a date-and-amount rule cannot.

## Input
`data/awards.csv` (one record per contract: its newest amendment).

## How it works
1. **Rules find the groups** (code, exact): same buyer, same supplier, non-competitive goods awards of $25,000 or less each, the later ones within 30 days of the first, together over $25,000. In all goods: **15 groups** (2026-11-25), almost all office furniture bought by Public Works. The code also computes the facts: total, days, awards on the same day, awards at 95% of the limit or more, a reference code repeated in two titles.
2. **The rules' own verdict** (to compare with the agent): a code in two titles, or two awards on one day → split.
3. **The agent judges each group:** `split` (one need cut into pieces), `separate` (different needs close in time), or `unclear`. LangGraph loop (`agents/agent_loop.py`, shared with M2 and M3), 5 steps; the last call has no tools.

## Tools the agent chooses from
| Tool | Gives |
|---|---|
| `history` | every award from this buyer to this supplier, any method, any value |
| `habit` | how often this buyer gives *any* supplier a non-competitive goods award near the limit (a buyer that does it with everyone is not favouring one) |
| `detail` | one award in full: description, end user, place, dates |
| `decide` | the verdict, a one-sentence reason, and for `split` a quote |
| `finish` | stop without a verdict |

## Kept in code, never left to the model
- The groups, every number and every date; the model never computes.
- `split` needs a **quote found word for word in two or more of the group's own titles or descriptions**: the words that make it one need. Otherwise it is refused and the model is told why.
- A `split` routes to **watch, never alert**: the award file cannot show intent; the auditor opens the files.
- Every verdict is stored in `reports/splits/verdicts.csv` and never asked again. **The auditor's verdict decides the route.**

## On the auditor's page
Groups a rule or the agent calls split appear in **To decide**, with the cases. The **Split purchases** page shows each group as a table of its contracts (date, amount, title, contract number), the rules', the agent's and the auditor's verdict, the agent's reason and steps, and **Split** / **Separate** buttons.

## How it is checked
- `python -m tests.test_split_agent`: the rules and the agent's code checks with a scripted fake model (no network).
- `python tenderlens.py splits --agent --labels tests/split_labels.csv`: rules vs agent against labels. **The labels were written blind** by a separate agent that saw only the groups, never the rules' or the agent's answers: 2 split, 10 separate, 3 unclear.
- Rules alone: **10 of 15 as labelled, 3 false splits, 0 missed.** Agent: to be measured (needs the Groq quota).

## Run it
```bash
python tenderlens.py splits                                   # groups + rules
python tenderlens.py splits --agent --labels tests/split_labels.csv   # + agent (Groq)
python tenderlens.py splits --families 5610                   # one family
python tenderlens.py splits --export reports/splits/to_label.md       # groups only, for labelling
```

## Known limits
- Only contracts published in the award file are seen; many contracts under $25,000 are never published there, so the true number of splits is unknown.
- 30 days and "same supplier, same buyer" are choices: a split across two suppliers, or over two months, is not seen.
- 15 groups is a small set: the measurement shows how the agent reasons, not a precise rate.
