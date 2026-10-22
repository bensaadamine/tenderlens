"""
M2 SPEC AUDITOR - tools.

Each function answers ONE question about ONE tender. That is the unit the
auditor works in: a tender is what gets inspected, so a tool that needs the
whole corpus to answer is the wrong shape.

Tool 1 - brands_without_equivalent(reference)
    Detection level 1. CFTA / CETA require functional requirements: a spec may
    name a product only if it also accepts an equivalent. Returns one finding
    per brand named without that allowance, each with the document, page, line
    and sentence, so a human can check it in seconds.

Verification is a Wikidata lookup, cached in reports/brand_cache.json. The
cache is shared and grows: a brand confirmed on one tender costs nothing on
every tender after it. Commit the cache - it makes the team's runs identical.

Two rules keep false positives near zero:
  * A blanket clause ("wherever a brand name appears, read 'or equivalent'")
    makes the document compliant. Those are reported as OK, not as findings.
  * A cited standard (CSA, ISO, ASTM) is evidence of good practice and is
    never a finding.

Use as a library (how the agent calls it):
    from spec_tools import brands_without_equivalent
    result = brands_without_equivalent("PW-23-01022340")

Or from the command line, to check one tender by hand:
    python tools/spec_tools.py --tender PW-23-01022340
    python tools/spec_tools.py --tender PW-23-01022340 --offline   # cache only
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


import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TEXT_DIR = "reports/text"
CACHE_PATH = "reports/brand_cache.json"
WD_API = "https://www.wikidata.org/w/api.php"
UA = "TenderLens/0.1 (academic research)"

# --------------------------------------------------------------- the patterns
RE_CAND = re.compile(
    r"\b([A-Z][A-Za-z&.\-]{2,}(?:\s+[A-Z][A-Za-z&.\-]{2,})?)\b")

# A model number beside a word makes that word much more likely to be a brand.
RE_MODEL_NEAR = re.compile(r"(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9\-]{1,8}\b")

# The model code as the VERY NEXT TOKEN after the name: "Leica TS16",
# "Michelin 275/80R22.5", "Caterpillar 320". Anchored at the start of the
# text that follows the candidate.
#
# Measured 2026-10-15: the old per-candidate test accepted RE_MODEL_NEAR
# anywhere within 25 characters BEFORE or after the name, and every one of
# the 13 rule-based findings on 400 tenders of 4110 - and all 3 alerts the
# first full TenderLens run raised on 2510 - was false:
#     "PPP-C-96, Type V, Round, Square, Oval"       spec reference before
#     "in column 76. Ensure that the number"         column number before
#     "( ) B2. The Bidder is a Joint Venture"        form item before
# A number BEFORE a word is a clause, a column, a standard. The model of a
# product follows its maker's name.
#
# An explicit introducer may sit between name and code: "The crane must be
# Palfinger model PK11001 or Equivalent". The first version of this rule
# dropped exactly that line - the one real brand among the 6 verified
# candidates across 2510 and 4110 - while correctly dropping the other 5.
# "type" and "no." are deliberately NOT introducers: "Type V" and "No. 3"
# are how specifications cite classes and clauses.
RE_MODEL_NEXT = re.compile(
    r"^[\s\-]{0,2}"
    r"(?:(?:model|modèle|series|série|p/?n|part\s+(?:no\.?|number))"
    r"\s*[:#]?\s*)?"
    r"(?=[A-Za-z0-9\-/.]*\d)[A-Z0-9][A-Za-z0-9\-/.]{1,14}\b", re.I)

# Capitalised because a sentence begins here, mid-line: "column 76. Ensure".
RE_YEAR = re.compile(r"(?:19|20)\d\d")

RE_SENTENCE_START = re.compile(r"[.!?;:]\s+$")

RE_TRADEMARK = re.compile(r"[®™]")

# Lines that explicitly introduce a product.
RE_MAKE_CONTEXT = re.compile(
    r"\b(?:manufacturer|manufactured by|make|makes|brand|brand name|"
    r"trade name|trademark|model|part number|p/?n|oem|supplier of|"
    r"produced by|marque|fabricant|modèle)\b", re.I)

RE_OR_EQUIV = re.compile(
    r"or\s+(?:an?\s+)?(?:approved\s+)?equivalent"
    r"|or\s+(?:an?\s+)?equal\b|or\s+better\b|or\s+comparable\b"
    r"|ou\s+(?:l['’])?équivalent|ou\s+(?:un\s+)?équivalent"
    r"|\bequivalent\s+(?:product|model|item)s?\b", re.I)

RE_EQUIV_TO = re.compile(r"equivalent\s+to\b|équivalent\s+à\b", re.I)

# A blanket clause that legalises every brand mention in the document.
RE_DOC_CLAUSE = re.compile(
    r"(?:any|all|each|wherever|where|whenever)\b[^.\n]{0,80}"
    r"\b(?:brand|trade)\s*names?\b[^.\n]{0,120}\bequivalent\b"
    r"|\bbrand\s*names?\b[^.\n]{0,60}\bfor\s+reference\s+only\b"
    r"|\bno\s+intention\s+to\s+restrict\b[^.\n]{0,80}\bequivalent\b"
    r"|\breference\s+to\s+a\s+(?:brand|trade)\s*name\b[^.\n]{0,120}"
    r"\bequivalent\b", re.I)

# Canada's standard "Equivalent Products" clause, written once per tender:
# "Products that are equivalent in form, fit, function and quality to the
# item(s) specified ... will be considered where the Bidder: designates the
# brand name, model ...". It permits an equivalent for EVERY brand the tender
# names. Measured 2026-10-17: present in 111 of the 200 2510 tenders with
# text (29 of 396 in 4110), recognised by RE_DOC_CLAUSE in none - only the
# LLM judge read it. --l1-equiv-clause off restores the earlier behaviour.
EQUIV_CLAUSE = True
# Measured 2026-10-19: "A Bendix Model AD9 ... must be provided" became the
# candidate "Bendix Model", and the LLM triage answered "'Bendix Model' is
# not a standalone brand" - losing a real lock-in. A trailing generic word
# (model, series, type, part no.) is not part of a maker's name.
# --l1-name-trim off restores.
NAME_TRIM = True
# Measured 2026-10-19: "Axis" (Axis Communications, a real lock-in) was cached
# as the ordinary word "shaft" - an ALIAS match - and the triage guard then
# hid it. Only an entry whose own label IS the name counts as the ordinary
# word. --l1-ordinary alias restores the earlier rule.
ORDINARY_EXACT = True
RE_NAME_TAIL = re.compile(
    r"(?:\s+(?:models?|series|type|part|p/?n|no\.?|modèle|série))+$", re.I)

# Measured 2026-10-19: Wikidata knows "Bendix" (like "Allison") only as a
# family name, so the verdict was "not a brand" and the name never reached
# the LLM. Makers are often named after people. A name whose only exact
# matches are personal names is now UNKNOWN (left to the LLM), not "not a
# brand". --l1-person-names not-brand restores.
PERSON_UNKNOWN = True
RE_PERSON_NAME = re.compile(
    r"\b(?:given name|surname|family name|male given name|female given name)\b",
    re.I)

# Which tenders --llm reaches (read by tenderlens.run_one): "unresolved"
# (default, user decision 2026-10-17) = any tender whose rules leave names
# unconfirmed or a finding to judge; "deep" = only tenders M1 routes deep.
LLM_SCOPE = "unresolved"
RE_EQUIV_CLAUSE = re.compile(
    r"\bequivalent\s+in\s+form\s*,?\s*fit\s*,?\s*(?:and\s+)?function"
    r"|\béquivalents?\b[^.\n]{0,60}\bforme\b[^.\n]{0,60}\bfonction"
    r"|\b(?:substituts?|produits?)\b[^.\n]{0,60}\béquivalents?\b[^.\n]{0,100}"
    r"\bpris\s+en\s+considération", re.I)

# A cited standard is the opposite of brand lock-in.
STANDARDS = {
    "csa", "ulc", "ansi", "astm", "nfpa", "iec", "din", "nema", "sae", "mil",
    "ieee", "nist", "cgsb", "iso", "en", "ul", "api", "awwa", "aashto", "cga",
    "tssa", "bifma", "ansi/bifma", "cmvss", "fmvss", "epa", "nsf", "fcc",
    # added 2026-10-17 when table cells became candidates
    "ppp", "unece", "ece", "cfps", "aap", "stanag", "dnd", "nato",
}

# Structural, legal and SACC-boilerplate vocabulary. Without this list the
# standard clauses in every solicitation become brand candidates - and some
# would verify on Wikidata ("Incoterms" is a registered ICC trademark).
STRUCTURAL = {
    "annex", "annexe", "appendix", "appendice", "article", "attachment",
    "section", "clause", "paragraph", "page", "part", "phase", "item",
    "table", "figure", "note", "notes", "schedule", "amendment", "revision",
    "number", "reference", "solicitation", "tender", "contract", "request",
    "proposal", "quotation", "bidder", "bidders", "offeror", "offerors",
    "contractor", "canada", "canadian", "ottawa", "quebec", "ontario",
    "alberta", "manitoba", "saskatchewan", "nunavut", "yukon", "halifax",
    "toronto", "montreal", "vancouver", "winnipeg", "edmonton", "calgary",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    "class", "type", "group", "level", "option", "line", "column", "row",
    "total", "version", "form", "chapter", "exhibit", "addendum",
    "department", "national", "defence", "defense", "public", "services",
    "procurement", "government", "minister", "ministry", "agency", "crown",
    "work", "works", "goods", "service", "delivery", "warranty", "payment",
    "price", "cost", "quantity", "description", "specification", "general",
    "mandatory", "technical", "evaluation", "criteria", "requirement",
    "requirements", "bid", "bids", "award", "closing", "date", "time",
    "the", "and", "all", "any", "for", "with", "this", "that", "shall",
    "must", "will", "may", "each", "such", "other", "new", "used",
    "english", "french", "yes", "no", "n/a", "tbd", "samples", "sample",
    # the context words themselves: "Manufacturer" is described on Wikidata as
    # "a company that produces goods", so it would verify as a brand
    "make", "makes", "manufacturer", "manufacturers", "manufactured",
    "model", "models", "brand", "brands", "trade", "trademark", "part",
    "parts", "supplier", "suppliers", "vendor", "vendors", "oem",
    "product", "products", "equivalent", "unit", "units", "equipment",
    "system", "systems", "assembly", "kit", "set", "series",
    # SACC standard-clause vocabulary
    "incoterms", "incoterm", "applicable", "trading", "partner", "partners",
    "pursuant", "withholding", "termination", "compliance", "applicant",
    "ownership", "accounts", "basis", "right", "rights", "parties", "code",
    "colonel", "sacc", "trafficking", "source", "act", "authority",
    "material", "customs", "tariff", "income", "insurance", "liability",
    "indemnity", "arbitration", "nscm", "cage", "ncage", "daat", "rdims",
    # added 2026-10-17 when table cells and field values became candidates:
    # words that open a line before a number without naming a product
    "bldg", "building", "amendement", "modification", "tables", "see",
    "figure", "valid", "process", "testing", "fees", "draft", "index",
    "package", "method", "notes", "contact", "officer", "courriel", "email",
    "tel", "fax", "irem", "red", "white", "blue", "black", "yellow", "green",
    "newfoundland", "labrador", "brunswick", "nova", "scotia", "british",
    "columbia", "prince", "edward", "northwest", "territories",
    "minimum", "maximum", "approximately", "approx", "width", "height",
    "depth", "length", "weight", "displacement", "capacity", "subsection",
    "rule", "labels", "label", "paint", "name", "electric", "diesel",
    "gasoline", "hangar", "upcoming", "truck", "trucks", "vehicle",
    "vehicles", "power", "speed",
    "question", "questions", "answer", "answers", "response", "responses",
    "réponse", "reply", "clarification",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "janv", "févr", "avr", "juil", "déc", "refer", "per",
}

RE_IS_BRAND = re.compile(
    r"\b(?:compan(?:y|ies)|corporation|corp\.?|incorporated|manufactur\w*|"
    r"brand|marque|enterprise|business|firm|automaker|automobile make|"
    r"conglomerate|multinational|subsidiary|producer|trademark|"
    r"product line|model of|series of)\b", re.I)

RE_NOT_BRAND = re.compile(
    r"\b(?:given name|surname|family name|human|person|city|town|village|"
    r"municipalit|province|state|country|river|lake|mountain|island|"
    r"hormone|protein|gene|chemical|disease|species|genus|film|movie|"
    r"album|song|novel|book|band\b|footballer|politician|writer|actor|"
    r"unit of|number|year|month|language|letter of|standard\b)\b", re.I)


# ------------------------------------------------------------------- helpers
def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def _plausible(term):
    low = term.lower()
    head = low.split()[0]
    if low in STRUCTURAL or low in STANDARDS:
        return False
    if head in STRUCTURAL or head in STANDARDS:
        return False
    # "MIL-I-52211B", "PPP-D-723", "UN-ECE R117" - a standard, hyphenated
    if re.split(r"[-/]", head)[0] in STANDARDS:
        return False
    if len(term) < 3 or len(term) > 40:
        return False
    if not re.match(r"^[A-Z]", term):
        return False
    if term.isupper() and len(term) <= 3:
        return False
    return True


def tender_documents(reference, text_dir=None):
    """Every extracted document belonging to one tender.

    text_dir defaults to the module's TEXT_DIR, read at call time so a
    test run (tests/synth_rig.py) can point the whole system elsewhere."""
    text_dir = text_dir or TEXT_DIR
    out = []
    if not os.path.isdir(text_dir):
        return out
    for cat in sorted(os.listdir(text_dir)):
        d = os.path.join(text_dir, cat)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".txt") or not fn.startswith(reference + "__"):
                continue
            with open(os.path.join(d, fn), encoding="utf-8",
                      errors="replace") as f:
                out.append({"category": cat,
                            "document": fn[len(reference) + 2:-4],
                            "text": f.read()})
    return out


# ------------------------------------------------------------ Wikidata, cached
def _load_cache(path=CACHE_PATH):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def _save_cache(cache, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)


# How a Wikidata answer becomes a verdict. "all" (default since 2026-10-17)
# or "first" (the earlier rule). Set by --l1-verify.
#
# Measured 2026-10-17, synthetic rigged tenders, online: the first rule read
# only the FIRST entry whose label matched, so "Cummins" was a family name
# and "Allison" a given name - both "not a brand". And a name Wikidata does
# not hold ("Knapheide", "Jerr-Dan") was cached as NOT a brand, when it only
# means Wikidata does not know. Of 112 injected brands outside the cache,
# 0 were found. Now: every exact label or alias is read, one that describes
# a company wins (the candidate already has a model code beside it), and no
# match is "unknown" - left to the LLM judge, never cleared and never
# counted.
#
# Then measured online the same day: "Approval" (a Slovak company) and
# "Room" (a Czech company) verified, so "TA Approval-14 days" and "Building
# 236 Room 108" became findings in 5 real tenders. So a name that ALSO has an
# ordinary meaning on Wikidata - an exact entry that is neither a company
# nor a person or place - stays unknown. A person's name does not block:
# "Cummins" is a family name and a company, and verifies.
VERIFY_RULE = "all"
LOWER_NOT_BRAND = True   # --l1-lower-label off restores
VERIFY_VERSION = 6
# v6 (2026-10-19): ordinary word only by the entry's own label, not an alias.
# Only undecided entries are re-queried (brand / not-brand verdicts stay).
# v5 (2026-10-17): with no company among the exact matches, an ordinary-word
# entry (lowercase label) is what gets cached - "pannier" (a bag) rather than
# "Pannier" (a family name) - so the LLM triage can be kept away from it.
# Only UNDECIDED v4 entries are re-queried; v4 brand / not-brand verdicts
# are unchanged by v5 and stay fresh.


def _decide(hits, key, rule=None):
    """Verdict from Wikidata search hits: (True|False|None, label, desc)."""
    rule = rule or VERIFY_RULE
    if rule == "first":
        for h in hits:
            if (h.get("label", "") or "").lower() != key:
                continue
            desc = h.get("description", "") or ""
            if RE_NOT_BRAND.search(desc):
                return False, h.get("label", ""), desc
            if RE_IS_BRAND.search(desc):
                return True, h.get("label", ""), desc
            return None, h.get("label", ""), desc
        return False, "", "(no exact match on Wikidata)"
    exact = [h for h in hits
             if (h.get("label", "") or "").lower() == key
             or ((h.get("match") or {}).get("text", "") or "").lower() == key]
    if not exact:
        return None, "", "(no exact match on Wikidata)"
    brand_hit = ordinary = None
    for h in exact:
        desc = h.get("description", "") or ""
        # A lowercase label is an ordinary word, never a company - even when
        # its description says "enterprise" ("project": "collaborative
        # enterprise ..." made "Program" a brand, 2026-10-19).
        lower_label = (h.get("label", "") or "")[:1].islower()
        if RE_IS_BRAND.search(desc) and not RE_NOT_BRAND.search(desc) \
                and not (LOWER_NOT_BRAND and lower_label):
            brand_hit = brand_hit or h
        elif (h.get("label", "") or "")[:1].islower() and \
                not RE_NOT_BRAND.search(desc) and \
                (not ORDINARY_EXACT or (h.get("label", "") or "") == key):
            # An ORDINARY WORD: English Wikidata labels common nouns in
            # lowercase ("room", "consent", "square") and names of people,
            # places and companies capitalised. Measured 2026-10-17: without
            # the lowercase test, people blocked real brands - "Honda" hit a
            # botanist, "Cummins" a mycologist, "Ford" a badminton player.
            ordinary = ordinary or h
    if brand_hit and ordinary:
        return None, ordinary.get("label", ""), (
            "ordinary word as well as a company: "
            + (ordinary.get("description", "") or "")[:80])
    if ordinary and not brand_hit and VERIFY_VERSION >= 5:
        return None, ordinary.get("label", ""), (
            "ordinary word: " + (ordinary.get("description", "") or "")[:80])
    if brand_hit:
        return True, brand_hit.get("label", ""), \
            brand_hit.get("description", "") or ""
    if PERSON_UNKNOWN and all(
            RE_PERSON_NAME.search(h.get("description", "") or "")
            for h in exact):
        h = exact[0]
        return None, h.get("label", ""), "person's name only: " + (
            h.get("description", "") or "")
    if all(RE_NOT_BRAND.search(h.get("description", "") or "") for h in exact):
        h = exact[0]
        return False, h.get("label", ""), h.get("description", "") or ""
    h = exact[0]
    return None, h.get("label", ""), h.get("description", "") or ""


def verify_brand(name, cache=None, offline=False, timeout=20, sleep=0.3):
    """True / False / None (unclear). Cached, so each name costs one lookup
    across the whole project."""
    cache = _load_cache() if cache is None else cache
    key = name.lower()
    entry = cache.get(key)
    fresh = entry is not None and (
        VERIFY_RULE == "first" or entry.get("v") == VERIFY_VERSION
        or (entry.get("v") in (4, 5) and entry.get("brand") is not None))
    if entry is not None and LOWER_NOT_BRAND and entry.get("brand") is True \
            and (entry.get("label") or "")[:1].islower():
        return None                # cached under the old rule: an ordinary word
    if entry is not None and (fresh or offline):
        if VERIFY_RULE != "first" and not fresh and \
                entry.get("description") == "(no exact match on Wikidata)":
            return None            # old cache said "not a brand" for "unknown"
        if PERSON_UNKNOWN and entry.get("brand") is False and \
                RE_PERSON_NAME.search(entry.get("description") or "") and \
                len((entry.get("description") or "").split()) <= 4:
            return None            # a maker may carry a person's name
        return entry.get("brand")
    if offline:
        return None
    url = (WD_API + "?action=wbsearchentities&format=json&language=en"
           "&uselang=en&limit=%d&type=item&search=" % (
               3 if VERIFY_RULE == "first" else 10)
           + urllib.parse.quote(name))
    hits = None
    for wait in (0, 2, 5, 10):     # Wikidata refuses bursts; never cache that
        if wait:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                hits = json.load(r).get("search", [])
            break
        except Exception:
            continue
    if hits is None:
        return None                # never cache a network failure as a verdict
    time.sleep(sleep)
    verdict, label, desc = _decide(hits, key)
    cache[key] = {"brand": verdict, "label": label, "description": desc,
                  "v": VERIFY_VERSION if VERIFY_RULE != "first" else 1}
    _save_cache(cache)
    return verdict


# Bidders' questions in an amendment's Q&A. Text between "Question #11" and
# "Answer #11" is written by a SUPPLIER, so a brand in it is the supplier
# naming its own product, never the buyer requiring one.
#
# Measured 2026-10-15: the first --online run over 2510 produced exactly one
# level-1 finding, and it was this - "3.7 We use a Honda GX390 engine mounted
# in rear of gooseneck. Is this sufficient...?" under Question #11, answered
# "The engine must meet the requirements of the purchase description." The
# model-code rule (decision 29) correctly saw "Honda GX390"; it could not see
# who was speaking.
# The whole word is safe followed by any number. A single letter is not:
# "A 5.08 cm (2 in) rear receiver hitch" would read as Answer 5, and a loose
# "Q" could open a question block over a real requirement and hide it. So a
# single letter needs a separator right after its number - "Q1:", "A1.",
# "Q 3)" - and a dot only if no digit follows it, which rules out decimals.
_QA_NUM = r"\s*(?:#|no\.?|n[oº]\.?)?\s*\d+"
_QA_SEP = r"\s*(?:[:)\-]|\.(?!\d)|$)"
RE_QUESTION = re.compile(
    rf"^\s*(?:question{_QA_NUM}\b|q{_QA_NUM}{_QA_SEP})", re.I)
# Answers are often unnumbered ("Answer:", "Response:"). Requiring a number
# left 26 files ending INSIDE a question block, hiding the buyer's answers -
# which are buyer text and can carry real requirements.
RE_ANSWER_LOOSE = re.compile(
    rf"^\s*(?:(?:answer|response|réponse|reply)\b"
    rf"|[ar]{_QA_NUM}{_QA_SEP}|[ar]\s*[:)\-])", re.I)
# Measured 2026-10-17: "a) Seating must be ..." - a list item inside a
# bidder's question - matched the loose form as an answer marker and closed
# the question at once, so the bidder's "FORD F550" became a finding. A
# single letter now counts only as a capital A or R with a colon or dash
# ("A:", "R -") or with a number ("A1.", "R 2)"). --l1-answer-marker loose
# restores the earlier pattern.
RE_ANSWER_STRICT = re.compile(
    rf"^\s*(?:(?i:answer|response|réponse|reply)\b"
    rf"|[AR]{_QA_NUM}{_QA_SEP}|[AR]\s*[:\-])")
RE_ANSWER = RE_ANSWER_STRICT


# Where a candidate may sit - see _candidates. "cell" (default since
# 2026-10-17) or "strict" (the earlier rule). Set by --l1-position.
POSITION_RULE = "cell"
QA_LOOKAHEAD = 40       # lines; 0 = close a question at its first blank line


# ------------------------------------------------- amendments, read strictly
# Decided 2026-10-17 (user): in an AMENDMENT, level 1 reads only what the
# buyer changes or answers - change clauses ("is deleted and replaced",
# "insert", "add the following"), answer blocks, and any requirement section
# the amendment restates (some reissue the whole RFP). Everything else is
# blanked before candidates are looked for. Both false alarms so far came
# from bidders' text in amendments (Honda GX390, ProHeat X45). The "or
# equivalent" check still reads the WHOLE amendment: a buyer's allowance
# anywhere still clears. --l1-amend all restores reading everything.
AMEND_RULE = "strict"
RE_AMEND_NAME = re.compile(r"amend|amd|modif|addend|(?<![a-z])q&?a\d", re.I)
RE_AMEND_HEAD = re.compile(
    r"\b(?:amendment|amendement|modification|modificatif|addendum)\s*"
    r"(?:no\.?|n[o°º]\.?|#|number)?\s*\d", re.I)
RE_CHANGE = re.compile(
    r"\b(?:delete[sd]?|replace[sd]?|insert(?:ed)?|add(?:ed)?\s+(?:the\s+"
    r"following|to)|is\s+(?:amended|added|revised)|amended\s+to\s+read|"
    r"now\s+reads?|shall\s+now|supprim\w*|remplac\w*|ajout\w*|insér\w*|"
    r"est\s+modifi\w*)\b", re.I)
AMEND_BLOCK = 40        # lines kept after a change clause or an answer

# Measured 2026-10-17 (real 2510, online): three false findings, all fixed
# here, each with a flag restoring the earlier behaviour.
#  * "FORD F550" in a bidder's "Question 4" of an amendment: the strict view
#    blanked the question marker but kept the bidder's text because it said
#    "replace seats" (a change word), so the question was never recognised.
#    Now question markers stay visible in the view. --l1-amend-markers off.
#  * "ANSI/SAIA A92.2": a name written right after a standards body and a
#    slash is part of a standard's name. --l1-standards off.
#  * "Ford F150 /250 / 350" in a list of vehicles the brackets must fit
#    (Chevrolet, Ford, Dodge ...): three or more makes listed together is a
#    fitment or choice list, not a lock-in. --l1-make-list off.
KEEP_Q_MARKERS = True
SKIP_STANDARDS = True
MAKE_LIST = True
MAKE_LIST_MIN = 3       # distinct makes on list-shaped lines
MAKE_LIST_WINDOW = 6    # lines either side
RE_STD_PREFIX = re.compile(r"\b[A-Z]{2,6}\s*/\s*$")
RE_LIST_LINE = re.compile(
    r"^\s*(?:\d+[.)]\s*|[-*•]\s*)?([A-Z][A-Za-z\-]{2,})\s+\S")
RE_UNIT_AFTER_NUM = re.compile(
    r"\d\s*(?:kg|lbs?|mm|cm|in\b|m\b|l\b|km|kw|hp|v\b|a\b|%|°)", re.I)
_census = None


def is_amendment(doc):
    """By file name, or by the cover page ("Amendment No. ... 002")."""
    if RE_AMEND_NAME.search(doc.get("document", "")):
        return True
    head = [l for l in doc["text"].splitlines()[:200] if l.strip()][:25]
    return bool(RE_AMEND_HEAD.search("\n".join(head)))


def amendment_view(text):
    """The amendment with everything but buyer changes, answers and restated
    requirement sections blanked - same length, so every character offset and
    line number still points into the original."""
    global _census
    lines = text.split("\n")
    keep = [False] * len(lines)
    try:
        if _census is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                "census_l1", os.path.join(_ROOT, "prepare", "04_census.py"))
            _census = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(_census)
        mask, mode = _census.scope_mask(lines)
        if mode == "section":
            keep = list(mask)
    except Exception:
        pass
    # A kept block is one paragraph: it ends at the first blank line after
    # it has some text past the marker line, at a bidder's question, or
    # after AMEND_BLOCK lines.
    left, has = 0, False
    for i, line in enumerate(lines):
        if RE_QUESTION.match(line):
            left = 0                       # a bidder speaks: stop keeping
            if KEEP_Q_MARKERS:
                # visible, so _candidates knows the text after it is a
                # bidder's, even where that text holds a change word
                keep[i] = True
        elif RE_ANSWER.match(line) or RE_CHANGE.search(line):
            left, has = AMEND_BLOCK, False
            keep[i] = True
            left -= 1
            continue
        if left:
            if not line.strip():
                if has:
                    left = 0
                    continue
            else:
                has = True
            keep[i] = True
            left -= 1
    return "\n".join(l if k else " " * len(l) for l, k in zip(lines, keep))


def add_l1_args(ap):
    """The level-1 rule flags, shared by every script that runs the system."""
    ap.add_argument("--l1-position", choices=["cell", "strict"], default="cell",
                    help="where a brand may sit: 'cell' (default) also accepts "
                         "a table cell or a field value with strong support; "
                         "'strict' = the pre-2026-10-17 rule")
    ap.add_argument("--l1-qa", choices=["lookahead", "blank"],
                    default="lookahead",
                    help="end of a bidder's question: 'lookahead' (default) "
                         "runs to an answer marker within 40 lines; 'blank' = "
                         "the earlier rule (first blank line)")
    ap.add_argument("--l1-verify", choices=["all", "first"], default="all",
                    help="Wikidata verdict: 'all' (default) reads every exact "
                         "match and treats no match as unknown; 'first' = the "
                         "earlier rule")
    ap.add_argument("--l1-amend", choices=["strict", "all"], default="strict",
                    help="amendments: 'strict' (default) reads only change "
                         "clauses, answers and restated requirement sections; "
                         "'all' = read the whole amendment")
    ap.add_argument("--l1-answer-marker", choices=["strict", "loose"],
                    default="strict",
                    help="single-letter answer markers: 'strict' (default) "
                         "only 'A:'/'R-' or 'A1.'; 'loose' also accepts "
                         "'a)', which is usually a list item")
    ap.add_argument("--llm-scope", choices=["unresolved", "deep"],
                    default="unresolved",
                    help="with --llm: 'unresolved' (default) asks the LLM on "
                         "any tender whose rules leave names unconfirmed or "
                         "a finding to judge; 'deep' = only tenders that "
                         "declare limited competition (earlier rule)")
    ap.add_argument("--l1-lower-label", choices=["on", "off"], default="on",
                    help="a Wikidata entry with a lowercase label (an ordinary "
                         "word) is never a company, whatever its description")
    ap.add_argument("--l1-defined-terms", choices=["on", "off"], default="on",
                    help="an acronym the tender spells out, e.g. 'Request for "
                         "Supply Arrangement (RFSA)', is never a brand")
    ap.add_argument("--l1-urls", choices=["on", "off"], default="on",
                    help="ignore names inside web addresses")
    ap.add_argument("--l1-buyer-equipment", choices=["on", "off"], default="on",
                    help="clear a brand the text shows is the buyer's own "
                         "equipment (intended, existing, transporting, repair "
                         "of ...)")
    ap.add_argument("--l1-equiv-clause", choices=["on", "off"], default="on",
                    help="clear brands in a tender carrying the standard "
                         "'Equivalent Products' clause (equivalent in form, "
                         "fit, function and quality) - default on")
    ap.add_argument("--l1-ordinary", choices=["exact", "alias"],
                    default="exact",
                    help="ordinary-word test on Wikidata: the entry's own "
                         "label must be the name (default) or an alias "
                         "match is enough (earlier rule)")
    ap.add_argument("--l1-name-trim", choices=["on", "off"], default="on",
                    help="drop a trailing generic word from a brand name "
                         "('Bendix Model' -> 'Bendix') - default on")
    ap.add_argument("--l1-person-names", choices=["unknown", "not-brand"],
                    default="unknown",
                    help="a name Wikidata knows only as a person's name: "
                         "'unknown' (default, left to the LLM) or 'not-brand' "
                         "(earlier rule)")
    ap.add_argument("--l1-excerpts", choices=["distinct", "all"],
                    default="distinct",
                    help="judge excerpts: one per distinct line (default) or "
                         "the first 12 mentions (earlier rule)")
    ap.add_argument("--l1-judge", choices=["v2", "v1"], default="v2",
                    help="LLM judge prompt: 'v2' (default) knows equipment the "
                         "buyer already owns and discards copied replies; "
                         "'v1' = the first prompt")
    ap.add_argument("--l1-triage-guard", choices=["on", "off"], default="on",
                    help="never let the LLM triage call a Wikidata ordinary "
                         "word (e.g. 'pannier') a brand - default on")
    ap.add_argument("--l1-pdf-order", choices=["on", "off"], default="on",
                    help="accept an 'or equivalent' sentence end that the PDF "
                         "printed just above the brand - default on")
    ap.add_argument("--l1-name-edge", choices=["loose", "strict"],
                    default="loose",
                    help="finding a brand's other mentions: 'loose' (default) "
                         "accepts a hyphen right after the name (Knapheide "
                         "KSS-96); 'strict' = the earlier rule")
    ap.add_argument("--l1-amend-markers", choices=["on", "off"], default="on",
                    help="keep bidders' question markers visible in the strict "
                         "amendment view (default on; off = earlier rule)")
    ap.add_argument("--l1-standards", choices=["on", "off"], default="on",
                    help="skip a name written after a standards body and a "
                         "slash, e.g. ANSI/SAIA (default on)")
    ap.add_argument("--l1-make-list", choices=["on", "off"], default="on",
                    help="clear a brand that only appears in a list of 3+ "
                         "different makes, e.g. vehicles a part must fit "
                         "(default on)")


def apply_l1_args(a):
    global POSITION_RULE, QA_LOOKAHEAD, VERIFY_RULE, AMEND_RULE
    global KEEP_Q_MARKERS, SKIP_STANDARDS, MAKE_LIST, RE_ANSWER, NAME_EDGE
    NAME_EDGE = a.l1_name_edge
    global EQUIV_CLAUSE, LLM_SCOPE, JUDGE_VERSION, TRIAGE_GUARD, PDF_ORDER
    JUDGE_VERSION = a.l1_judge
    global NAME_TRIM, PERSON_UNKNOWN, EXCERPT_DEDUP, ORDINARY_EXACT
    ORDINARY_EXACT = a.l1_ordinary == "exact"
    NAME_TRIM = a.l1_name_trim == "on"
    PERSON_UNKNOWN = a.l1_person_names == "unknown"
    EXCERPT_DEDUP = a.l1_excerpts == "distinct"
    TRIAGE_GUARD = a.l1_triage_guard == "on"
    PDF_ORDER = a.l1_pdf_order == "on"
    EQUIV_CLAUSE = a.l1_equiv_clause == "on"
    LLM_SCOPE = a.llm_scope
    global DEFINED_TERMS, SKIP_URLS, BUYER_EQUIPMENT, LOWER_NOT_BRAND
    LOWER_NOT_BRAND = a.l1_lower_label == "on"
    DEFINED_TERMS = a.l1_defined_terms == "on"
    SKIP_URLS = a.l1_urls == "on"
    BUYER_EQUIPMENT = a.l1_buyer_equipment == "on"
    RE_ANSWER = (RE_ANSWER_STRICT if a.l1_answer_marker == "strict"
                 else RE_ANSWER_LOOSE)
    AMEND_RULE = a.l1_amend
    KEEP_Q_MARKERS = a.l1_amend_markers == "on"
    SKIP_STANDARDS = a.l1_standards == "on"
    MAKE_LIST = a.l1_make_list == "on"
    POSITION_RULE = a.l1_position
    QA_LOOKAHEAD = 40 if a.l1_qa == "lookahead" else 0
    VERIFY_RULE = a.l1_verify
RE_STRONG_MODEL = re.compile(
    r"^[\s\-]{0,2}(?:(?:model|modèle|series|série)\s*[:#]?\s*)?"
    r"(?=[A-Za-z0-9\-/.]*[A-Za-z])(?=[A-Za-z0-9\-/.]*\d)"
    r"[A-Z0-9][A-Za-z0-9\-/.]{1,14}\b", re.I)


def _strong_support(tail, rest):
    """A name opening a line or a field is a brand only with strong support:
    a model code holding letters AND digits ("GX390", not "12"), or a short
    cell - at most three more words on the line ("Fontaine 6000")."""
    if RE_TRADEMARK.search(tail[:3]) or RE_STRONG_MODEL.match(tail):
        return True
    return len(rest.split()) <= 3


# ------------------------------------------------------------------- the tool
def _candidates(text):
    """Brand candidates in one document, with where they are."""
    found = {}
    lines = text.splitlines()
    run = 0
    # A bidder's question is ONE PARAGRAPH: the block closes at an answer
    # marker, or at the first blank line after the question's text. Without
    # the blank-line close, a stray "Question 7" inside a catalogue RFSO
    # body opened a block of 4,943 lines and hid most of its specification.
    #
    # Measured 2026-10-17: the blank-line close let a real bidder's text
    # through - "Question 28" quotes the spec clause, a blank line, then
    # "We will not provide a battery blanket as the coach is configured with
    # a ProHeat X45 Plus ..." read as the buyer naming a brand. So when an
    # answer marker follows within QA_LOOKAHEAD lines, the block runs to it;
    # only without one does the blank-line close apply. 0 restores the old
    # rule (--l1-qa blank).
    in_question = has_text = False
    until = -1
    for i, line in enumerate(lines):
        start, run = run, run + len(line) + 1
        mq = RE_QUESTION.match(line)
        if mq:
            in_question = True
            has_text = bool(line[mq.end():].strip(" :.)-#\t"))
            until = -1
            for j in range(i + 1, min(len(lines), i + 1 + QA_LOOKAHEAD)):
                if RE_QUESTION.match(lines[j]):
                    break
                if RE_ANSWER.match(lines[j]):
                    until = j
                    break
        elif in_question:
            if RE_ANSWER.match(line):
                in_question = False
            elif i < until:
                pass               # an answer is coming: still the question
            elif not line.strip():
                if has_text:
                    in_question = False
            else:
                has_text = True
        if in_question:
            continue
        if len(line) > 400:
            continue
        # table-of-contents leader: "International Sanctions ........ 12"
        if "....." in line:
            continue
        tm = bool(RE_TRADEMARK.search(line))
        ctx = bool(RE_MAKE_CONTEXT.search(line))
        model = bool(RE_MODEL_NEAR.search(line))
        if not (tm or ctx or model):
            continue
        for m in RE_CAND.finditer(line):
            term = m.group(1).strip(" .-&")
            if NAME_TRIM:
                term = RE_NAME_TAIL.sub("", term).strip(" .-&") or term
            if not _plausible(term):
                continue
            # Wikidata confirms ordinary English words that happen to be
            # brands: "Ensure" (an Abbott drink), "Square" (payments),
            # "Joint Venture". Measured on this corpus, those alone were 80%
            # of the findings. So a ONE-WORD candidate needs corroboration
            # right beside it - a trademark symbol, or a model number within
            # a few characters - not merely somewhere on the same line.
            # This applies to multi-word candidates too ("Joint Venture" was
            # 36 of 50 findings without it). A real brand lock-in in a
            # specification reads "Leica TS16" or "Michelin 275/80R22.5" - the
            # model or size is right there. A legal phrase never has one.
            # "Within a few characters" was then implemented as 25 characters
            # either side, which let a clause number BEFORE the word count -
            # see RE_MODEL_NEXT. Now: a trademark symbol, or the model code
            # as the very next token.
            tail = line[m.end():m.end() + 25]
            mm = RE_MODEL_NEXT.match(tail)
            # "01 Dec 2025", "Edition 2023": a year is not a model code.
            if mm and RE_YEAR.fullmatch(mm.group(0).strip(" -")):
                mm = None
            if not (RE_TRADEMARK.search(tail[:3]) or mm):
                continue
            # Capitalised because a sentence starts here, mid-line - or
            # because it opens the line or a numbered item ("Ensure all
            # other products function properly").
            #
            # Measured 2026-10-17 with synthetic rigged tenders: rejecting
            # every such position missed ALL brands written as a table cell
            # (pdfminer puts one cell per line, "Honda GX390" opens its line)
            # and all brands in a field ("make and model: Honda GX390").
            # POSITION_RULE "cell" (default) keeps those when the support is
            # strong: the model code mixes letters and digits, or the rest of
            # the line is a short cell. "strict" restores the old rule.
            after_colon = bool(RE_SENTENCE_START.search(line[:m.start()]))
            at_start = bool(re.fullmatch(r"\s*(?:\d+[.)]\s*|[-*•]\s*)?",
                                         line[:m.start()]))
            if after_colon or at_start:
                if POSITION_RULE == "strict" or \
                        line[:m.start()].rstrip().endswith((".", "!", "?", ";")):
                    continue
                if not _strong_support(tail, line[m.end():]):
                    continue
            # "Square/Carre:", "Colour:" - a field label, not a product.
            if tail[:1] in (":", "/", "="):
                continue
            # "ANSI/SAIA A92.2", "CSA/ISO 9001": part of a standard's name.
            if SKIP_STANDARDS and RE_STD_PREFIX.search(line[:m.start()]):
                continue
            key = term.lower()
            found.setdefault(key, {"name": term, "occurrences": []})
            found[key]["occurrences"].append({
                "page": text.count("\f", 0, start) + 1,
                "line_no": i + 1,
                "pos": start + m.start(),
                "end": start + m.end(),
                "line": _norm(line)[:300],
                "trademark": tm, "make_context": ctx,
            })
    return list(found.values())


# A numeric requirement beside the brand, so the line can be ranked.
RE_REQ_LINE = re.compile(
    r"\b(?:must|shall|required?|minimum|maximum|min\.?|max\.?|"
    r"not\s+exceed|at\s+least)\b|≥|≤|>=|<=", re.I)

# Lines that mention a brand without specifying it: packing lists, titles,
# file names, delivery manifests. Weak evidence even when the brand is real.
RE_WEAK_LINE = re.compile(
    r"\b(?:quick\s+guide|user\s+manual|guide|manual|datasheet|data\s+sheet|"
    r"brochure|catalogue|catalog|quantity|qty|packing|annex|appendix|"
    r"table\s+of\s+contents|\.pdf|\.docx?|\.xlsx?)\b", re.I)


# ------------------------------------------------------------ LLM judgement
# The rules decide nothing on their own. They find candidates cheaply and hand
# every one, with its evidence line, to a model that can read context. That is
# what the Leica false positive needed: "Leica products (or equivalent) ... are
# mandatory" is obvious to a reader and invisible to a proximity window.
try:
    import llm
except ImportError:
    llm = None

TRIAGE_SYSTEM = (
    "You identify commercial product and company names in government "
    "procurement documents. You answer only with JSON, no prose.")

TRIAGE_PROMPT = """For each numbered item, decide whether the NAME is the name of a
real commercial company, brand or product model.

