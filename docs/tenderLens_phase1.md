# TenderLens — Phase 1: Theme & Objectives

**Theme:** Legal / Contract Analysis
**Team:** 4 members · 12 data science objectives · 4 agents

---

## 1. Theme statement

Public bodies are legally required to buy through open competition. The law is precise: technical requirements must describe a *function*, not a product; naming a brand is forbidden unless followed by "or equivalent"; bidding deadlines have legal minimums; bidders must not coordinate.

These rules are broken in ways that leave a paper trail but no obvious mark on any single file. A specification written around one product looks like an ordinary specification. Two bidders controlled by the same person look like two bidders. The violation only becomes visible when one document is compared against thousands of others.

Audit bodies cannot do that comparison. A national audit office holds tens of thousands of contracts per year and inspects a few hundred. Selection is close to arbitrary.

**TenderLens reads published procurement records and returns a ranked audit queue: which contracts to inspect first, with the evidence already assembled.**

It does not decide who should win a contract, does not accuse anyone, and does not replace the auditor. It answers one question — *was the competition on this contract real, or staged?* — and hands a human a file that is ready to work on.

---

## 2. Business goals (SMART)

| # | Goal | Measured by | Target |
|---|---|---|---|
| **G1** | Rank real contracts by probability of irregular competition | precision@50 on the evaluation set | ≥ 40% |
| **G2** | Cut the volume needing expensive document analysis without losing real cases | % of corpus filtered out / % of known irregular cases retained | ≥ 80% filtered, ≥ 90% retained |
| **G3** | Make a flagged case fast to understand | minutes for a reader to state the finding and locate its proof | from ~45 min manual to ≤ 5 min |
| **G4** | Never produce an unsupported claim | % of findings with a verifiable source (PDF page, JSON field, graph edge) | 100% |
| **G5** | Quantify what is under review, not just count it | total contract value of flagged cases | reported per batch |

All five are measurable before the end of the project on our own evaluation set. None requires access to a real audit body.

---

## 3. Key performance indicators

**Detection quality** — precision@50, precision@200, recall on the synthetic rigged set
**Efficiency** — % of corpus filtered by triage, number of document analyses avoided, cost per analysed file
**Usability** — minutes-to-understand a case file, auditor agreement rate with the system's recommendation
**Integrity** — evidence completeness (%), false positive rate on hand-verified clean contracts
**Coverage** — contracts processed, € value under review, % of contracts with a usable specification document

---

## 4. Objective tree

### Level 0 — Main objective

> **Maximise the number of confirmed procurement irregularities found per auditor-hour.**

Chosen because it is the actual constraint. The bottleneck is not data and not detection — it is human review capacity. Every sub-objective below either raises the hit rate of that scarce hour or lowers the cost of using it.

### Level 1 — Intermediate objectives

```
                    Maximise confirmed irregularities
                          per auditor-hour
                                  │
        ┌─────────────────┬───────┴───────┬─────────────────┐
        │                 │               │                 │
   A. TRIAGE         B. DOCUMENTS    C. NETWORK       D. CASE FILE
   Reduce the        Detect tailored  Detect non-      Deliver decision-
   volume without    technical        genuine          ready evidence
   losing cases      requirements     competition      and measure it
        │                 │               │                 │
   [Member 1]        [Member 2]      [Member 3]        [Member 4]
   Screener agent    Spec Auditor    Investigator      Case Builder
```

### Level 2 — The twelve data science objectives

Each is written as **input → output → metric**. One per member is agentic.

#### Branch A — Triage · Member 1 · *Screener agent*

**A1 — Cheap risk features from structured records**
Award records (amount, procedure type, number of bids, deadline length, distance to a legal threshold, buyer–supplier history) → a feature table computable without opening any document → *AUC of a model using only these features*.

**A2 — Ranking under a fixed review budget**
Feature table + labels → a model that orders contracts by probability of irregularity → *precision@50 and recall@500*. This is a ranking problem, not a classification problem: overall accuracy is irrelevant when only the top of the list is ever read.

**A3 — AGENTIC · Screener agent**
Calls the ranking model and a cost estimator, then **decides per file**: deep analysis now / watchlist / close → *% of known irregular cases retained while cutting deep analyses by 80%*.

#### Branch B — Documents · Member 2 · *Spec Auditor agent*

