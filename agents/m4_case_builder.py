"""
M4 CASE BUILDER.

Fuses what M1, M2 and M3 found about ONE tender into a case, checks every
claim has a source, scores it, and decides: alert / watch / close.

    from agents.m4_case_builder import build_case
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
# What the buyer DECLARES on the notice: an advance award notice, a limited
# tendering method or reason. User decision 2026-10-19: alone, these rank a
# case but never put it in the queue - measured on 4110, an ACAN alone sent
# 194 of 957 tenders (20%) to WATCH, burying any document finding. They
# still reach WATCH together with anything not declared (a brand, a short
# window, a recurring supplier ...).
DECLARED = {"acan", "limited_tendering_method", "limited_tendering_reason"}
# "No trade agreement" is context, not a sign: it only means the rule against
# naming brands does not apply. User decision 2026-10-19: it no longer lifts
# declared signals into WATCH on its own.
CONTEXT_ONLY = {"no_trade_agreement"}
# Brand vs winner: the brand is also checked against
# the winner's other spellings that M3 accepted as the same company - a
# winner written "BENDIX CVS" whose award notices also say "BENDIX
# COMMERCIAL VEHICLE SYSTEMS". Same whole-word test on each name. False
# restores the check against the winner's own name only.
BRAND_VS_SPELLINGS = True
# Wording of the case page: one sentence per reason, each
# marked declared / found in the documents / context; no internal codes.
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
        also = c.get("also_written") or []
        by = c.get("decided_by")
        return (f"The specification requires {b}"
                + (f" (also written {', '.join(also)})" if also else "")
                + " and does not accept an equivalent product"
                + (f" (settled by {by})" if by else "") + ".")
    return c.get("claim", "")
WATCH = 2
# A claim scores only if its words were CHECKED against its source
# (source["checked"]), not merely attached: M2 found them in the document
# text, M1 and M3 copied them by code from the notice field / award record,
# the M2 agent and M4's re-read passed their quote test. "attached" restores
# the weaker rule: an origin and some words are enough.
VERIFY = "checked"

LEVEL2_KINDS = {"narrow_band", "extreme_threshold", "within_document_bands"}
CONTEXT_KINDS = {"recurring_supplier", "dependent_supplier"}


def _attached(src):
    """A source a human can open: an origin AND the words it says."""
    if not src:
        return False
    origin = src.get("file") or src.get("where")
    return bool(origin) and bool(src.get("line"))


def _verified(src):
    """Attached, and its words checked against what it names."""
    return _attached(src) and (VERIFY == "attached"
                               or bool(src.get("checked")))


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


def build_case(m1, m2, m3, followups=None):
    """followups: what M4 checked again (m4_checks.py); shown on
    the case page, never scored."""
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
                       "context": None, "checked": True,
                       "checked_how": "copied from the notice field by M1"},
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
        # (name, how it is tied to the contract); the winner first
        others = [w for w in (award.get("winners") or []) if w != winner]
        names = [(winner, "")]
        if BRAND_VS_SPELLINGS:
            names += [(n, f" (also written {n} in the award notices)")
                      for n in award.get("other_spellings") or []]
        names += [(n, f"; {n} is one of the {len(others) + 1} suppliers "
                      f"it was awarded to") for n in others]
        for f in (m2 or {}).get("findings", []):
            hit = next((h for h in names
                        if _brand_matches(f.get("brand"), h[0])), None)
            if f.get("kind") == "brand_without_equivalent" and hit and \
                    _verified(f.get("source")):
                via = hit[1]
                claims.append({
                    "agent": "M4", "kind": "brand_matches_winner", "level": 1,
                    "confidence": "high",
                    "claim": f"The specification names '{f['brand']}' "
                             f"without accepting an equivalent, and the "
                             f"contract was awarded to {winner}{via}.",
                    "source": {"file": f["source"].get("file"),
                               "where": f["source"].get("where"),
                               "line": f["source"].get("line"),
                               "context": f"award: {winner} "
                                          f"{award.get('date', '')}",
                               "checked": f["source"].get("checked"),
                               "checked_how": f["source"].get(
                                   "checked_how")},
                })

    for c in claims:
        c["source_attached"] = _attached(c.get("source"))
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
            w, why = 0, ("its words were not found in the source"
                         if c["source_attached"] else "no checkable source")
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
                    and c["kind"] not in DECLARED
                    and c["kind"] not in CONTEXT_ONLY
                    for c in claims)
    # An alert needs ALERT points from document findings alone (two brands,
    # or a brand that is the winner): notice signals are what the buyer
    # declared - they rank a case, never make it an alert. Counted together,
    # one brand + an ACAN + no trade agreement reached ALERT (2026-10-19,
    # the buyer's own two vans being repaired).
    doc_score = sum(c.get("points") or 0 for c in claims
                    if c.get("agent") in ("M2", "M4")
                    and c["kind"] not in LEVEL2_KINDS)
    if doc_score >= ALERT and can_alert:
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
        "narrative": narrative_plain(route, claims, award, m2, followups),
        "followups": list(followups or []),
        "readable": bool((m2 or {}).get("readable")),
        "award": award or None,
        "trace": {"M1": f"route={(m1 or {}).get('route')}",
                  "M2": (m2 or {}).get("trace", []),
                  "M3": (m3 or {}).get("trace", [])},
    }


# Case page, "Brand names checked": every name the documents raised and who
# settled it - a rule, Wikidata, or the M2 agent (decided_by).
NAMES_SHOWN = 8
NAME_RESULT = {"finding": "kept as a finding", "cleared": "cleared",
               "open": "not settled"}


def narrative_plain(route, claims, award, m2, followups=None):
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
    names = [n for n in m2.get("names") or [] if n["result"] != "dropped"]
    dropped = len(m2.get("names") or []) - len(names)
    if names:
        out.append("\nBrand names checked")
        out += [f"- {n['name']}: {NAME_RESULT[n['result']]} - "
                f"{n['decided_by']}" for n in names[:NAMES_SHOWN]]
        more = len(names) - NAMES_SHOWN
        if more > 0 or dropped:
            out.append("- " + "; ".join(
                ([f"{more} more"] if more > 0 else [])
                + ([f"{dropped} other name(s) dropped as not brands"]
                   if dropped else []))
                + " (technical details)")
    if award and award.get("supplier"):
        out.append("\nSupplier history")
        tot, mine = award.get("buyer_awards_in_group"), \
            award.get("to_this_supplier")
        sup = award["supplier"].rstrip(". ")
        others = [w for w in award.get("winners") or [] if w != award["supplier"]]
        line = f"- Awarded to {sup}."
        if others:
            line += (f" The award notices name {len(others)} other "
                     f"supplier(s) for this tender: " + "; ".join(others)
                     + ".")
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
    if followups:
        out.append("\nChecked again before ranking")
        out += [f"- {x['text']}" for x in followups]
    out.append("\nThese are hypotheses with evidence attached, not findings "
               "of wrongdoing. A person decides.")
    return "\n".join(out)
