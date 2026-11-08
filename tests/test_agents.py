"""
Tests for the rules M3 and M4 claim to enforce. No data needed - builds a
synthetic award file and synthetic agent outputs.

    python -m tests.test_agents

Each check is one sentence of the design stated as a test. If one fails, the
module's docstring is lying about what it does.
"""


import csv
import os
import sys
import tempfile

from agents import m3_investigator as m3
from agents import m4_case_builder as m4

FAILS = 0


def check(name, cond):
    global FAILS
    FAILS += not cond
    print(("ok   " if cond else "FAIL ") + name)


def awards_file(path, rows=None):
    h = ["referenceNumber-numeroReference",
         "solicitationNumber-numeroSollicitation",
         "supplierLegalName-nomLegalFournisseur-eng",
         "supplierLegalName-nomLegalFournisseur-fra",
         "contractingEntityName-nomEntitContractante-eng",
         "contractingEntityName-nomEntitContractante-fra",
         "totalContractValue-valeurTotaleContrat",
         "contractAwardDate-dateAttributionContrat", "unspsc"]
    dnd = "Department of National Defence"
    rows = rows or [
        ("A1", "W8476-001", "PALFINGER INC.", "x", dnd, "x", "250000",
         "2025-03-01", "25101900"),
        ("A2", "W8476-002", "Palfinger Inc", "x", dnd, "x", "180000",
         "2025-06-01", "25101900"),
        ("A3", "W8476-003", "PALFINGER INC.", "x", dnd, "x", "90000",
         "2025-09-01", "25101900"),
        ("A4", "W8476-004", "Other Cranes Ltd", "x", dnd, "x", "70000",
         "2025-10-01", "25101900"),
        # the same award re-published with an amendment
        ("A4", "W8476-004", "Other Cranes Ltd", "x", dnd, "x", "70000",
         "2025-10-02", "25101900"),
        ("A5", "X-9", "PALFINGER INC.", "x", "City of Ottawa", "x", "50000",
         "2024-01-01", "25101900"),
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(h)
        w.writerows(rows)


def main():
    tmp = os.path.join(tempfile.mkdtemp(), "awards.csv")
    awards_file(tmp)

    print("M3 investigator")
    g = m3.load_graph(tmp)
    check("columns detected, English not French",
          g["cols"]["supplier"].endswith("-eng")
          and g["cols"]["buyer"].endswith("-eng"))
    check("an award re-published with an amendment counts once",
          g["bg"][("DEPARTMENT OF NATIONAL DEFENCE", "2510")] == 4)
    check("'PALFINGER INC.' and 'Palfinger Inc' are one supplier",
          g["bsg"][("DEPARTMENT OF NATIONAL DEFENCE", "PALFINGER",
                    "2510")] == 3)
    notice = {"solicitationNumber-numeroSollicitation": "w8476-002",
              "unspsc": "25101900"}
    r = m3.investigate("cb-x", notice, g)
    check("joins on solicitation number, case-insensitive",
          bool(r["award"]) and r["award"]["supplier"] == "Palfinger Inc")
    check("recurring winner found (3 of 4) and judged dependent "
          "(3 of its 4 awards from this buyer)",
          bool(r["findings"]) and
          r["findings"][0]["kind"] == "dependent_supplier")
    check("depth 2 ran and the trace says so",
          any("depth 2" in t for t in r["trace"]))
    check("M3 findings are context, not evidence (decision 4)",
          r["findings"][0]["weight"] == "context")
    r0 = m3.investigate("cb-x", {"solicitationNumber-numeroSollicitation":
                                 "NOPE"}, g)
    check("no award found -> no finding, no error",
          r0["award"] is None and r0["findings"] == [])
    check("no award file -> M3 unavailable, pipeline continues",
          m3.investigate("cb-x", {}, None)["available"] is False)

    # Audit finding 2026-11-01: the winner depended on the file's row order
    split = [("S1", "W-SPLIT", "Alpha Trucks Ltd", "x", "DND", "x", "1",
              "2025-01-10", "25101900"),
             ("S2", "W-SPLIT", "Bravo Trucks Ltd", "x", "DND", "x", "1",
              "2025-03-05", "25101900")]
    picks = []
    for order in (split, split[::-1]):
        p2 = os.path.join(tempfile.mkdtemp(), "awards.csv")
        awards_file(p2, order)
        a = m3.investigate("cb-s", {"solicitationNumber-numeroSollicitation":
                                    "W-SPLIT"}, m3.load_graph(p2))["award"]
        picks.append((a["supplier"], a["winners"]))
    check("the winner no longer depends on the file's row order",
          picks[0] == picks[1] and picks[0][0] == "Bravo Trucks Ltd")
    check("every supplier of a split award is listed, newest first",
          picks[0][1] == ["Bravo Trucks Ltd", "Alpha Trucks Ltd"])
    m3.AWARD_PICK = "first"
    p3 = os.path.join(tempfile.mkdtemp(), "awards.csv")
    awards_file(p3, split)
    a = m3.investigate("cb-s", {"solicitationNumber-numeroSollicitation":
                                "W-SPLIT"}, m3.load_graph(p3))["award"]
    m3.AWARD_PICK = "latest"
    check("AWARD_PICK = 'first' restores the first row",
          a["supplier"] == "Alpha Trucks Ltd"
          and a["winners"] == ["Alpha Trucks Ltd"])

    print("\nM4 case builder")
    m1 = {"reference": "cb-x", "title": "Crane truck", "signals": [],
          "route": "close"}
    src = {"file": "rfp.pdf", "where": "page 3, line 40",
           "line": "The crane must be Palfinger model PK11001;",
           "checked": True}
    l1 = {"level": 1, "kind": "brand_without_equivalent",
          "confidence": "medium", "claim": "Names 'Palfinger'",
          "brand": "Palfinger", "source": src}

    c = m4.build_case(m1, {"readable": True, "findings": [l1]}, r)
    pts = {x["kind"]: x.get("points") for x in c["claims"]}
    check("brand named in the spec == awarded supplier -> cross-check",
          "brand_matches_winner" in pts)
    check("context counts once there is document evidence",
          pts.get("dependent_supplier") == 2)
    check("level 1 + winner match + context -> ALERT", c["route"] == "alert")
    check("the narrative never says 'fraud' (decision 3)",
          "fraud" not in c["narrative"].lower())

    c1 = m4.build_case(m1, {"readable": True, "findings": [l1]}, None)
    check("ONE unqualified brand alone -> WATCH, not ALERT",
          c1["route"] == "watch")

    c2 = m4.build_case(m1, {"readable": True, "findings": []}, r)
    check("context alone scores nothing (decision 4)",
          c2["score"] == 0 and c2["route"] == "close")

    l2 = {"level": 2, "kind": "extreme_threshold", "confidence": "low",
          "claim": "x", "source": {"file": "f", "where": "s", "line": "q",
                                   "checked": True}}
    c3 = m4.build_case(m1, {"readable": True, "findings": [l2, dict(l2)]},
                       None)
    check("level 2 alone cannot make a case (measured unreliable)",
          c3["route"] == "close")

    nosrc = dict(l1, source={"file": "rfp.pdf", "where": "p1",
                             "line": None})
    c4 = m4.build_case(m1, {"readable": True, "findings": [nosrc]}, None)
    check("a claim with no quoted line is unverified and scores nothing",
          c4["score"] == 0 and c4["claims"][0]["verified"] is False)

    unchecked = dict(l1, source=dict(src, checked=False))
    c6 = m4.build_case(m1, {"readable": True, "findings": [unchecked]}, None)
    check("a quote not found in its document is unverified, scores nothing",
          c6["score"] == 0 and c6["claims"][0]["source_attached"]
          and not c6["claims"][0]["verified"]
          and "not found" in c6["claims"][0]["not_scored_because"])
    m4.VERIFY = "attached"
    c7 = m4.build_case(m1, {"readable": True, "findings": [unchecked]}, None)
    m4.VERIFY = "checked"
    check("VERIFY = 'attached' restores the old rule", c7["score"] == 3)

    alpha = dict(l1, brand="Alpha")
    split_m3 = {"award": {"supplier": "Bravo Trucks Ltd",
                          "winners": ["Bravo Trucks Ltd", "Alpha Trucks Ltd"]},
                "findings": []}
    c8 = m4.build_case(m1, {"readable": True, "findings": [alpha]}, split_m3)
    check("a brand matching one of several winners -> brand = winner",
          any(x["kind"] == "brand_matches_winner"
              and "one of the 2 suppliers" in x["claim"]
              for x in c8["claims"]) and c8["route"] == "alert")

    from tools import documents as m2a
    docs = [{"document": "rfp.pdf",
             "text": "ANNEX A\nThe crane must be\nPalfinger model PK11001;\n"},
            {"document": "amd.pdf", "text": "Question 1: other text"}]
    check("M2 checks a quote against its document, across line breaks",
          m2a.quote_in_documents(docs, "rfp.pdf",
                                 "The crane must be Palfinger model PK11001;"))
    check("words not in the tender are not checked",
          not m2a.quote_in_documents(docs, "rfp.pdf",
                                     "The crane must be a Liebherr"))
    check("a quote cut with '...' needs every piece, in order",
          m2a.quote_in_documents(docs, "rfp.pdf", "The crane ... PK11001")
          and not m2a.quote_in_documents(docs, "rfp.pdf",
                                         "PK11001 ... The crane"))
    check("a document named another way: the tender's other texts are read",
          m2a.quote_in_documents(docs, "RFP (1).pdf", "Palfinger model"))

    many = [dict(l1, brand=f"B{i}") for i in range(5)]
    c5 = m4.build_case(m1, {"readable": True, "findings": many}, None)
    check("5 lock-in brands capped at 2 - repetition is not corroboration",
          c5["score"] == 6)

    acan = {"reference": "cb-x", "signals": [
        {"signal": "acan", "weight": "strong", "why": "w",
         "evidence": "Advance Contract Award Notice"}]}
    c6 = m4.build_case(acan, {"readable": False, "findings": []}, None)
    check("a DECLARED ACAN alone -> CLOSE, keeps its score for ranking",
          c6["route"] == "close" and c6["score"] == 2)
    acan2 = {"reference": "cb-x", "signals": acan["signals"] + [
        {"signal": "short_window", "weight": "weak", "why": "w",
         "evidence": "11 days"}]}
    c7 = m4.build_case(acan2, {"readable": False, "findings": []}, None)
    check("ACAN + a short window -> WATCH, never ALERT",
          c7["route"] == "watch")
    acan3 = {"reference": "cb-x", "signals": acan["signals"] + [
        {"signal": "no_trade_agreement", "weight": "weak", "why": "w",
         "evidence": "*None"}]}
    c8 = m4.build_case(acan3, {"readable": False, "findings": []}, None)
    check("ACAN + no trade agreement -> CLOSE (context only)",
          c8["route"] == "close" and c8["score"] == 3)

    # ---- what M2 and M3 hand to M4 (agents/evidence.py)
    from agents.evidence import Evidence, Finding
    d = Finding(kind="brand_without_equivalent", level=1, claim="c",
                source=Evidence(file="01_rfp.pdf", where="p1", line="x"),
                brand="Honda").to_dict()
    check("a finding travels as a plain dict; empty fields left out",
          d["source"] == {"file": "01_rfp.pdf", "where": "p1", "line": "x"}
          and "agent_letter" not in d and d["brand"] == "Honda")
    try:
        Finding(kind="k", level=1, claim="c", source=None, brnad="typo")
        typo = False
    except TypeError:
        typo = True
    check("a misspelt field fails at once", typo)

    # ---- M2: one brand written several ways is one finding
    from agents import m2_spec_auditor as m2s
    rows = [{"brand": "FormLabs Form", "occurrences": 1, "agent_letter": "f",
             "decided_by": "the M2 agent: brand required, no equivalent"},
            {"brand": "Formlabs", "occurrences": 1, "agent_letter": "f",
             "decided_by": "the M2 agent: brand required, no equivalent"},
            {"brand": "Bendix", "occurrences": 2,
             "decided_by": "rules: Wikidata confirms the brand"}]
    merged = m2s.merge_spellings(rows)
    check("one brand written two ways -> one finding, plainest name kept",
          [r["brand"] for r in merged] == ["Formlabs", "Bendix"]
          and merged[0]["also_written"] == ["FormLabs Form"]
          and merged[0]["occurrences"] == 2)
    m2s.SAME_BRAND = None
    check("SAME_BRAND = None restores one finding per spelling",
          len(m2s.merge_spellings(rows)) == 3)
    m2s.SAME_BRAND = "first word"
    names = m2s.names_checked({"findings": merged, "ok": [],
                               "unresolved": [], "dropped": [
                                   {"brand": "RFSA", "decided_by":
                                    "rule: the tender's own acronym"}]})
    check("every name says who settled it, merged spellings included",
          [(n["name"], n["result"]) for n in names] == [
              ("Formlabs", "finding"), ("FormLabs Form", "finding"),
              ("Bendix", "finding"), ("RFSA", "dropped")]
          and all(n["decided_by"] for n in names))

    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