Answer false for: ordinary English or French words, legal and contractual
phrases, place names, street addresses, people's names, job titles, document
or section titles, technical standards (CSA, ISO, ASTM, BIFMA), units, and
government bodies.

Answer true only for a manufacturer, a brand, or a specific product model.

Items:
%s

Reply with a JSON array, one object per item, nothing else:
[{"n": 1, "company": true, "why": "Swiss surveying equipment manufacturer"}]
"""

JUDGE_SYSTEM = (
    "You audit public procurement specifications against trade-agreement "
    "rules. You answer only with JSON, no prose. You are cautious: you report "
    "a problem only when the text shows it.")

JUDGE_PROMPT = """Canadian trade agreements (CFTA, CETA) require procurement
requirements to be functional. A specification may name a brand or product only
if it also permits an equivalent.

Brand named: %s

Every place this tender mentions it, with surrounding text:

%s

EVERY sentence in this tender that allows an equivalent, anywhere in any of its
documents:

%s

Decide: does this tender REQUIRE this specific product without permitting an
equivalent?

Answer "locked" false if any allowance above covers this product. An allowance
written over a LIST or a TABLE covers every item in it - "The following Leica
products (or equivalent) in the quantities listed are mandatory" permits an
equivalent for every row that follows, including accessories, software and
licences, even though each row names only its own part number. The allowance
does not have to sit next to the product, or name it.

