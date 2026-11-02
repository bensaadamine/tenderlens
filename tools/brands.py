"""
BRANDS - detection level 1 for ONE tender: brand names the specification
requires without accepting an equivalent.

Canadian trade agreements (CFTA, CETA) require functional requirements: a
specification may name a product only if it also accepts an equivalent.
brands_without_equivalent(ref) sorts every brand-like name into:

    findings    named and required, no equivalent accepted - with document,
                page, line and sentence, so a person can check it in seconds
    ok          cleared by the text: "or equivalent", the standard Equivalent
                Products clause, the buyer's own equipment, a list of makes,
                an operating system or browser
    unresolved  Wikidata cannot say whether it is a company - the M2 agent's
                job (agents/m2_agent.py)

A candidate is a capitalised name followed by a model code ("Bendix AD9"),
found in the requirements section (documents.py), outside bidders'
questions. Whether it is a company: wikidata.py. The clearances read the
WHOLE tender - an "or equivalent" written over a table covers every row.

    python tools/brands.py --tender PW-23-01022340 [--offline]
"""

import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import repo  # noqa: E402  (agents/ tools/ prepare/ tests/ importable; works from the repo root)

import argparse
import json
import re

from documents import (amendment_view, is_amendment, question_mask,
                       requirement_view, tender_documents)
from wikidata import load_cache, verify_brand

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

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
# LLM judge read it.
# Measured 2026-10-19: "A Bendix Model AD9 ... must be provided" became the
# candidate "Bendix Model", which no lookup confirms as a brand - losing a
# real lock-in. A trailing generic word
# (model, series, type, part no.) is not part of a maker's name.
RE_NAME_TAIL = re.compile(
    r"(?:\s+(?:models?|series|type|part|p/?n|no\.?|modèle|série))+$", re.I)

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

# Measured 2026-10-17 (real 2510, online): three false findings, all fixed
# here.
#  * "FORD F550" in a bidder's "Question 4" of an amendment: the strict view
#    blanked the question marker but kept the bidder's text because it said
#    "replace seats" (a change word), so the question was never recognised.
#    Now question markers stay visible in the view.
#  * "ANSI/SAIA A92.2": a name written right after a standards body and a
#    slash is part of a standard's name.
#  * "Ford F150 /250 / 350" in a list of vehicles the brackets must fit
#    (Chevrolet, Ford, Dodge ...): three or more makes listed together is a
#    fitment or choice list, not a lock-in.
MAKE_LIST_MIN = 3       # distinct makes on list-shaped lines
MAKE_LIST_WINDOW = 6    # lines either side
RE_STD_PREFIX = re.compile(r"\b[A-Z]{2,6}\s*/\s*$")
RE_LIST_LINE = re.compile(
    r"^\s*(?:\d+[.)]\s*|[-*•]\s*)?([A-Z][A-Za-z\-]{2,})\s+\S")
RE_UNIT_AFTER_NUM = re.compile(
    r"\d\s*(?:kg|lbs?|mm|cm|in\b|m\b|l\b|km|kw|hp|v\b|a\b|%|°)", re.I)


# Decided 2026-11-01 (user, measured): candidate brand names are looked for
# only in the REQUIREMENTS SECTION of a document - the section scope_mask
# finds, the one level 2 already uses. On the 76 hand-labelled cases this lost
# 0 of 12 real lock-ins and removed 10 of 46 junk names (addresses,
# regulations, cross-references). A document with no recognisable section is
# read whole, as before. Judging still reads the whole tender ("or
# equivalent", the Equivalent Products clause, the agent's tools).
# --l1-scope all restores looking for names everywhere.
CANDIDATE_SCOPE = "section"


def add_l1_args(ap):
    """The level-1 option shared by every script that runs the system."""
    ap.add_argument("--l1-scope", choices=["section", "all"],
                    default="section",
                    help="where brand names are looked for: the requirements "
                         "section (default; whole document if none found) or "
                         "everywhere (old rule)")


