"""
M4 CASE BUILDER.

Fuses what M1, M2 and M3 found about ONE tender into a case, checks every
claim has a source, scores it, and decides: alert / watch / close.

    from m4_case_builder import build_case
    case = build_case(m1_result, m2_result, m3_result)

-------------------------------------------------------- what it refuses to do

DECISION 3. It never says fraud. The strongest thing it writes is "the
competition on this contract does not appear to have been genuine, for these
reasons, at these locations". A human decides.

VERIFICATION. A claim counts only if it carries a source a human can open:
a document and the line quoted from it, a notice field, or an award record.
A claim without one is SHOWN, marked unverified, and scores nothing. This is
the one check in the system that is not negotiable - an audit queue whose
claims cannot be checked is a list of accusations.

DECISION 4. M3's findings (a buyer that keeps awarding to the same supplier)
are context. They add to a case that already has evidence from the
documents, and add nothing to a case that does not.

WHAT CAN RAISE AN ALERT. Only a verified LEVEL-1 finding - a brand named
without "or equivalent", which is a concrete departure from CFTA/CETA's
functional-specification duty, quoted from the tender. Everything else can
make a case worth WATCHING but not, on its own, an alert:

  * notice signals (ACAN, a limited-tendering reason) are things the buyer
    DECLARED; the question they raise is whether the justification holds,
    which is a review, not a hidden finding.
  * level-2 findings are measured unreliable on this corpus (level2.py: the
    top peer "finding" was a semi-trailer compared with pickup trucks), so
    they are low confidence by construction.

------------------------------------------------------------- the weights

UNCALIBRATED. There is no ground truth yet (decision 7: synthetic rigged
tenders measure recall, real complaints prove relevance; neither exists).
They are an ordering, chosen so the logic above holds, and they are in one
table so that calibrating them later is one edit. Do not quote a score as a
probability.
"""

