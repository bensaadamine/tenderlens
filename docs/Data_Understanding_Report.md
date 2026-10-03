# TenderLens — Data Understanding

**Phase 2, step 1: the structured data**
Written after exploring 10,000 real tenders from the Prozorro API.

---

## 1. What we did

Three passes, each one cheap before the next one got expensive.

| Pass | What it did | Sample |
|---|---|---|
| Walk the feed | collected tender ids and a few cheap fields | 10,000 |
| Fetch full records | downloaded the complete object for each | 10,000 |
| Profile and diagnose | measured what is actually usable | 500 → 3,000 → 10,000 |

Numbers below are labelled with the sample they come from, because some were measured before the full harvest finished.

---

## 2. The data source

**Prozorro** — Ukraine's national public procurement system.

- Public API, no key, no authentication, free
- `https://public-api.prozorro.gov.ua/api/2.5`
- History from February 2015, updated in real time
- OCDS-compatible (an international open standard), so the method transfers to other countries

The API is a **change log**, not a searchable database. You ask "what changed since this date?" and walk forward or backward. There is no way to ask "give me all medical equipment tenders from 2025". This single fact shaped our whole sampling strategy.

**One important limitation we found by testing:** the feed only returns simple top-level fields. The product category, the contract value and the items are *not* available until you download the full record. So we cannot filter before downloading — we download broadly and filter afterwards.

---

## 3. What one tender record contains

A tender is one purchase: a public body publishes what it needs, companies submit offers, one wins. The record follows that whole life, and the `status` field says where it is:

`active.enquiries` → `active.tendering` → `active.auction` → `active.qualification` → `active.awarded` / `complete`
(or it ends as `unsuccessful` or `cancelled`)

Each record carries:

| Group | Fields | Present |
|---|---|---|
| Identity | id, tenderID, title, dates | 100% |
| What is bought | items, classification (CPV code), value | 100% |
| Who buys | procuringEntity with a national company number | 100% |
| Timing | tenderPeriod, enquiryPeriod | 100% / 77% |
| Documents | documents[] with url, title, format, type | 100% |
| Offers | bids[] | 53% |
| Result | awards[], contracts[] | 53% |

*(presence measured on 3,000 records)*

**Why bids are only there half the time:** by law, offers stay confidential while bidding is open. They become public from the qualification stage onward. So our system can only work on tenders that have reached the end of their life. That is not a data problem — it is how the law works, and it confirms that this is an audit tool, used after the award, not a procurement tool.

---

## 4. The six findings that matter

### Finding 1 — Documents are Word files, not PDFs

*(3,000 records, 16,700 documents)*

| Format | Share |
|---|---|
| .docx | 58.4% |
| .p7s (digital signatures — not content) | 22.4% |
| .doc (legacy Word) | 12.1% |
| .pdf | 3.6% |
| other | 3.5% |

We expected PDFs. There are almost none. This does not block us — Word files are readable — but it changes the tools we need, and the legacy `.doc` format (12%) needs a converter.

**62.4% of tenders have a specification document in a readable format.** That is the hard ceiling for any analysis that reads the requirements. The other 38% we simply cannot read.

### Finding 2 — "Only one bidder" is normal, not suspicious

*(3,000 records with published bids)*

We planned to treat a single bidder as a warning sign. It is the majority case:

| Procedure | Single-bidder rate |
|---|---|
| aboveThreshold (large, formal) | 74.3% |
| belowThreshold (small, simplified) | 59.6% |
| priceQuotation | 44.9% |

A warning that fires on 6 out of 10 purchases is not a warning. The same turned out to be true of short deadlines: 59% of tenders have a bidding window under 7 days, because short windows are perfectly legal for small purchases.

**This forced the central design decision of the project** — see Finding 4.

### Finding 3 — Confirmed cases are rare

*(3,000 records)*

| Signal | Share |
|---|---|
| Has a complaint anywhere in the record | 2.4% |
| Has a cancellation | 2.6% |
| Status `unsuccessful` | 12.2% |
| Status `cancelled` | 2.0% |

About **240 complaint cases per 10,000 tenders**. Enough to check whether our ranking puts real problems near the top. Not enough to train a model from scratch.

This confirms the plan we chose: build artificial rigged tenders to measure how much we catch, and use the real complaints to check that we are relevant to the field.

