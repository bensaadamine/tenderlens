"""
THE RULES OF LAW the system checks - one entry per rule, each with its
official source, a short quote, and which component uses it.

Used by the auditor's page (/rules, and a link under every reason of a case)
and by the rules assistant (agents/rules_agent.py) as its starting points.
The quotes are short; the link opens the official text. Amounts written in
the trade agreements are adjusted over time: the official page is the
reference.

    from tools import laws
    laws.for_kind("brand_without_equivalent")   # -> [rule, ...]
"""

GCR = "https://laws-lois.justice.gc.ca/eng/Regulations/SOR-87-402/page-1.html"
CFTA5 = ("https://www.cfta-alec.ca/cfta-agreement/"
         "chapter-five-government-procurement")
SM3 = ("https://canadabuys.canada.ca/en/how-procurement-works/"
       "policies-and-guidelines/supply-manual/chapter-3-annexes")

RULES = [
    {"id": "solicit-bids",
     "title": "Bids must be asked for",
     "plain": "Before a federal contract is signed, the buyer must ask for "
              "bids. Buying without competition is the exception, not the "
              "rule.",
     "quote": "the contracting authority shall solicit bids therefor",
     "source": "Government Contracts Regulations, s. 5", "url": GCR,
     "used_by": "The whole system: competition is what it checks.",
     "kinds": ["limited_tendering_method"]},
    {"id": "no-bids-exceptions",
     "title": "When a contract may be given without bids",
     "plain": "Four cases only: a pressing emergency; a goods contract of "
              "$25,000 or less; bids not in the public interest; only one "
              "supplier able to do the work.",
     "quote": "in the case of a goods contract, $25,000",
     "source": "Government Contracts Regulations, s. 6", "url": GCR,
     "used_by": "M1 (limited tendering is declared, ranks a case); the "
                "split-purchase agent (the $25,000 limit).",
     "kinds": ["limited_tendering_method", "acan", "split_purchase"]},
    {"id": "no-brand",
     "title": "No brand name without “or equivalent”",
     "plain": "A specification should not require a particular brand. If "
              "it names one, the buyer must accept equivalent products and "
              "say so, with words such as “or equivalent”.",
     "quote": "require or refer to a particular trademark or trade name",
     "source": "Canadian Free Trade Agreement, art. 509(1)", "url": CFTA5,
     "used_by": "M2 spec auditor (rules find the names, the agent judges); "
                "M4 (brand = winner).",
     "kinds": ["brand_without_equivalent", "brand_matches_winner"]},
    {"id": "equivalent-clause",
     "title": "The standard “Equivalent Products” clause",
     "plain": "Canada's standard clause B3000T says equivalent products "
              "are accepted (same form, fit and function). When a tender "
              "carries it, a brand named in it is legal.",
     "quote": "",
     "source": "Standard Acquisition Clauses and Conditions, B3000T",
     "url": "",
     "used_by": "M2 rules: the clause, or its number alone, clears every "
                "brand of the tender.",
     "kinds": []},
    {"id": "no-splitting",
     "title": "No dividing a purchase to avoid the rules",
     "plain": "A buyer may not cut one need into smaller purchases, or "
              "choose how to value it, so as to escape the obligations of "
              "the agreement.",
     "quote": "divide procurement requirements in order to avoid the "
              "obligations",
     "source": "Canadian Free Trade Agreement, art. 503(1)", "url": CFTA5,
     "used_by": "The split-purchase agent.",
     "kinds": ["split_purchase"]},
    {"id": "limited-tendering",
     "title": "When limited tendering is allowed",
     "plain": "The agreement lists the cases where a buyer may skip open "
              "competition, for example when only one supplier can provide "
              "the goods and no reasonable alternative exists.",
     "quote": "no reasonable alternative or substitute goods or services "
              "exist",
     "source": "Canadian Free Trade Agreement, art. 513(1)(b)", "url": CFTA5,
     "used_by": "M1: a limited-tendering procedure is declared on the "
                "notice; it ranks a case, never puts it in the queue (the "
                "data never carries the justification).",
     "kinds": ["limited_tendering_method"]},
    {"id": "acan",
     "title": "Advance Contract Award Notice (ACAN)",
     "plain": "The buyer announces the supplier it intends to use, for at "
              "least 15 calendar days; any other supplier may answer with "
              "a statement of capabilities. The market was told in "
              "advance, so it is not an audit case here.",
     "quote": "",
     "source": "Supply Manual, 3.15.5", "url": SM3,
     "used_by": "The gate: an ACAN is not processed.",
     "kinds": ["acan"]},
    {"id": "time-periods",
     "title": "Time to answer a tender",
     "plain": "The agreement fixes no number of days: suppliers must have "
              "a reasonable time. So a short window is only compared with "
              "tenders run the same way, and never queues a case alone.",
     "quote": "a reasonable period of time",
     "source": "Canadian Free Trade Agreement, art. 511", "url": CFTA5,
     "used_by": "M1 (short window, compared with its own procedure).",
     "kinds": ["short_window"]},
    {"id": "coverage",
     "title": "Which purchases the agreement covers",
     "plain": "The agreement applies to goods purchases at or above its "
              "threshold. Below it, or when the notice lists no trade "
              "agreement, its rules (the brand rule included) may not "
              "apply: context, not evidence.",
     "quote": "$25,000 or greater for goods",
     "source": "Canadian Free Trade Agreement, art. 504(3)", "url": CFTA5,
     "used_by": "M1 (no trade agreement: context only).",
     "kinds": ["no_trade_agreement"]},
]

BY_ID = {r["id"]: r for r in RULES}


def for_kind(kind):
    """The rules behind one kind of claim (a case's reason)."""
    return [r for r in RULES if kind in r["kinds"]]