**B1 — Requirement extraction**
Specification document (PDF) → structured requirements as (parameter, operator, value, unit) → *F1 on a 300-requirement annotated sample*.

**B2 — Restrictive specification detection**
Extracted requirements → a restrictiveness score, from two sources: rule-based (brand named without "or equivalent") and statistical (over-precision outlier within the same CPV category — an exact `= 78 cm` among forty `≥ 70 cm`) → *precision and recall on synthetic rigged documents plus a real annotated set*.

**B3 — AGENTIC · Spec Auditor agent**
Calls the extractor, the rule checker and the category comparator; **re-runs extraction on low-confidence fields**; emits findings with page references → *% of findings carrying a valid page citation*.

#### Branch C — Network · Member 3 · *Investigator agent*

**C1 — Bidder–buyer graph and network features**
Organisation identifiers (UA-EDR) across all contracts → a graph with engineered features: shared addresses, co-bidding frequency, head-to-head rate, community membership, company age at bid time → *feature coverage and stability across time windows*.

**C2 — Collusion pattern detection**
Graph + bidding history → flags for never-facing pairs, bid rotation, winners incorporated shortly before the notice → *precision@k on contracts later annulled or contested*.

**C3 — AGENTIC · Investigator agent**
Queries the graph and **decides how deep to traverse** — stops when the winner is clean at the first level, digs further when a link appears → *cases resolved within N queries with no loss of detection*.

#### Branch D — Case file & evaluation · Member 4 · *Case Builder agent*

**D1 — Signal fusion and monetary exposure**
Findings from A, B, C → one calibrated risk score plus € exposure (score × contract value) → *calibration error, and € correctly ranked at the top*.

**D2 — Evaluation harness**
Three label sources combined: synthetic rigged tenders we inject, real annulled or contested contracts, and a 100-file manual annotation → *measurable recall on synthetic data, precision on real data*. Without this objective the whole project has no number to report.

**D3 — AGENTIC · Case Builder agent**
Assembles the findings, calls the scoring model, **verifies that every claim has a source and discards those that do not**, decides close / watch / alert, and writes the audit file → *100% evidence completeness, plus agreement rate with a human reader*.

### Level 3 — Why the branches are not independent

The agents are chained: A feeds B, B and C feed D. A member cannot finish alone, which is the point — one system, not four side projects.

```
  Screener ──► Spec Auditor ──┐
      │                       ├──► Case Builder ──► Audit queue
      └──────► Investigator ──┘
```

---

## 5. What this project deliberately does not claim

Stated here because it is the part most likely to be challenged, and having the answer ready is worth more than a bigger feature list.

**A company winning many contracts is not evidence of anything.** It may be the best supplier, or the only qualified one. No law requires spreading contracts across firms. Concentration is used as *context* in this system — it carries no weight on its own and only matters sitting on top of a verified violation.

**The system never outputs "fraud".** It outputs *"competition on this contract does not appear to have been genuine, for these reasons, at these locations in these documents"*. The finding is a hypothesis with evidence attached. A human confirms or dismisses it.

**It works only after the award.** Bid contents are not public before that. This is a constraint of the data, and it is why the product is an audit tool and not a procurement tool.

---

## 6. Data source

**Prozorro (Ukraine) — public procurement API.** Free, no authentication, live, history from February 2015.

- OCDS-compatible: `https://public-api.prozorro.gov.ua/api/2.5` with `opt_schema=ocds`
- The *Bids and Expressions of Interest* extension is published → number and identity of bidders available
- Organisations identified by `UA-EDR` (national company register) on both buyer and supplier side → the graph is buildable
- `documents[]` carries downloadable files with `url`, `title`, `format`, `documentType` → specification documents are reachable
- Product classification `ДК021`, numerically identical to EU CPV codes → category comparison works, and results stay internationally comparable

**Three data types, one source:** structured (award records), semi-structured (nested OCDS releases, full contract history), unstructured (specification documents).

Known constraints, to be handled in Phase 2: the corpus is hundreds of GB and must be sampled by category, date and procedure type rather than downloaded whole; the majority of records are direct awards (`procurementMethodType: reporting`) with no competition at all and must be filtered out; documents are in Ukrainian; and some contracting processes are redacted for national security reasons.