def apply_l1_args(a):
    global CANDIDATE_SCOPE
    CANDIDATE_SCOPE = getattr(a, "l1_scope", "section")


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


def _candidates(text):
    """Brand candidates in one document, with where they are."""
    found = {}
    lines = text.splitlines()
    in_q = question_mask(lines)
    run = 0
    for i, line in enumerate(lines):
        start, run = run, run + len(line) + 1
        if in_q[i]:
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
            # Those are kept when the support is strong: the model code mixes
            # letters and digits, or the rest of the line is a short cell.
            after_colon = bool(RE_SENTENCE_START.search(line[:m.start()]))
            at_start = bool(re.fullmatch(r"\s*(?:\d+[.)]\s*|[-*•]\s*)?",
                                         line[:m.start()]))
            if after_colon or at_start:
                if line[:m.start()].rstrip().endswith((".", "!", "?", ";")):
                    continue
                if not _strong_support(tail, line[m.end():]):
                    continue
            # "Square/Carre:", "Colour:" - a field label, not a product.
            if tail[:1] in (":", "/", "="):
                continue
            # "ANSI/SAIA A92.2", "CSA/ISO 9001": part of a standard's name.
            if RE_STD_PREFIX.search(line[:m.start()]):
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


def _brand_pattern(brand):
    """The brand as a whole word. A hyphen after it still counts as a word
    edge: "Knapheide KSS-96 or equivalent" must be found from "Knapheide
    KSS" (measured 2026-10-17)."""
    return re.compile(r"(?<![\w-])" + re.escape(brand) + r"(?!\w)", re.I)


# Measured 2026-10-17: "All wheel ends must be provided with stainless steel
# Bearing Buddy® wheel / bearing protectors or equivalent." - pdfminer printed
# the END of the sentence ("bearing protectors or equivalent.") a few lines
# ABOVE the brand. When the brand's line stops mid-sentence and a nearby line
# just above starts in lowercase (a continuation fragment) and ends in "or
# equivalent", that is this sentence's end.
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
            if _tail_moved_up(text, m):
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
# findings (2 genuine: Bendix AD9 x2). Three new rules.
#  * DEFINED TERMS: "Request for Supply Arrangement (RFSA)", "Arctic Mobility
#    Amphibious Vehicles (AMAV)" - an acronym the tender itself spells out is
#    its own shorthand, never a brand.
#  * URLS: "PW-HP" taken from ".../appels-d-offres/PW-HP-539-73419". A name
#    inside a web address is never a requirement.
#  * BUYER'S EQUIPMENT: "increase the heating ... in two Ford Econoline",
#    "The intended forklift is the Sellick STM55", "for loading and
#    transporting a Fat Truck(R)". The brand is what the buyer already owns
#    and the purchase must work with, not what it buys. One such mention in
#    the same sentence clears the brand (as one "or equivalent" does).
# A name not confirmed as a brand still goes through the text clearances
# ("or equivalent", the clauses, buyer's equipment, make list) - before
# 2026-10-22 it went straight to "unresolved", so the M2 agent was asked
# about "a trailer for transporting a Fat Truck" and called it a lock-in.
# Cleared that way it is "ok" with brand_unconfirmed=True.
# An operating system or web browser names the computer the purchase must
# run on - the buyer's environment, not a product it locks in ("software
# must run on Windows 10", "Internet Explorer 11"). Measured 2026-11-01:
# "Windows" made ~17 of the 35 4110 cases with the M2 agent, all false.
PLATFORM_NAMES = ("windows", "microsoft windows", "macos", "mac os", "os x",
                  "linux", "ubuntu", "red hat", "rhel", "unix", "ios",
                  "android", "chrome os", "chromeos", "internet explorer",
                  "microsoft edge")


def is_platform(name):
    n = _norm(name).lower()
    return any(n == p or n.startswith(p + " ") for p in PLATFORM_NAMES)
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