*(Note: a complaint filed is not a complaint upheld. We still need to read the complaint outcome and keep only those that were accepted.)*

### Finding 4 — The same fact means opposite things in different categories

*(10,000 records — this is the key result)*

Single-bidder rate, by product category:

| CPV-4 | Product | Single bidder |
|---|---|---|
| 4291 | water treatment machinery | **4.8%** |
| 3019 | office equipment | 31.7% |
| 3433 | vehicle parts | 35.2% |
| 3023 | monitors, printers | 52.9% |
| 3916 | furniture | 80.8% |
| 3369 | medical consumables | 82.2% |
| 3411 | **motor vehicles** | **92.6%** |

From 4.8% to 92.6%.

One bidder for water treatment machinery is a 1-in-20 event — worth a look. One bidder for motor vehicles is the normal case, because dealers hold exclusive territories. That is market structure, not fraud. **Flagging it would produce noise, not findings.**

So no signal in this system is absolute. Every one is measured against the norm of its own group — same category, same procedure type, same value band. This is what makes the project a data science problem rather than a list of rules.

*Caution: the extreme values sit on small samples (4291 has 21 scored tenders, 3411 has 27). They need confidence intervals, or a 50-tender minimum, before being quoted in a report.*

### Finding 5 — One signal that does work

*(10,000 records)*

Bidding windows for large (`aboveThreshold`) tenders:

- median 15.7 days
- middle half between 15.3 and 16.1 days
- **28 tenders below the 15-day legal floor (4.5% of large tenders, 0.28% of all)**
- shortest observed: 7.2 days

Almost every buyer sits exactly on the legal minimum. That makes going *below* it rare, sharp and legally defined — the opposite of the single-bidder flag. It is checkable from two dates, with no modelling at all.

*(The 15-day floor still has to be confirmed against the current Ukrainian law text before we quote it.)*

### Finding 6 — Structure is simpler than feared

*(3,000 records)*

- **70.6%** of tenders have exactly one lot, **2.1%** have more than one. We can work at tender level and ignore lot-level complexity.
- **100%** of buyers carry a national company number (`UA-EDR`), and 53% of suppliers do. The company network needed for the collusion analysis can be built.
- Product codes use `ДК021`, which has the same numbers as the EU CPV standard — so comparisons by category work, and results stay internationally comparable.

---

## 5. What we can and cannot do

| | |
|---|---|
| ✅ Compare bidding behaviour across categories | 36 categories have enough data |
| ✅ Detect deadlines below the legal floor | works today, no model needed |
| ✅ Build the buyer–supplier network | identifiers are complete |
| ✅ Read requirement documents | for 62% of tenders |
| ⚠️ Validate against confirmed cases | only ~2.4% have complaints |
| ❌ Analyse tenders still in progress | offers are confidential until award |
| ❌ Filter by category before downloading | the API does not allow it |

---

## 6. The scope we chose

Ranking categories by raw volume would have been a mistake. The biggest groups are road works, fuel and food, whose documents are bills of quantities and price lists — no technical parameters to analyse.

So we rank by **usable** tenders: goods (not works or services) **and** a readable specification document.

- 8,043 of 10,000 tenders are goods
- **36 categories** have 30 or more usable tenders
- The top 20 cover **1,340 usable tenders**

Those 20 categories — computers, monitors, vehicle parts, construction materials, lab and medical equipment — are the project's working scope. They are saved in `reports/selected_cpv4.json`.

---

## 7. Still open

1. **Verify the legal minimum deadlines** against the Ukrainian procurement law. Our 15-day figure comes from the shape of the data, not the statute.
2. **Read the complaint outcomes** — only upheld complaints count as ground truth.
3. **Confidence intervals** on the per-category rates before publishing any of them.
4. **A disagreement between two of our own scripts** on how many tenders have a complete bidding period. One of them is wrong and we need to find out which.
5. **Open the documents themselves.** Everything above describes the structured data. We have not yet looked inside a single Word file to see what a requirements list actually looks like. That is the next step.

---

## 8. Next step

Open a sample of `.docx` specification files from the selected categories and answer:

- How are requirements written? Prose, tables, or numbered lists?
- Can we reliably pull out (parameter, operator, value, unit)?
- How often does a brand name appear, and is "or equivalent" next to it?
- How often is a value written as an exact number instead of a minimum?

That tells us whether the Spec Auditor agent is buildable, and how hard.