Answer "locked" false too if the name is only mentioned incidentally: in a
document title, a file name, a delivery address, a list of manuals, or as
software the buyer already owns and must stay compatible with.

Answer "locked" true only when the product is mandatory and NO allowance above
can reasonably be read as covering it.

Reply with JSON only:
{"locked": true, "confidence": "high", "reason": "one sentence",
 "quote": "the exact sentence that decides it"}
"""

# v2 (2026-10-17). Measured on the first full LLM run: the judge locked
# equipment the buyer ALREADY OWNS - "Trailer Boat of configuration Zodiac
# Hurricane 540", the "Fat Truck" a hauler trailer carries, "Internet
# Explorer 11" in the buyer's workstation description - and three replies
# copied the example JSON verbatim ("reason": "one sentence", locked true).
# v2 names those cases and gives an example with nothing to copy; a reply
# that still echoes it is discarded (judge_reply_ok). --l1-judge v1 restores.
JUDGE_PROMPT_V2 = """Canadian trade agreements (CFTA, CETA) require procurement
requirements to be functional. A specification may name a brand or product only
if it also permits an equivalent.

Brand named: %s

Every place this tender mentions it, with surrounding text:

%s

EVERY sentence in this tender that allows an equivalent, anywhere in any of its
documents:

%s