def _text_clearance(docs, name, hits, lines_of, window, window_before,
                    blanket, equiv_clause):
    """The first thing in the text that clears this name, as row fields
    ({"status": ...}), else None. Same order as before 2026-10-22."""
    qualified = _qualified_anywhere(docs, name, window, window_before)
    if qualified:
        # The tender allows an equivalent for this product somewhere.
        # Conservative on purpose: one allowance clears the brand.
        return {"status": "OK", "qualified_at": qualified}
    if blanket:
        return {"status": "OK_BLANKET_CLAUSE"}
    if equiv_clause:
        return {"status": "OK_EQUIV_PRODUCTS_CLAUSE"}
    equipment = _buyer_equipment(docs, hits)
    if equipment:
        return {"status": "OK_BUYER_EQUIPMENT", "buyer_equipment": equipment}
    if all(_in_make_list(lines_of[h["document"]], h["line_no"])
                         for h in hits):
        return {"status": "OK_MAKE_LIST"}
    if is_platform(name):
        return {"status": "OK_PLATFORM"}
    return None


def brands_without_equivalent(reference, text_dir=None, window=200,
                              window_before=80, offline=False, cache=None):
    """Detection level 1 for ONE tender.
    Returns {reference, documents, findings, ok, unresolved, blanket_clause}.
    A finding means: this requirement names a product and does not accept an
    equivalent. It is a hypothesis with evidence attached, never a verdict.
    """
    docs = tender_documents(reference, text_dir)
    if not docs:
        return {"reference": reference, "error": "no extracted text found",
                "documents": 0, "findings": [], "ok": [], "unresolved": []}
    cache = load_cache() if cache is None else cache
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
            RE_EQUIV_CLAUSE.search(text))
        if is_amendment(d):
            view = amendment_view(text)
        elif CANDIDATE_SCOPE == "section":
            view = requirement_view(text)
        else:
            view = text
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
    acros = _defined_acronyms(docs)
    for key in [k for k in per_brand if k.split()[0] in acros]:
        del per_brand[key]
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
    # Wikidata: free and cached. What it cannot settle is "unresolved" -
    # the M2 agent's job.
    wd = {k: verify_brand(b["name"], cache, offline)
          for k, b in per_brand.items()}
    findings, ok, unresolved = [], [], []
    for key, b in per_brand.items():
        verdict = wd[key]
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
        if verdict is False:
            continue                          # not a brand: not a finding
        clear = _text_clearance(docs, b["name"], hits, lines_of, window,
                                window_before, blanket, equiv_clause)
        if verdict is None:
            if clear:
                row.update(clear, brand_unconfirmed=True)
                ok.append(row)
            else:
                row["reason"] = "could not confirm this is a brand"
                unresolved.append(row)
        elif clear:
            row.update(clear)
            ok.append(row)
        else:
            row["status"] = "BRAND_WITHOUT_EQUIVALENT"
            findings.append(row)
    findings.sort(key=lambda r: -r["occurrences"])
    return {"reference": reference, "documents": len(docs),
            "blanket_clause": blanket, "equiv_products_clause": equiv_clause,
            "findings": findings, "ok": ok,
            "unresolved": unresolved}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tender", required=True, help="reference number")
    ap.add_argument("--text-dir", default=None)
    ap.add_argument("--offline", action="store_true",
                    help="use the cache only, no Wikidata calls")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()
    r = brands_without_equivalent(args.tender, args.text_dir,
                                  offline=args.offline)
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
    print(f"\n  compliant brand mentions ... {len(r['ok'])}")
    if r["unresolved"]:
        print(f"  unconfirmed candidates ..... {len(r['unresolved'])}"
              "   (not reported as findings)")
    if not r["findings"]:
        print("\n  Nothing to report on level 1 for this tender.")


if __name__ == "__main__":
    main()