# --- repo layout: find the agents/, tools/, prepare/ folders and run from
# the repository root, whatever folder the script sits in (2026-10-22).
import os as _os
import sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("agents", "tools", "prepare", "tests", ""):
    _p = _os.path.join(_ROOT, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
if __name__ == "__main__":
    _os.chdir(_ROOT)
# ---


import re

WEIGHTS = {
    # level 1 - legally grounded, quoted from the document
    "brand_without_equivalent": 3,
    # the brand the spec names is the company that won the contract
    "brand_matches_winner": 3,
    # M1 - declared by the buyer on the notice
    "acan": 2,
    "limited_tendering_method": 2,
    "limited_tendering_reason": 2,
    "short_window": 1,
    "no_trade_agreement": 1,
    # level 2 - low confidence, see level2.py
    "narrow_band": 1,
    "extreme_threshold": 1,
    "within_document_bands": 1,
    # M3 - context only (decision 4)
    "recurring_supplier": 1,
    "dependent_supplier": 2,
}
# Repetition is not corroboration: five lock-in brands in one spec is one
# buyer's drafting habit, not five independent reasons.
CAPS = {"brand_without_equivalent": 2, "level2": 2}

ALERT = 5
ALERT_FROM = "documents"   # or "all" - see build_case
# What the buyer DECLARES on the notice: an advance award notice, a limited
# tendering method or reason. User decision 2026-10-19: alone, these rank a
# case but never put it in the queue - measured on 4110, an ACAN alone sent
# 194 of 957 tenders (20%) to WATCH, burying any document finding. They
# still reach WATCH together with anything not declared (a brand, a short
# window, a recurring supplier ...). DECLARED_ALONE = "watch" restores.
DECLARED = {"acan", "limited_tendering_method", "limited_tendering_reason"}
DECLARED_ALONE = "close"
# "No trade agreement" is context, not a sign: it only means the rule against
# naming brands does not apply. User decision 2026-10-19: it no longer lifts
# declared signals into WATCH on its own. CONTEXT_ALONE = "watch" restores.
CONTEXT_ONLY = {"no_trade_agreement"}
CONTEXT_ALONE = "close"
# Wording of the case page. "plain" (2026-10-19): one sentence per reason,
# each marked declared / found in the documents / context; no internal codes.
# "v1" restores the earlier text.
NARRATIVE = "plain"
NATURE = {
    "acan": "Declared by the buyer",
    "limited_tendering_method": "Declared by the buyer",
    "limited_tendering_reason": "Declared by the buyer",
    "short_window": "From the notice dates",
    "no_trade_agreement": "Context",
    "brand_without_equivalent": "Found in the documents",
    "brand_matches_winner": "Found in the documents + award",
    "narrow_band": "Low-confidence comparison",
    "extreme_threshold": "Low-confidence comparison",
    "within_document_bands": "Low-confidence comparison",
    "recurring_supplier": "Award history",
    "dependent_supplier": "Award history",
}


def plain_text(c, award=None):
    """One plain sentence for a claim, for a reader who is not us."""
    k, s = c["kind"], c.get("source") or {}
    ev = str(s.get("line") or "").strip()
    who = (award or {}).get("supplier") or "a supplier it named"
    if k == "acan":
        return (f"The buyer announced in advance that it would give this "
                f"contract to {who} without an open competition (Advance "
                f"Contract Award Notice).")
    if k == "limited_tendering_method":
        return ("The buyer used a limited-tendering procedure: the contract "
                "was not opened to competition.")
    if k == "limited_tendering_reason":
        return f"The buyer's stated reason for not competing: \u201c{ev}\u201d."
    if k == "short_window":
        d, m = c.get("days"), c.get("median")
        if d is not None and m is not None:
            return (f"Bidders had {d:g} days to respond; tenders run the same "
                    f"way usually get {m:g}. A short window favours a bidder "
                    f"who already knew.")
        return "Bidders had an unusually short time to respond."
    if k == "no_trade_agreement":
        return ("The contract is not covered by any trade agreement, so the "
                "rule against naming brands does not apply.")
    if k == "brand_without_equivalent":
        b = c.get("brand") or "a brand"
        return (f"The specification requires {b} and does not accept an "
                f"equivalent product.")
    return c.get("claim", "")
WATCH = 2

LEVEL2_KINDS = {"narrow_band", "extreme_threshold", "within_document_bands"}
CONTEXT_KINDS = {"recurring_supplier", "dependent_supplier"}


def _verified(src):
    """A source a human can open: an origin AND the words it says."""
    if not src:
        return False
    origin = src.get("file") or src.get("where")
    return bool(origin) and bool(src.get("line"))


def _tokens(s):
    return {t for t in re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", (s or "").upper())}


def _brand_matches(brand, supplier):
    """Does the brand named in the spec appear in the winner's name?

    Whole-word, first significant token of the brand: "Ford" matches "FORD
    MOTOR COMPANY OF CANADA", "Leica" matches "LEICA MICROSYSTEMS". Not
    fuzzy - a fuzzy match between a brand and a company name is the kind of
    coincidence that becomes an accusation.
    """
    bt = [t for t in re.findall(r"[A-Za-zÀ-ÿ0-9]{3,}", (brand or "").upper())]
    return bool(bt) and bt[0] in _tokens(supplier)


def build_case(m1, m2, m3):
    ref = (m1 or {}).get("reference") or (m2 or {}).get("reference")
    claims = []

    # ---- M1: notice signals. Their source is the notice field itself.
    for s in (m1 or {}).get("signals", []):
        if s["weight"] == "info":
            continue
        claims.append({
            "agent": "M1", "kind": s["signal"], "level": 0,
            "confidence": "declared" if s["weight"] == "strong" else "weak",
            "days": s.get("days"), "median": s.get("median"),
            "claim": s["why"],
            "source": {"file": "tender notice (CanadaBuys feed)",
                       "where": s["signal"], "line": s["evidence"],
                       "context": None},
        })

    # ---- M2: documents
    for f in (m2 or {}).get("findings", []):
        c = dict(f)
        c["agent"] = "M2"
        claims.append(c)

    # ---- M3: awards
    for f in (m3 or {}).get("findings", []):
        c = dict(f)
        c["agent"] = "M3"
        claims.append(c)

    # ---- cross-check: is the brand the spec locks in the one that won?
    award = (m3 or {}).get("award") or {}
    winner = award.get("supplier")
    if winner:
        for f in (m2 or {}).get("findings", []):
            if f.get("kind") == "brand_without_equivalent" and \
                    _brand_matches(f.get("brand"), winner) and \
                    _verified(f.get("source")):
                claims.append({
                    "agent": "M4", "kind": "brand_matches_winner", "level": 1,
                    "confidence": "high",
                    "claim": f"The specification names '{f['brand']}' "
                             f"without accepting an equivalent, and the "
                             f"contract was awarded to {winner}.",
                    "source": {"file": f["source"].get("file"),
                               "where": f["source"].get("where"),
                               "line": f["source"].get("line"),
                               "context": f"award: {winner} "
                                          f"{award.get('date', '')}"},
                })

    for c in claims:
        c["verified"] = _verified(c.get("source"))

    # ---- score
    has_doc_evidence = any(c["verified"] and c.get("agent") in ("M2", "M4")
                           and c["kind"] not in LEVEL2_KINDS for c in claims)
    used = {"brand_without_equivalent": 0, "level2": 0}
    breakdown = []
    score = 0
    for c in claims:
        k = c["kind"]
        w = WEIGHTS.get(k, 0)
        why = None
        if not c["verified"]:
            w, why = 0, "no checkable source"
        elif k in CONTEXT_KINDS and not has_doc_evidence:
            w, why = 0, "context only - no document finding to attach to " \
                        "(decision 4)"
        elif k == "brand_without_equivalent":
            if used[k] >= CAPS[k]:
                w, why = 0, "cap reached - repetition is not corroboration"
            used[k] += 1
        elif k in LEVEL2_KINDS:
            if used["level2"] >= CAPS["level2"]:
                w, why = 0, "level-2 cap reached"
            used["level2"] += 1
        c["points"] = w
        if why:
            c["not_scored_because"] = why
        score += w
        breakdown.append((k, w))

    can_alert = any(c["verified"] and c["kind"] in
                    ("brand_without_equivalent", "brand_matches_winner")
                    for c in claims)
    # Level 2 may ADD to a case, never MAKE one. Measured: its top-ranked
    # peer finding was a semi-trailer compared with pickup trucks, and with
    # level 2 alone allowed to reach WATCH, that false positive did. A
    # signal measured not to work cannot be what puts a tender in front of
    # an auditor.
    has_other = any(c.get("points") and c["kind"] not in LEVEL2_KINDS
                    and (DECLARED_ALONE == "watch"
                         or c["kind"] not in DECLARED)
                    and (CONTEXT_ALONE == "watch"
                         or c["kind"] not in CONTEXT_ONLY)
                    for c in claims)
    # Measured 2026-10-19: one brand (3) + an ACAN (2) + no trade agreement
    # (1) = 6 reached ALERT, against the routing rule - notice signals are
    # what the buyer declared, so they can rank a case, never make it an
    # alert. An alert now needs ALERT points from document findings alone
    # (two brands, or a brand that is the winner). ALERT_FROM = "all"
    # restores the earlier rule.
    doc_score = sum(c.get("points") or 0 for c in claims
                    if c.get("agent") in ("M2", "M4")
                    and c["kind"] not in LEVEL2_KINDS)
    alert_score = doc_score if ALERT_FROM == "documents" else score
    if alert_score >= ALERT and can_alert:
        route = "alert"
    elif score >= WATCH and has_other:
        route = "watch"
    else:
        route = "close"

    return {
        "reference": ref,
        "title": (m1 or {}).get("title", ""),
        "entity": (m1 or {}).get("entity", ""),
        "method": (m1 or {}).get("method", ""),
        "published": (m1 or {}).get("published", ""),
        "route": route,
        "score": score,
        "breakdown": breakdown,
        "claims": claims,
        "narrative": (narrative(route, claims, award)
                      if NARRATIVE == "v1"
                      else narrative_plain(route, claims, award, m2)),
        "readable": bool((m2 or {}).get("readable")),
        "award": award or None,
        "trace": {"M1": f"route={(m1 or {}).get('route')}",
                  "M2": (m2 or {}).get("trace", []),
                  "M3": (m3 or {}).get("trace", [])},
    }


def narrative_plain(route, claims, award, m2):
    """The case page text: why it is listed, what we could not check, the
    supplier's history. Built only from scored, verified claims."""
    for c in claims:
        c["plain"] = plain_text(c, award)
        c["nature"] = NATURE.get(c["kind"], "Context")
    scored = sorted([c for c in claims if c.get("points")],
                    key=lambda c: -c["points"])
    if route == "alert":
        head = ("The competition on this contract does not appear to have "
                "been genuine.")
    elif route == "watch":
        head = "This contract is worth an auditor's review."
    elif scored:
        head = ("Not in the audit queue: the only signals are what the buyer "
                "declared on the notice, or context. Kept for ranking.")
    else:
        head = ("No checkable signal suggests the competition on this "
                "contract was anything other than genuine.")
    out = [head]
    if scored:
        out.append("\nWhy it is listed" if route != "close"
                   else "\nSignals on record")
        for c in scored[:6]:
            out.append(f"- {c['plain']} ({c['nature']}.)")
    gaps = []
    m2 = m2 or {}
    if not m2.get("readable"):
        gaps.append("No tender documents were published, so the "
                    "specification was not read.")
    else:
        un = (m2.get("level1") or {}).get("unresolved") or 0
        if un:
            gaps.append(f"{un} name(s) in the documents could not be "
                        f"confirmed as brands (needs the online check or the "
                        f"language model).")
    if not award:
        gaps.append("No award notice was found, so the winner is unknown.")
    if gaps:
        out.append("\nWhat we could not check")
        out += [f"- {g}" for g in gaps]
    if award and award.get("supplier"):
        out.append("\nSupplier history")
        tot, mine = award.get("buyer_awards_in_group"), \
            award.get("to_this_supplier")
        sup = award["supplier"].rstrip(". ")
        line = f"- Awarded to {sup}."
        if tot:
            line += (f" This buyer gave {tot} contract(s) in this category to "
                     f"{award.get('distinct_suppliers')} supplier(s); "
                     f"{sup} won {mine}.")
            if not any(c["kind"] in ("recurring_supplier",
                                     "dependent_supplier") for c in claims):
                line += " No sign of a favoured supplier."
        if award.get("other_spellings"):
            line += (" Counted with its other spelling(s) in the award "
                     "notices: " + "; ".join(award["other_spellings"]) + ".")
        out.append(line)
    out.append("\nThese are hypotheses with evidence attached, not findings "
               "of wrongdoing. A person decides.")
    return "\n".join(out)


def narrative(route, claims, award):
    """Decision 3 wording. Built only from scored, verified claims."""
    scored = [c for c in claims if c.get("points")]
    if route == "close" or not scored:
        return ("No checkable signal suggests the competition on this "
                "contract was anything other than genuine.")
    head = ("The competition on this contract does not appear to have been "
            "genuine" if route == "alert" else
            "This contract shows signals worth an auditor's review")
    reasons = []
    for c in sorted(scored, key=lambda c: -c["points"]):
        s = c["source"]
        loc = s.get("file") or ""
        if s.get("where"):
            loc += f" ({s['where']})"
        reasons.append(f"{c['claim']} [{loc}]")
    out = head + ", for these reasons:\n- " + "\n- ".join(reasons[:6])
    if award and award.get("supplier"):
        out += f"\nAwarded to {award['supplier']}."
    out += ("\nThese are hypotheses with evidence attached, not findings of "
            "wrongdoing. A human decides.")
    return out