Decide: does this tender REQUIRE the bidder to supply this specific product,
without permitting an equivalent?

Answer "locked" false if any allowance above covers this product. An allowance
written over a LIST or a TABLE covers every item in it, even though each row
names only its own part. The allowance does not have to sit next to the
product, or name it.

Answer "locked" false too if the name is NOT something the bidder must supply:
- equipment the buyer ALREADY OWNS that the purchased item must fit, carry,
  tow, mount or connect to (a trailer built for a named boat or vehicle, a
  bracket for the buyer's existing vehicles, a camera the buyer provides);
- the buyer's own computer or software environment the bidder's system must
  run in (an operating system, a browser);
- a mention in a title, a file name, an address, a list of manuals, or a
  bidder's question.

Answer "locked" true only when the bidder must supply that product and NO
allowance above can reasonably be read as covering it.

Reply with JSON only, filling every field in your own words:
{"locked": <true or false>, "confidence": "<high, medium or low>",
 "reason": "<why, in your own words>",
 "quote": "<the exact sentence from the tender that decides it>"}
"""
JUDGE_VERSION = "v2"


def judge_reply_ok(j):
    """A usable verdict: a boolean, a real reason, and a quote."""
    if not isinstance(j, dict) or not isinstance(j.get("locked"), bool):
        return False
    reason = str(j.get("reason", "")).strip().lower()
    if reason in ("", "one sentence", "<why, in your own words>") or \
            reason.startswith("<"):
        return False
    return bool(str(j.get("quote", "")).strip())


def llm_triage(names, evidence, verbose=False):
    """Which of these candidate names are real companies or products?

    One call for the whole batch - Groq's free tier is capped per minute, so
    asking once about forty names is the difference between working and not.
    Returns {name_lower: True/False}. On any failure returns {} and the caller
    keeps its own verdict, rather than losing findings to a dead key.
    """
    if llm is None or not names:
        return {}
    items = "\n".join(
        f'{i}. NAME: "{n}"  CONTEXT: {evidence.get(n.lower(), "")[:160]}'
        for i, n in enumerate(names, 1))
    try:
        out = llm.ask_json(TRIAGE_SYSTEM, TRIAGE_PROMPT % items,
                           max_tokens=120 * len(names) + 200, verbose=verbose)
    except llm.NoProvider:
        return {}
    except Exception as e:
        if verbose:
            print(f"    triage failed: {type(e).__name__}: {e}")
        return {}
    result = {}
    for row in out if isinstance(out, list) else out.get("items", []):
        try:
            n = names[int(row["n"]) - 1]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        result[n.lower()] = bool(row.get("company"))
    return result


def llm_judge_lock(brand, excerpts, allowances=(), verbose=False):
    """Is the tender locked to this brand? Returns the model's dict or None.

    `allowances` is every equivalence clause in the tender, regardless of where
    it sits. Without it the model judged "Cyclone Software" a violation while
    eighteen lines above its table the tender said "The following Leica
    products (or equivalent) ... are mandatory". A clause written over a list
    covers the whole list, and no window around one product can see that.
    """
    if llm is None or not excerpts:
        return None
    blob = "\n\n".join(f"[{i}] {e}" for i, e in enumerate(excerpts[:12], 1))
    allow = ("\n\n".join(f"({i}) {a}" for i, a in enumerate(allowances[:12], 1))
             or "(none found anywhere in this tender)")
    try:
        # Generous budget: the strong models are reasoning models and the
        # visible answer is whatever is left after they think.
        prompt = JUDGE_PROMPT_V2 if JUDGE_VERSION == "v2" else JUDGE_PROMPT
        j = llm.ask_json(JUDGE_SYSTEM, prompt % (brand, blob, allow),
                         strong=True, max_tokens=3000, verbose=verbose)
        if JUDGE_VERSION == "v2" and not judge_reply_ok(j):
            if verbose:
                print(f"    judge reply unusable for {brand}: {str(j)[:120]}")
            return None
        return j
    except llm.NoProvider:
        return None
    except Exception as e:
        if verbose:
            print(f"    judge failed: {type(e).__name__}: {e}")
        return None


# Measured 2026-10-17 (first LLM run): "Knapheide KSS-96 or equivalent" was
# reported as a lock-in. The candidate name had absorbed the letters of the
# model code ("Knapheide KSS"), and the whole-word search that looks for the
# allowance and collects the judge's excerpts refused a match followed by "-"
# ("KSS" then "-96"), so it saw neither the "or equivalent" nor any excerpt -
# the judge was never even called. A hyphen after the name now counts as a
# word edge. --l1-name-edge strict restores the earlier rule.
NAME_EDGE = "loose"


def _brand_pattern(brand):
    tail = r"(?![\w-])" if NAME_EDGE == "strict" else r"(?!\w)"
    return re.compile(r"(?<![\w-])" + re.escape(brand) + tail, re.I)


# Measured 2026-10-17: the fast triage model called "Pannier" ("Pannier 17L
# Black", a bicycle bag) and "DSCO" companies. A name Wikidata knows as an
# ORDINARY WORD (an exact entry with a lowercase label - "pannier", "room")
# is not offered to triage at all; it stays undecided. --l1-triage-guard off
# restores the earlier behaviour.
TRIAGE_GUARD = True


def _ordinary_word(cache, key):
    e = cache.get(key) or {}
    label = e.get("label") or ""
    return bool(label[:1].islower()) or \
        str(e.get("description", "")).startswith("ordinary word")


def _excerpts(docs, brand, pad=320, limit=12):
    """Every mention of the brand in the tender, with text around it, for the
    model to read. Taken from the RAW text - the candidate filter is not
    allowed to hide the sentence that proves compliance."""
    pat = _brand_pattern(brand)
    out, seen, lines_seen = [], set(), set()
    for d in docs:
        text = d["text"]
        for m in pat.finditer(text):
            # Measured 2026-10-19: "Fat Truck(R) Hauler Trailer" is repeated
            # 50 times as a title, so the first 12 excerpts were all titles
            # and the judge never saw "a trailer ... for transporting a Fat
            # Truck" - the buyer's own vehicle. One excerpt per distinct
            # line. --l1-excerpts all restores.
            if EXCERPT_DEDUP:
                ls = text.rfind("\n", 0, m.start()) + 1
                le = text.find("\n", m.end())
                key = _norm(text[ls:le if le >= 0 else len(text)]).lower()
                if key in lines_seen:
                    continue
                lines_seen.add(key)
            a = max(0, m.start() - pad)
            b = min(len(text), m.end() + pad)
            frag = _norm(text[a:b])
            if frag in seen:
                continue
            seen.add(frag)
            out.append(f"{d['document']}: ...{frag}...")
            if len(out) >= limit:
                return out
    return out


EXCERPT_DEDUP = True


def _allowances(docs, pad=260, limit=12):
    """Every equivalence clause in the tender, wherever it sits. A clause over
    a table covers the table, so these must be judged against every brand, not
    only the ones they happen to sit beside."""
    out, seen = [], set()
    for d in docs:
        text = d["text"]
        for pat in (RE_OR_EQUIV, RE_DOC_CLAUSE, RE_EQUIV_CLAUSE):
            for m in pat.finditer(text):
                a = max(0, m.start() - pad)
                b = min(len(text), m.end() + pad)
                frag = _norm(text[a:b])
                k = frag[:80].lower()
                if k in seen:
                    continue
                seen.add(k)
                out.append(f"{d['document']}: ...{frag}...")
                if len(out) >= limit:
                    return out
    return out


# Measured 2026-10-17: "All wheel ends must be provided with stainless steel
# Bearing Buddy® wheel / bearing protectors or equivalent." - pdfminer printed
# the END of the sentence ("bearing protectors or equivalent.") a few lines
# ABOVE the brand. When the brand's line stops mid-sentence and a nearby line
# just above starts in lowercase (a continuation fragment) and ends in "or
# equivalent", that is this sentence's end. --l1-pdf-order off restores.
PDF_ORDER = True
RE_EQUIV_TAIL = re.compile(
    r"(?:or\s+(?:an?\s+)?(?:approved\s+)?(?:equivalent|equal)|"
    r"ou\s+(?:l['’]\s*)?(?:un\s+)?équivalent)\s*[.;]?\s*$", re.I)


def _tail_moved_up(text, m, lines_up=4):
    ls = text.rfind("\n", 0, m.start()) + 1
    le = text.find("\n", m.end())
    rest = text[m.end():le if le >= 0 else len(text)]
    if re.search(r"[.;!?]\s*$", rest):
        return False                       # the brand's sentence ends here
    above = text[max(0, ls - 600):ls].split("\n")
    seen = 0
    for line in reversed(above):
        t = line.strip()
        if not t or re.fullmatch(r"[\d.()a-z]{1,8}", t):
            continue                       # blank or a clause number
        seen += 1
        if t[:1].islower() and RE_EQUIV_TAIL.search(t):
            return True
        if seen >= lines_up:
            break
    return False


def _qualified_anywhere(docs, brand, window, window_before):
    """Does the tender allow an equivalent for this brand ANYWHERE?

    This must be asked of the raw text, not of the filtered candidates. The
    candidate filter demands a model number beside the brand, which is exactly
    what the exonerating sentence lacks: "The following Leica products (or
    equivalent) ... are mandatory" names no model. Checking only filtered
    occurrences therefore reported a compliant tender as a violation.
    """
    pat = _brand_pattern(brand)
    for d in docs:
        text = d["text"]
        for m in pat.finditer(text):
            after = text[m.end():m.end() + window]
            before = text[max(0, m.start() - window_before):m.start()]
            if RE_OR_EQUIV.search(after) or RE_EQUIV_TO.search(before):
                return f"{d['document']} offset {m.start()}"
            if PDF_ORDER and _tail_moved_up(text, m):
                return f"{d['document']} offset {m.start()} (sentence end " \
                       f"printed above the brand)"
    return None


def _evidence_rank(occ):
    """Higher is better evidence that this line is where the brand is
    specified, rather than merely mentioned."""
    line = occ["line"]
    score = 0
    if occ["trademark"]:
        score += 4
    if occ["make_context"]:
        score += 3
    if RE_REQ_LINE.search(line):
        score += 3
    if RE_WEAK_LINE.search(line):
        score -= 4
    if len(line) < 25:
        score -= 1
    return score


def _in_make_list(lines, line_no):
    """Is this line one of MAKE_LIST_MIN+ list-shaped lines naming different
    makes ("Chevrolet Silverado 1500 /2500", "Ford F150 /250 / 350")?
    List-shaped: short, opens with a capitalised name, lists variants with a
    slash, carries no measurement."""
    i = line_no - 1
    lo, hi = max(0, i - MAKE_LIST_WINDOW), min(len(lines), i + MAKE_LIST_WINDOW + 1)
    makes = set()
    for line in lines[lo:hi]:
        m = RE_LIST_LINE.match(line)
        if not m or "/" not in line or len(line.split()) > 8:
            continue
        if RE_UNIT_AFTER_NUM.search(line):
            continue
        makes.add(m.group(1).lower())
    mine = RE_LIST_LINE.match(lines[i]) if i < len(lines) else None
    return bool(mine) and len(makes) >= MAKE_LIST_MIN


# Measured 2026-10-19, first full 2510 run with the LLM: 8 false brand
# findings (2 genuine: Bendix AD9 x2). Three new rules, each behind a flag.
#  * DEFINED TERMS: "Request for Supply Arrangement (RFSA)", "Arctic Mobility
#    Amphibious Vehicles (AMAV)" - an acronym the tender itself spells out is
#    its own shorthand, never a brand.                --l1-defined-terms off
#  * URLS: "PW-HP" taken from ".../appels-d-offres/PW-HP-539-73419". A name
#    inside a web address is never a requirement.     --l1-urls off
#  * BUYER'S EQUIPMENT: "increase the heating ... in two Ford Econoline",
#    "The intended forklift is the Sellick STM55", "for loading and
#    transporting a Fat Truck(R)". The brand is what the buyer already owns
#    and the purchase must work with, not what it buys. One such mention in
#    the same sentence clears the brand (as one "or equivalent" does).
#    --l1-buyer-equipment off
DEFINED_TERMS = True
SKIP_URLS = True
BUYER_EQUIPMENT = True
_SMALL = ("for", "of", "and", "the", "de", "du", "des", "et", "la", "le")
RE_DEFINED = re.compile(
    r"((?:[A-Z][\w'\u2019-]*\s+(?:(?:for|of|and|the|de|du|des|et|la|le)\s+)?){2,8})"
    r"\(([A-Z][A-Z0-9]{1,7})\)")
RE_URL = re.compile(
    r"(?:https?://|www\.)\S+|\b\S+\.(?:gc\.ca|ca|com|org|gov)/\S*", re.I)
RE_BUYER_EQUIP = re.compile(
    r"\b(?:intended|existing|currently|owned|in-service|in\s+service"
    r"|compatib\w*|for\s+use\s+(?:with|on|in)|to\s+fit"
    r"|transport\w*|carry\w*|haul\w*|tow\w*|loading"
    r"|repair\w*|retrofit\w*|modif\w*|upgrad\w*|refurbish\w*|overhaul\w*"
    r"|increase\w*|improve\w*|install\w*\s+(?:in|on|into)"
    r"|mount\w*\s+(?:on|to|in)|existant\w*|réparation)\b", re.I)


def _defined_acronyms(docs):
    """Acronyms the tender spells out: 'Request for Supply Arrangement (RFSA)'."""
    out = set()
    for d in docs:
        for m in RE_DEFINED.finditer(d["text"]):
            words, acro = m.group(1).split(), m.group(2)
            full = "".join(w[0] for w in words).upper()
            big = "".join(w[0] for w in words if w.lower() not in _SMALL).upper()
            if full.endswith(acro) or big.endswith(acro):
                out.add(acro.lower())
    return out


def _sentence_before(text, pos, limit=160):
    """The part of the sentence that precedes a mention."""
    before = text[max(0, pos - limit):pos]
    cut = max(before.rfind(". "), before.rfind(";"), before.rfind("\n\n"),
              before.rfind(":"))
    return before[cut + 1:] if cut >= 0 else before


def _buyer_equipment(docs, hits):
    """Words showing the brand is the buyer's own equipment, if any mention
    reads that way; else None."""
    text_of = {d["document"]: d["text"] for d in docs}
    for h in hits:
        before = _sentence_before(text_of[h["document"]], h["pos"])
        if RE_BUYER_EQUIP.search(before):
            return _norm(before)[-120:]
    return None


def brands_without_equivalent(reference, text_dir=None, window=200,
                              window_before=80, offline=False, cache=None,
                              use_llm=False, verbose=False):
    """Detection level 1 for ONE tender.

    Returns {reference, documents, findings, ok, unresolved, blanket_clause}.
    A finding means: this requirement names a product and does not accept an
    equivalent. It is a hypothesis with evidence attached, never a verdict.
    """
    docs = tender_documents(reference, text_dir or TEXT_DIR)
    if not docs:
        return {"reference": reference, "error": "no extracted text found",
                "documents": 0, "findings": [], "ok": [], "unresolved": []}

    cache = _load_cache() if cache is None else cache
    blanket = False
    equiv_clause = False

    # Collect every occurrence of every candidate across the WHOLE tender, so
    # a requirement repeated in the RFP, the statement of requirement and two
    # amendments is one finding with four locations, not four findings.
    per_brand = {}
    lines_of = {d["document"]: d["text"].splitlines() for d in docs}
    for d in docs:
        text = d["text"]
        clause = bool(RE_DOC_CLAUSE.search(text))
        blanket = blanket or clause
        equiv_clause = equiv_clause or bool(
            EQUIV_CLAUSE and RE_EQUIV_CLAUSE.search(text))
        view = (amendment_view(text)
                if AMEND_RULE == "strict" and is_amendment(d) else text)
        for c in _candidates(view):
            key = c["name"].lower()
            b = per_brand.setdefault(key, {"name": c["name"], "hits": []})
            for occ in c["occurrences"]:
                after = text[occ["end"]:occ["end"] + window]
                before = text[max(0, occ["pos"] - window_before):occ["pos"]]
                occ = dict(occ)
                occ["document"] = d["document"]
                occ["category"] = d["category"]
                occ["blanket_clause"] = clause
                occ["qualified"] = bool(RE_OR_EQUIV.search(after)
                                        or RE_EQUIV_TO.search(before))
                b["hits"].append(occ)

    if DEFINED_TERMS:
        acros = _defined_acronyms(docs)
        for key in [k for k in per_brand if k.split()[0] in acros]:
            del per_brand[key]
    if SKIP_URLS:
        text_of = {d["document"]: d["text"] for d in docs}
        for key in list(per_brand):
            keep = []
            for h in per_brand[key]["hits"]:
                t = text_of[h["document"]]
                ls = t.rfind("\n", 0, h["pos"]) + 1
                le = t.find("\n", h["pos"])
                le = len(t) if le < 0 else le
                col = h["pos"] - ls
                if any(m.start() <= col < m.end()
                       for m in RE_URL.finditer(t[ls:le])):
                    continue
                keep.append(h)
            if keep:
                per_brand[key]["hits"] = keep
            else:
                del per_brand[key]

    # Wikidata first - free and already cached. Whatever it cannot settle goes
    # to the LLM in ONE batched call, which is what turns 127 unresolved
    # candidates from a dead end into an answer.
    wd = {k: verify_brand(b["name"], cache, offline)
          for k, b in per_brand.items()}
    triaged = {}
    if use_llm:
        undecided = [b["name"] for k, b in per_brand.items() if wd[k] is None
                     and not (TRIAGE_GUARD and _ordinary_word(cache, k))]
        if undecided:
            ev = {b["name"].lower(): max(b["hits"], key=_evidence_rank)["line"]
                  for b in per_brand.values()}
            if verbose:
                print(f"  LLM triage on {len(undecided)} unconfirmed names")
            triaged = llm_triage(undecided, ev, verbose=verbose)

    findings, ok, unresolved = [], [], []
    for key, b in per_brand.items():
        verdict = wd[key]
        if verdict is None and key in triaged:
            verdict = triaged[key]
        hits = b["hits"]
        best = max(hits, key=_evidence_rank)
        row = {
            "brand": b["name"],
            "document": best["document"], "category": best["category"],
            "page": best["page"], "line_no": best["line_no"],
            "evidence": best["line"],
            "occurrences": len(hits),
            "documents": sorted({h["document"] for h in hits}),
            "locations": [f"{h['document']} p.{h['page']} l.{h['line_no']}"
                          for h in sorted(hits, key=lambda h: (h["document"],
                                                               h["page"]))],
            "blanket_clause": best["blanket_clause"],
        }
        if verdict is None:
            row["reason"] = "could not confirm this is a brand"
            unresolved.append(row)
        elif verdict is False:
            continue                          # not a brand: not a finding
        elif _qualified_anywhere(docs, b["name"], window, window_before):
            # The tender allows an equivalent for this product somewhere.
            # Conservative on purpose: one allowance clears the brand.
            row["status"] = "OK"
            row["qualified_at"] = _qualified_anywhere(docs, b["name"], window,
                                                      window_before)
            ok.append(row)
        elif blanket:
            row["status"] = "OK_BLANKET_CLAUSE"
            ok.append(row)
        elif equiv_clause:
            row["status"] = "OK_EQUIV_PRODUCTS_CLAUSE"
            ok.append(row)
        elif BUYER_EQUIPMENT and _buyer_equipment(docs, hits):
            row["status"] = "OK_BUYER_EQUIPMENT"
            row["buyer_equipment"] = _buyer_equipment(docs, hits)
            ok.append(row)
        elif MAKE_LIST and all(
                _in_make_list(lines_of[h["document"]], h["line_no"])
                for h in hits):
            row["status"] = "OK_MAKE_LIST"
            ok.append(row)
        else:
            # The rules say this looks like a lock-in. Before reporting it, let
            # the model read every mention in context - this is the step that
            # catches "Leica products (or equivalent)" and incidental mentions
            # in titles, addresses and manual lists.
            if use_llm:
                j = llm_judge_lock(b["name"], _excerpts(docs, b["name"]),
                                   allowances=_allowances(docs),
                                   verbose=verbose)
                if j is not None:
                    row["llm_reason"] = str(j.get("reason", ""))[:300]
                    row["llm_quote"] = str(j.get("quote", ""))[:300]
                    row["llm_confidence"] = str(j.get("confidence", ""))[:20]
                    if not j.get("locked"):
                        row["status"] = "OK_LLM_CLEARED"
                        ok.append(row)
                        continue
            row["status"] = "BRAND_WITHOUT_EQUIVALENT"
            findings.append(row)

    findings.sort(key=lambda r: -r["occurrences"])
    return {"reference": reference, "documents": len(docs),
            "blanket_clause": blanket, "equiv_products_clause": equiv_clause,
            "findings": findings, "ok": ok,
            "unresolved": unresolved}


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tender", required=True, help="reference number")
    ap.add_argument("--text-dir", default=TEXT_DIR)
    ap.add_argument("--offline", action="store_true",
                    help="use the cache only, no Wikidata calls")
    ap.add_argument("--llm", action="store_true",
                    help="use the LLM to triage unconfirmed names and to "
                         "judge each apparent lock-in in context")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()

    r = brands_without_equivalent(args.tender, args.text_dir,
                                  offline=args.offline, use_llm=args.llm,
                                  verbose=args.verbose)
    if args.json:
        print(json.dumps(r, indent=2))
        return
    if r.get("error"):
        sys.exit(f"{args.tender}: {r['error']}")

    print(f"\nTender {r['reference']}  ({r['documents']} documents)")
    if r["blanket_clause"]:
        print("  a blanket 'brand names are for reference only' clause is "
              "present")
    print(f"\n  FINDINGS ... {len(r['findings'])}")
    for f in r["findings"]:
        print(f"\n    {f['brand']}   {f['document']} p.{f['page']} "
              f"line {f['line_no']}")
        print(f"      {f['evidence'][:160]}")
        if f["occurrences"] > 1:
            print(f"      also at: {', '.join(f['locations'][:4])}"
                  + (" ..." if len(f["locations"]) > 4 else ""))
        if f.get("llm_reason"):
            print(f"      judged: {f['llm_reason']} "
                  f"[{f.get('llm_confidence', '')}]")
            if f.get("llm_quote"):
                print(f"      quote:  \"{f['llm_quote'][:140]}\"")
    print(f"\n  compliant brand mentions ... {len(r['ok'])}")
    if r["unresolved"]:
        print(f"  unconfirmed candidates ..... {len(r['unresolved'])}"
              "   (not reported as findings)")
    if not r["findings"]:
        print("\n  Nothing to report on level 1 for this tender.")


if __name__ == "__main__":
    main()
