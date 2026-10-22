"""
CANADA - STEP 4: the vocabulary census.

This detects NOTHING. It counts, so that the level-2 design rests on a
measurement instead of an assumption.

Level 2 compares a requirement against THE SAME requirement in other tenders
of the same product family. That only works if enough tenders state the same
attribute. Nobody has measured whether they do.

So this script pulls every (label, comparator, value, unit) candidate out of
the cached text and answers four questions:

  1. How many DISTINCT requirement labels are there per product family?
  2. How many of them reach n >= 8 distinct tenders - the floor below which
     an outlier test cannot tell "unusual" from "small sample"?
  3. What is the date spread? A 2016 spec and a 2026 spec are different
     populations for a performance attribute.
  4. The WITHIN-DOCUMENT baseline: per tender, how many requirements are
     thresholds (>=, <=) versus exact equalities? That test needs no peers
     at all, and the peer model has to beat it to justify its cost.

If most labels land below n = 8, the peer comparison is a bonus that fires
occasionally and the within-document test is the primary detector. That is
the decision this script exists to inform.

Labels are NOT canonicalized here - that needs the LLM and costs tokens.
This is the free pass that says whether paying is worth it. Read
reports/census_<grp>_labels.csv afterwards: the raw vocabulary is the input
to the attribute map, and the near-duplicates in it ("chamber volume" /
"internal capacity") are exactly what the LLM has to merge.

Reads the text cached by 03 - nothing is re-extracted, nothing downloaded.

Run:
    python prepare/04_census.py                      # group 4110
    python prepare/04_census.py --group 2510 --min-peers 8

Writes:
    reports/census_<grp>.csv          every extracted tuple, one per row
    reports/census_<grp>_labels.csv   per label: tenders, comparator mix
    reports/census_<grp>.txt          the summary printed below
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
import csv
import io
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict

csv.field_size_limit(10_000_000)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# --------------------------------------------------------------- the units
# Each unit maps to a PHYSICAL DIMENSION. The dimension is half the grouping
# key: "length" tells us a wheelbase and a cargo-box depth are at least
# commensurable, where "volume" and "length" never are. It comes free from the
# unit - no model, no ambiguity, reproducible.
#
# Deliberately NOT here: bare "t" (tonne/ton/short ton - three values), bare
# "C" (matches prose), '"' (inches or a quote mark), "gal" (US vs Imperial,
# 20% apart, and both conventions appear in bilingual procurement). Decision
# 15: convert or drop, never guess. A dropped observation costs one data
# point; a wrong conversion manufactures a false accusation.
DIMENSION = {}
for _dim, _units in {
    # Compound units FIRST - see the UNIT regex note below.
    "speed":       "km/h kph mph m/s",
    "length":      "mm cm dm m km nm um µm in inch inches ft",
    "mass":        "kg mg g lb lbs oz",
    "volume":      "ml mL l L cc m3 m³ cm3 cm³ mm3 ft3 ft³",
    "voltage":     "kV mV V",
    "current":     "mA A",
    "power":       "kW MW W VA kVA",
    "frequency":   "GHz MHz kHz Hz",
    "rotation":    "rpm",
    "pressure":    "psi kPa MPa bar mbar",
    "temperature": "°C °F degC",
    "ratio":       "%",
    "sound":       "dB",
    "light":       "lm lx cd",
    "data":        "GB TB MB",
    "data_rate":   "Mbps Gbps",
    "resolution":  "dpi ppi px",
    "torque":      "Nm",
    "heat":        "BTU",
    "flow":        "CFM lpm gpm",
}.items():
    for _u in _units.split():
        DIMENSION[_u] = _dim

# Units whose reading is not certain from the token alone. "70 in" is inches,
# but "70 in total" is a preposition; bare "m" and "l" and "g" collide with
# abbreviations. Counted separately so the summary shows how much of the
# corpus depends on resolving them, rather than silently trusting them.
SOFT_UNITS = {"in", "inch", "inches", "m", "l", "g", "A", "cc"}

# ORDER MATTERS. Python's alternation returns the first branch that matches
# at a position, so every compound unit must come before the prefix it starts
# with: km/h before km, m3 and m/s before m, ft3 before ft. Getting this wrong
# filed "110 km/h" as 110 km of length and "0.566 m3" as 0.566 m.
UNIT = (r"(?:km/h|kph|mph|m/s|m3|m³|cm3|cm³|mm3|ft3|ft³|"
        r"mm|cm|dm|km|m|nm|um|µm|in|inch|inches|ft|"
        r"kg|mg|g|lb|lbs|oz|"
        r"ml|mL|l|L|cc|"
        r"kV|mV|V|mA|A|kW|MW|W|VA|kVA|"
        r"GHz|MHz|kHz|Hz|rpm|"
        r"psi|kPa|MPa|bar|mbar|"
        r"°C|°F|degC|"
        r"%|dB|lm|lx|cd|"
        r"GB|TB|MB|Mbps|Gbps|dpi|ppi|px|"
        r"Nm|BTU|CFM|lpm|gpm)")

# The number. TWO shapes, and the first one must come first:
#
#   13,600   57 982   26 300        grouped thousands
#   3.96     0,5      110           plain, decimal point OR decimal comma
#
# FOUND BY MEASUREMENT, 2026-10-14, and it had corrupted every census run
# to date: the old pattern was `\d{1,6}([.,]\d+)?` and the value was stored
# with `value.replace(",", ".")`, so `13,600 kg` became 13.6 kg. A truck's
# GVWR entered the knowledge base a thousand times too small. It stayed
# invisible until unit conversion put a base unit beside the number and the
# GVWR median read 10.9 kg.
#
# The space-separated form was worse than wrong, it was silent: `26 300 kg`
# matched only `300 kg`, so the observation was off by two orders of
# magnitude with nothing odd-looking in the row. Canadian procurement is
# bilingual and French convention groups thousands with a space, so this is
# not a rare shape.
NUM = (r"\d{1,3}(?:[,   ]\d{3})+(?:\.\d+)?"
       r"|\d{1,6}(?:[.,]\d+)?")

RE_VALUE = re.compile(
    r"(?<![\w.\-/])(" + NUM + r")\s?(" + UNIT + r")\b")

RE_GROUPED = re.compile(r"^\d{1,3}(?:[,   ]\d{3})+(?:\.\d+)?$")
RE_DEC_COMMA = re.compile(r"^\d+,\d{1,2}$")


def parse_number(raw):
    """Text -> canonical decimal string, or '' if it will not parse.

    The rule for a comma is the digit count after it: three digits is a
    thousands separator (`13,600`), one or two is a decimal comma
    (`3,96 m`, French). `1,500` is genuinely ambiguous and is read as
    1500 - the convention that is far commoner in this corpus.
    """
    s = raw.strip()
    if RE_GROUPED.match(s):
        return re.sub(r"[,   ]", "", s)
    if RE_DEC_COMMA.match(s):
        return s.replace(",", ".")
    s = s.replace(",", ".")
    try:
        float(s)
    except ValueError:
        return ""
    return s

# ---------------------------------------------------------- the comparator
# THIS is the signal. Peers write ">= 70 cm"; the rigged tender writes
# "= 78 cm". Direction matters: a band (both bounds) is a different claim
# from a single threshold, and an equality is a different claim again.
RE_GE = re.compile(r"""
    ≥|>=
  | \bmin\.?\b|\bminimum\b|\bminimale?\b
  | \bat\ least\b|\bno(?:t)?\ less\ than\b|\bgreater\ than\b
  | \bor\ (?:more|greater|higher|better)\b
  | \bau\ moins\b
""", re.I | re.X)

RE_LE = re.compile(r"""
    ≤|<=|=<
  | \bmax\.?\b|\bmaximum\b|\bmaximale?\b
  | \bno(?:t)?\ more\ than\b|\bless\ than\b|\bnot\ (?:to\ )?exceed\b
  | \bup\ to\b|\bor\ (?:less|lower)\b
  | \bau\ plus\b|\bne\ doit\ pas\ dépasser\b
""", re.I | re.X)

RE_RANGE = re.compile(r"±|\+/-|\bbetween\b|\bentre\b|\btolerance\b", re.I)

# "38 mm to 100 mm", "0 degC - 40 degC" - a two-sided band.
RE_INTERVAL = re.compile(
    r"\d{1,6}(?:[.,]\d+)?\s?" + UNIT + r"?\s*(?:to|through|–|—|à)\s*"
    r"\d{1,6}(?:[.,]\d+)?\s?" + UNIT + r"\b", re.I)

# An exact value where a threshold belongs. The negative lookbehind keeps
# ">=" and "<=" out - without it every threshold reads as an equality.
RE_EQ = re.compile(
    r"(?<![<>=!≥≤])=\s*\d|\bexactly\s+\d|\bprecisely\s+\d", re.I)

# Administrative numbers that look like requirements: bid file size limits,
# page counts, payment terms, insurance, delivery deadlines. Lifted from 03,
# where dropping these was worth several points of precision.
RE_BOILERPLATE = re.compile(
    r"(?:file|e-?mail|attachment|message|upload|submission)s?\b.{0,40}"
    r"\b(?:KB|MB|GB)\b"
    r"|\b(?:KB|MB|GB)\b.{0,40}\b(?:limit|maximum|attachment)"
    r"|\bfont\b|\bmargins?\b|\bdouble.?spaced\b|\bpage limit\b"
    r"|\bno more than \d+\s*pages?\b|\b\d+\s*pages? (?:maximum|or less)\b"
    r"|\binsurance\b|\bliabilit|\bbid security\b|\bperformance bond\b"
    r"|\bGST\b|\bHST\b|\bQST\b|\bwithholding\b"
    r"|\b(?:calendar|business|working) days?\b|\bwithin \d+ days?\b"
    r"|\b\d+\s?% of the (?:contract|bid|total|value)"
    # Standard-clause and solicitation references. In 4110 these produced the
    # four most common "attributes" in the whole category: "General Conditions
    # 2010A" read as 2010 amperes, and "W8703-270084A" likewise. Pure noise,
    # and it outranked every real requirement.
    r"|general conditions|supplemental conditions|standard instructions"
    r"|conditions g\u00e9n\u00e9rales|instructions uniformis\u00e9es"
    r"|\b20\d{2}[A-Z]?\s*\(\d{4}-\d{2}-\d{2}\)"
    r"|\b[A-Z]\d{4,}-\d{5,}"
    r"|net.?zero|greenhouse gas|\bghg\b"
    r"|zip files|file attachments|inch format"
    # Bid-formatting rules. "use 8.5 x 11 inch (216 mm x 279 mm) paper" was
    # 57 rows and cleared the peer floor on its own.
    r"|8\.5\s?[xX]\s?11|letter.?size|\bpaper\b.{0,25}(?:mm|inch)"
    # Packaging and shipping. Real requirements, but about the CRATE, not the
    # product - pallet and skid dimensions pollute length and mass badly.
    r"|\bpallets?\b|\bskids?\b|\bcrates?\b|\bdunnage\b|\bISPM\b"
    r"|\bshipments?\b|\bpacking list\b|\bpackaging\b|\bcartons?\b"
    r"|\bstrap(?:ped|ping)?\b|\bshrink.?wrap\b|\bpalletiz"
    # Anthropometric annex. A reused standard table of body measurements for
    # cab sizing - dozens of "length" labels that describe a person.
    r"|\bpopliteal\b|\bbuttock\b|\bsitting height\b|\belbow rest\b"
    r"|\bchest (?:depth|circumference)\b|\bhip breadth\b|\bstature\b"
    r"|\banthropometric\b|\bbust circumference\b|\bwaist circumference\b"
    # Returns / repair-and-overhaul admin.
    r"|repair and overhaul|\bRMA\b", re.I)

# Part-number shapes, not measurements. Decision 9: a category specified by
# part number has no threshold to compare, and "30x9.5R16" must never be
# parsed as the number 30.
RE_PARTNUM = re.compile(
    r"\b\d{1,3}\s?[xX×]\s?\d{1,3}(?:\.\d)?\s?[A-Z]\s?\d{2}\b"
    r"|\b[A-Z]{1,3}\d{3,}[A-Z\d-]*\b"
    r"|\b\d{2,}-\d{3,}\b")

RE_PAGENUM = re.compile(r"^\s*(?:page\s*)?\d+\s*(?:of|/|de)\s*\d+\s*$", re.I)

# Words that carry no attribute identity. Stripped before counting a label so
# "minimum chamber depth" and "chamber depth shall be" are one string.
# Comparator words go too - the comparator is recorded in its own column, and
# leaving it in the label would split one attribute into a min- and a
# max-flavoured copy.
STOP = set("""
a an the of for with and or to in on at by from as is are be being been
shall must should will may can each per item no not nor than then that this
these those it its approx approximately about around nominal nominally rated
capable minimum maximum min max least more less greater lower higher better
exceed exceeding exceeds up under over between within least total overall
required require requires requirement requirements spec specification
value values range ranges size sizes unit units qty quantity
le la les des une un pour avec doit être au moins plus moins de du
""".split())

# A label that is only these is not an attribute - it is table furniture.
JUNK_LABELS = {"", "yes", "no", "n a", "na", "tbd", "see annex", "as per",
               "item", "description", "qty", "quantity", "unit", "units",
               "comply", "compliance", "y n", "oui non", "s o"}

WINDOW = 2          # non-blank lines either side, matching 03
MIN_LABEL_ALPHA = 3  # a label needs this many letters to be a label

# Lines either side kept as readable CONTEXT. Wider than WINDOW, which only
# hunts a comparator. Context is not used to decide anything here - it is
# carried so that the things downstream of this script can decide:
#
#   - the attribute map (05) names an attribute from a bare fragment today
#     and answers `unknown` 55% of the time; a table row plus its heading is
#     usually enough to name it (open item 9)
#   - ambiguous units resolve on sight: "70 in total" against "70 in width"
#     is obvious in context and impossible without (open item 10, 615 rows)
#   - decision 3 owes the auditor the location AND the wording of a claim,
#     and a 200-character fragment is not a quotation
#   - decision 11 gives the LLM the judgement, and nothing can be judged
#     from a fragment
CONTEXT = 3
CONTEXT_CHARS = 400

# ------------------------------------------- the requirement section - dec 21
# Canadian federal solicitations follow a fixed skeleton:
#
#   PART 1 GENERAL INFORMATION        PART 4 EVALUATION PROCEDURES
#   PART 2 BIDDER INSTRUCTIONS        PART 5 CERTIFICATIONS
#   PART 3 BID PREPARATION            PART 6 RESULTING CONTRACT CLAUSES
#   ANNEX A - STATEMENT OF REQUIREMENT   <- the spec
#   ANNEX B - BASIS OF PAYMENT           <- not the spec
#
# Measured on 2510: 96% of tenders carry a recognizable requirement heading,
# and `PURCHASE DESCRIPTION` (the DND spelling) alone appears 431 times.
# Parts 1, 2, 3, 5 and 6 are pure administration - and are exactly the text
# the boilerplate regex above has been fighting one clause at a time. Cutting
# them out attacks the noise at its source instead of filtering it afterwards.
#
# THE AUDIT RULE APPLIES HERE WITH FULL FORCE: boilerplate repeats across
# tenders more than real requirements do, so if coverage goes UP after
# scoping, the scoping is wrong.

# Opens a requirement span.
RE_SPEC_HEAD = re.compile(r"""
    \bstatement\ of\ (?:requirements?|work)\b
  | \bpurchase\ description\b
  | \btechnical\ (?:specifications?|requirements?|description)\b
  | \bmandatory\ (?:technical\ )?(?:requirements?|criteria)\b
  | \bdescription\ of\ (?:the\ )?(?:requirements?|work|goods)\b
  | \bscope\ of\ (?:work|the\ requirement)\b
  | \bspecifications?\b
  | \b(?:technical\ )?evaluation\ matrix\b
  | \bdata\ table\b
  | \b(?:énoncé\ des\ travaux|besoin\ technique|devis)\b
    # A heading that is just "<adjective> REQUIREMENTS": CORE REQUIREMENTS,
    # GENERAL REQUIREMENTS, VEHICLE REQUIREMENTS. Safe because the
    # administrative ones - SECURITY REQUIREMENTS, INSURANCE REQUIREMENTS -
    # are matched by RE_ADMIN_HEAD, which is tested first.
  | ^(?:[a-zà-ÿ]+\ ){0,2}requirements?$
""", re.I | re.X)

# Closes it. Checked BEFORE the opener, because "SECURITY REQUIREMENTS" and
# "INSURANCE REQUIREMENTS" are administration that happen to say the word.
RE_ADMIN_HEAD = re.compile(r"""
    \bsecurity\ requirements?\b | \binsurance\b
  | \bbasis\ of\ (?:payment|selection)\b
  | \bcertifications?\b | \bresulting\ contract\ clauses\b
  | \bbidder\ instructions\b | \bbid\ preparation\b
  | \bgeneral\ information\b | \bevaluation\ procedures\b
  | \btable\ of\ contents\b | \blist\ of\ annexes\b
  | \bterms\ of\ payment\b | \binvoicing\b | \bdeclaration\b
  | \breciprocal\ procurement\b | \bapplicable\ documents\b
  | \bgeneral\ conditions\b | \bstandard\ instructions\b
  | \bintegrity\ provisions\b | \bsubmission\ of\ bids\b
""", re.I | re.X)

# A structural marker. ANNEX A opens only if it also names the spec; PART n
# and ANNEX B always close, because the spec is never under them.
RE_STRUCT_HEAD = re.compile(
    r"^(?:part|partie|annex|annexe|appendix|appendice|attachment|section)"
    r"\s*[-–—:]?\s*[A-Za-z0-9]{1,3}\b", re.I)

# Running headers, not headings. `DLP 8-2-7` appears 1,431 times in 2510.
RE_RUNNING_HEAD = re.compile(r"^(?:A/)?(?:DLP|DAAT|RDIMS|TBIPS|SACC|CCC)\b", re.I)

# A table-of-contents entry: dot leaders or a trailing page number. These
# name the spec section without being it, and would open a span inside the
# contents page.
RE_TOC_ENTRY = re.compile(r"(?:\.{3,}|…)\s*\d{1,4}$|\s{2,}\d{1,4}$")


# A numbered clause heading: "10.  Evaluation Procedures." - DND style, title
# case, but unmistakably a heading because of the leading clause number.
RE_NUM_HEAD = re.compile(r"^\d{1,2}(?:\.\d{1,2})*\.?\s{2,}[A-Z]")


def is_heading(line):
    """Could this line be a section heading? Shape only, no meaning yet.

    MEASURED THE HARD WAY: the first version asked only for a short line
    with a few letters, and pdfminer emits paragraph text one fragment per
    line, so "Annex A," and "Insurance." and a bare "requirements" inside a
    sentence all read as headings. The state machine below was then driven
    by cross-references in prose and flipped the span on and off at random:
    it cut `jack load` and `lunette ring inner diameter` - both real
    requirements, both plainly inside a spec - while keeping 11 of one
    catalogue RFSO's 557 observations.

    So a heading must LOOK like one: all upper case, or a numbered clause
    title. Everything else is prose, whatever words it happens to contain.
    A document whose headings are neither falls back to whole-document,
    which is the safe direction.
    """
    s = line.strip()
    if not (5 <= len(s) <= 90):
        return False
    if RE_RUNNING_HEAD.match(s) or RE_TOC_ENTRY.search(s):
        return False
    letters = [ch for ch in s if ch.isalpha()]
    if len(letters) < 5:
        return False
    if sum(ch.isupper() for ch in letters) / len(letters) >= 0.85:
        return True
    return bool(RE_NUM_HEAD.match(s))


def heading_title(body, i):
    """The full heading at line i, re-joined if pdfminer split it.

    "ANNEX C" and "TECHNICAL EVALUATION MATRIX" are one heading laid out on
    two lines, and the second line is the one that says what the section is.
    Classifying the bare marker alone closed the span immediately before the
    Data Table - the densest, cleanest numeric requirements in the whole
    corpus, the `GVWR | 13,600 kg (30,000 lbs)` rows - and cost three
    tenders two attributes. Same lesson as the ±2 comparator window: the
    line is not the unit of meaning, pdfminer just ends it there.
    """
    s = body[i].strip()
    if RE_STRUCT_HEAD.match(s) and alpha_count(RE_STRUCT_HEAD.sub("", s, 1)) < 3:
        for k in (i + 1, i + 2):
            if k < len(body) and is_heading(body[k]):
                return s + " - " + body[k].strip()
    return s


# A requirement sitting under one of these headings is about the CRATE, the
# shipment or the paperwork, not the product - whatever its wording.
#
# Earned 2026-10-15. Giving 05 the context (decision 24) rescued 178 labels
# from `unknown`, and one of them was `individual item length`, fed by "The
# Contractor must secure individual items exceeding 1.22 m (48 in.) in
# length" under `5.6. Palletization`. Reused across 17 tenders, so it
# cleared the peer floor easily and inflated coverage from 58% to 66%.
#
# Rule 1 again, and a new wrinkle in it: context makes the model BETTER at
# naming things, including things that should not be named. A clause that
# reads like a requirement is not stopped by a better namer - it has to be
# stopped by where it sits. The section is a fact about the document and
# costs nothing, so it is the right place to decide this, not the prompt.
RE_ADMIN_SECTION = re.compile(
    r"palletiz|packag|\bpacking\b|transportation cost|carrier\b"
    r"|\bshipping\b|\bshipment|\bmarking\b|preservation"
    r"|basis of payment|invoic|\bwarrant(?:y|ies)\b", re.I)


def section_index(body):
    """For each line, the nearest heading at or above it.

    The section a requirement sits under is the cheapest context there is -
    it is already computed for scoping, and it tells a reader (or the LLM)
    whether a number came from PURCHASE DESCRIPTION or from a packaging
    annex, which is most of what they need to judge it.
    """
    heads, cur = [""] * len(body), ""
    for i, line in enumerate(body):
        if is_heading(line):
            cur = heading_title(body, i)[:90]
        heads[i] = cur
    return heads


def scope_mask(body):
    """Per-line flag: is this line inside a requirement section?

    A state machine over heading lines. Any spec heading opens the span, any
    administrative or structural heading closes it. Returns (mask, mode);
    mode is 'whole' when no requirement heading was found at all - the 4%
    that have no skeleton, where cutting nothing is safer than guessing.
    """
    mask = [False] * len(body)
    inside = False
    found = False
    for i, line in enumerate(body):
        if is_heading(line):
            s = heading_title(body, i)
            if RE_ADMIN_HEAD.search(s):
                inside = False
            elif RE_SPEC_HEAD.search(s):
                inside = True
                found = True
            elif RE_STRUCT_HEAD.match(s):
                # "ANNEX A - STATEMENT OF REQUIREMENT" was caught above; a
                # bare "PART 5" or "ANNEX B - BASIS OF PAYMENT" was not.
                inside = False
        mask[i] = inside
    if not found:
        return [True] * len(body), "whole"
    return mask, "section"


def norm_label(raw):
    """Lowercase, drop numbers/units/punctuation/stopwords, collapse space.

    Crude on purpose. This is a COUNTING key, not the canonical name - the
    LLM produces that later. The near-duplicates this leaves behind
    ("chamber volume" vs "internal capacity") are the census's finding, not
    its bug: they are what the attribute map has to merge.
    """
    s = raw.lower()
    s = RE_VALUE.sub(" ", s)
    s = re.sub(r"[^a-z0-9àâçéèêëîïôûùüÿñæœ]+", " ", s)
    toks = [t for t in s.split()
            if len(t) > 1 and t not in STOP and not t.isdigit()]
    return " ".join(toks)[:80]


def alpha_count(s):
    return sum(c.isalpha() for c in s)


def label_from(text):
    """(trimmed_raw, normalized) for a line, or None if it is not a label.

    The test is on the NORMALIZED string. A value cell reading "minimum 70 L"
    leaves the raw remainder "minimum", which passes a letter count but
    normalizes to nothing - the comparator is a stopword. Judging the raw
    form dropped every table-form requirement instead of looking one line up
    for the header.
    """
    cand = re.sub(r"\s+", " ", RE_VALUE.sub(" ", text)).strip(" :;.-|\t")
    if alpha_count(cand) < MIN_LABEL_ALPHA:
        return None
    norm = norm_label(cand)
    if norm in JUNK_LABELS or alpha_count(norm) < MIN_LABEL_ALPHA:
        return None
    return cand, norm


def classify(window_text):
    """GE / LE / RANGE / EQ / NONE from the line and its neighbours.

    Both bounds present is a BAND, not a threshold - a two-sided band can be
    as narrow as an equality and is part of the same signal.
    """
    ge = bool(RE_GE.search(window_text))
    le = bool(RE_LE.search(window_text))
    if RE_RANGE.search(window_text) or RE_INTERVAL.search(window_text) \
            or (ge and le):
        return "RANGE"
    if ge:
        return "GE"
    if le:
        return "LE"
    if RE_EQ.search(window_text):
        return "EQ"
    return "NONE"


def extract(txt, scoped=True):
    """Every (label, comparator, value, unit) candidate in one document.

    With `scoped`, candidates are emitted only from lines inside a
    requirement section (decision 21c). The ±2 window still reads the full
    body, because the header a bare value cell needs can sit one line above
    the section's first line.
    """
    lines = [l.strip() for l in txt.splitlines()]
    body = [l for l in lines if l and not RE_PAGENUM.match(l)]
    if scoped:
        mask, mode = scope_mask(body)
    else:
        mask, mode = [True] * len(body), "all"
    out = _harvest(body, mask, mode)

    # A "requirement section" holding no numeric requirement at all was not
    # the requirement section - the heading matched something else, or the
    # spec is laid out in a way the state machine missed. Scoping is an
    # enrichment step, so when it empties a document it has failed, and
    # failing must cost nothing. Five tenders vanished outright before this
    # existed, one of them with 110 observations.
    if scoped and not out and mode == "section":
        out = _harvest(body, [True] * len(body), "whole_empty")
    return out


def _harvest(body, mask, mode):
    out = []
    heads = section_index(body)

    for i, line in enumerate(body):
        if not mask[i]:
            continue
        if heads[i] and RE_ADMIN_SECTION.search(heads[i]):
            continue
        # 03 used an 8-character floor, which is right when the whole
        # requirement is one sentence. Here it is wrong: a table cell reading
        # "= 78 L" is six characters, and bare value cells are exactly where
        # exact values hide. The label and boilerplate filters below carry the
        # noise instead.
        if not (3 <= len(line) <= 300):
            continue
        if RE_BOILERPLATE.search(line):
            continue
        # finditer, not findall: a bare `in` is resolved by the text that
        # follows it, so the match position is part of the evidence.
        vals = [(m.group(1), m.group(2), line[m.end():m.end() + 12])
                for m in RE_VALUE.finditer(line)]
        if not vals:
            continue

        # Specs are TABLES and pdfminer emits one cell per line, so the
        # comparator often sits in a neighbouring cell, not this one. 03
        # measured that a same-line test discards 42% of real constraints.
        comparator = classify(line)
        comp_src = "inline"
        if comparator == "NONE":
            near = body[max(0, i - WINDOW):i + WINDOW + 1]
            comparator = classify(" | ".join(near))
            comp_src = "window" if comparator != "NONE" else "none"

        # The label is this line with the numbers taken out. When nothing is
        # left, the line is a bare table cell ("70 cm") and the label is the
        # row or column header ABOVE it. label_src records which, so the
        # summary can report how much of the vocabulary is inferred rather
        # than read - inferred labels are the ones that go wrong.
        got = label_from(line)
        if got:
            raw_label, label, label_src = got[0], got[1], "inline"
        else:
            raw_label = label = ""
            label_src = "none"
            for j in range(i - 1, max(-1, i - 1 - WINDOW), -1):
                up = label_from(body[j])
                if up and len(up[0]) <= 120:
                    raw_label, label, label_src = up[0], up[1], "above"
                    break
            if label_src == "none":
                continue

        part = bool(RE_PARTNUM.search(line))
        for value_text, unit, tail in vals:
            unit_resolved = ""
            if unit == "in":
                unit_resolved = resolve_in(tail)
                if not unit_resolved:
                    continue
            # "conditions 2010A" = clause 2010A, not 2010 amperes. pdfminer
            # splits the cell so "general" is on another line and the
            # boilerplate filter cannot see it; the shape is the only tell.
            if unit == "A" and re.fullmatch(r"(19|20)\d{2}", value_text):
                continue
            value = parse_number(value_text)
            if value == "":
                continue
            out.append({
                "label": label,
                "raw_label": raw_label[:120],
                "label_src": label_src,
                "comparator": comparator,
                "comp_src": comp_src,
                "value": value,
                "value_text": value_text,
                "unit": unit,
                "unit_resolved": unit_resolved,
                "dimension": DIMENSION.get(unit, "?"),
                "soft_unit": int(unit in SOFT_UNITS),
                "part_number_line": int(part),
                "scope": mode,
                "line_no": i,
                "value_lo": "",
                "value_hi": "",
                "band_width": "",
                "restated": 1,
                "base_value": "",
                "base_unit": "",
                "base_lo": "",
                "base_hi": "",
                "also_stated_as": "",
                "section": heads[i],
                "context": re.sub(
                    r"\s+", " ",
                    " | ".join(body[max(0, i - CONTEXT):i + CONTEXT + 1])
                )[:CONTEXT_CHARS],
                "source_line": re.sub(r"\s+", " ", line)[:200],
            })
    return out


# ------------------------------------------------ unit conversion - step 3
# One base per dimension. The converted value is what peer statistics run
# on; the original value, unit and source_line all survive beside it,
# because decision 3 says the auditor must see what the tender actually
# said, not what we made of it.
BASE = {"length": "mm", "mass": "kg", "volume": "L", "temperature": "degC",
        "speed": "km/h", "pressure": "kPa", "power": "W", "voltage": "V",
        "current": "A", "frequency": "Hz", "rotation": "rpm", "ratio": "%",
        "torque": "Nm", "sound": "dB", "flow": "lpm", "data": "MB",
        "data_rate": "Mbps", "light": "lm", "resolution": "dpi",
        "heat": "BTU"}

FACTOR = {
    "mm": 1.0, "cm": 10.0, "dm": 100.0, "m": 1000.0, "km": 1e6,
    "nm": 1e-6, "um": 1e-3, "µm": 1e-3,
    "in": 25.4, "inch": 25.4, "inches": 25.4, "ft": 304.8,
    "kg": 1.0, "mg": 1e-6, "g": 1e-3, "lb": 0.45359237,
    "lbs": 0.45359237, "oz": 0.028349523,
    "L": 1.0, "l": 1.0, "mL": 1e-3, "ml": 1e-3, "cc": 1e-3,
    "m3": 1000.0, "m³": 1000.0, "cm3": 1e-3, "cm³": 1e-3,
    "mm3": 1e-6, "ft3": 28.316847, "ft³": 28.316847,
    "km/h": 1.0, "kph": 1.0, "mph": 1.609344, "m/s": 3.6,
    "kPa": 1.0, "MPa": 1000.0, "bar": 100.0, "mbar": 0.1, "psi": 6.894757,
    "W": 1.0, "kW": 1000.0, "MW": 1e6, "VA": 1.0, "kVA": 1000.0,
    "V": 1.0, "kV": 1000.0, "mV": 1e-3, "A": 1.0, "mA": 1e-3,
    "Hz": 1.0, "kHz": 1e3, "MHz": 1e6, "GHz": 1e9,
    "MB": 1.0, "GB": 1024.0, "TB": 1048576.0,
    "rpm": 1.0, "%": 1.0, "Nm": 1.0, "dB": 1.0, "lpm": 1.0,
    "Mbps": 1.0, "Gbps": 1000.0, "lm": 1.0, "dpi": 1.0,
    "degC": 1.0, "°C": 1.0,
}

# Decision 15: convert or drop, NEVER guess. These stay unconverted and so
# never reach a peer statistic. `inch`/`inches` spelled out are safe; bare
# `in` is a preposition as often as a unit ("70 in total"), and bare `m`,
# `l`, `g`, `A`, `cc` collide with abbreviations. Open item 10 decides each
# of them on evidence - until then a missing observation costs one data
# point, and a wrong one manufactures a false accusation.
AMBIGUOUS = {"in", "m", "l", "g", "A"}

# `cc` is off the ambiguous list on evidence: every occurrence in 2510 is an
# engine displacement (`599cc`, `must have a displacement of at least 825
# cc`). Cubic centimetres, and leaving it out cost a whole attribute.
#
# Bare `in` is settled per row instead, by what FOLLOWS it (open item 10,
# 887 rows). Measured on 2510:
#
#     788  inches        "250 cm (98 in);"   "10 in x 4 in"   "2 in (50.8 mm)"
#      79  preposition   "Type 1 in 43-PG-17 Box"  -> not a measurement
#      20  torque        "27,650 kg-m (2,400,000 in-lbs)" -> not a length
#
# The decisive shape is a closing bracket: three quarters of them are the
# imperial twin inside `250 cm (98 in)`, so once resolved they convert and
# then fold away as restatements of the metric value (decision 22). This
# resolves units by context rather than by guessing, which is what decision
# 15 asks for - the preposition and torque rows are DROPPED, not converted.
RE_IN_UNIT = re.compile(r"^\s*\.?\s*(?:[)\];,]|$|[xX×]\s*\d|\(\s*\d)")
RE_IN_TORQUE = re.compile(r"^\s*\.?\s*[-–]\s*lbs?\b", re.I)


def resolve_in(tail):
    """'inch' if this bare `in` is a unit, '' if it is not. `tail` is the
    text immediately following the match."""
    if RE_IN_TORQUE.match(tail):
        return ""
    return "inch" if RE_IN_UNIT.match(tail) else ""

# Fahrenheit is affine, not a scale factor, so it needs its own arm.
def to_base(value, unit, dim):
    """(base_value, base_unit) or ('', '') when the unit is not convertible."""
    base = BASE.get(dim)
    if not base or unit in AMBIGUOUS:
        return "", ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "", ""
    if unit == "°F":
        return round((v - 32.0) * 5.0 / 9.0, 6), "degC"
    f = FACTOR.get(unit)
    if f is None:
        return "", ""
    return round(v * f, 6), base


def convert_units(rows):
    """Fill base_value / base_unit (and both band bounds) in place."""
    done = 0
    for r in rows:
        u = r.get("unit_resolved") or r["unit"]
        bv, bu = to_base(r["value"], u, r["dimension"])
        if bu:
            r["base_value"], r["base_unit"] = bv, bu
            done += 1
            for src, dst in (("value_lo", "base_lo"), ("value_hi", "base_hi")):
                if r.get(src) != "":
                    b, _ = to_base(r[src], u, r["dimension"])
                    r[dst] = b
    return done


def fold_restatements(rows, tol=0.02):
    """"13,600 kg (30,000 lbs)" is one requirement, written twice.

    Bilingual Canadian specs restate every dimension in metric and
    imperial. Two rows on the SAME line of the SAME document, in the same
    dimension, whose converted values agree within `tol`, are one
    observation - and a catalogue RFSO restating 47 dimensions both ways
    contributes 94 votes to a median that should hear 47. The metric row is
    kept, because the base units are metric and keeping it avoids a second
    rounding.
    """
    groups = defaultdict(list)
    out = []
    for r in rows:
        if r.get("base_value") == "":
            out.append(r)
        else:
            groups[(r["reference"], r["file"], r["line_no"], r["label"],
                    r["dimension"])].append(r)
    folded = 0
    for g in groups.values():
        kept = []
        for r in sorted(g, key=lambda x: 0 if x["unit"] in
                        ("mm", "cm", "m", "kg", "L", "km/h") else 1):
            bv = float(r["base_value"])
            twin = next((k for k in kept
                         if abs(float(k["base_value"]) - bv)
                         <= tol * max(abs(bv), 1e-9)), None)
            if twin is None:
                kept.append(r)
            else:
                twin["restated"] = twin.get("restated", 1) + 1
                twin["also_stated_as"] = f"{r['value']} {r['unit']}"
                folded += 1
        out.extend(kept)
    return out, folded


# --------------------------------------------- collapse and dedup - dec 22
def collapse_bands(rows):
    """A two-sided band is ONE observation, not two.

    The extractor emits a row per bound, so "between 1850 lbs to 2500 lbs"
    arrives as two mass observations at 1850 and 2500. Fed to a median that
    is two votes, both of them wrong: the tender requires neither value, it
    requires the interval. Collapsed to value_lo / value_hi with the
    midpoint as the single comparable value, and band_width - which IS the
    signal for decision 17.2, an exact value being the zero-width case.

    Only bounds from the SAME line of the SAME document are paired. Two
    adjacent table rows that happen to read RANGE are not one band, and the
    ±2 comparator window makes that mistake easy to arrive at.
    """
    out, pend = [], defaultdict(list)
    for r in rows:
        if r["comparator"] == "RANGE":
            pend[(r["reference"], r["file"], r["line_no"],
                  r["label"], r["unit"])].append(r)
        else:
            out.append(r)
    collapsed = 0
    for group in pend.values():
        vals = []
        for r in group:
            try:
                vals.append(float(r["value"]))
            except ValueError:
                pass
        # Exactly two distinct bounds is a band. One value is a point that
        # merely sits near the word "between"; three or more is a table row
        # the line-splitting ran together, and guessing which two are the
        # bounds would invent a requirement.
        if len(set(vals)) == 2 and len(vals) == 2:
            lo, hi = min(vals), max(vals)
            r = dict(group[0])
            r["value_lo"], r["value_hi"] = lo, hi
            r["band_width"] = round(hi - lo, 6)
            r["value"] = repr(round((lo + hi) / 2.0, 6))
            out.append(r)
            collapsed += 1
        else:
            out.extend(group)
    return out, collapsed


def dedup(rows, cut):
    """One observation per (tender, family, attribute, value, unit).

    The same requirement is restated in the RFP, then in amendment 01, then
    in amendment 02, and pdfminer repeats lines within one document on top
    of that. Measured: 911 observations on the comparable attributes become
    457, losing zero tenders and zero attributes, and the share held by the
    two largest catalogue RFSOs falls from 38% to 21%.

    `restated` keeps the count, because how emphatically a tender repeats a
    requirement is evidence a human auditor may want - it is just not a
    second data point.
    """
    seen, out = {}, []
    for r in rows:
        # Key on the CONVERTED value where there is one, so the same
        # requirement written in kg by one document and lb by another
        # folds; fall back to the raw pair when it could not be converted.
        v = r.get("base_value") or r["value"]
        u = r.get("base_unit") or r["unit"]
        k = (r["reference"], r["unspsc8"][:cut], r["label"], v, u,
             r.get("base_hi", "") or r.get("value_hi", ""))
        if k in seen:
            seen[k]["restated"] += 1
            continue
        r["restated"] = 1
        seen[k] = r
        out.append(r)
    return out


# ------------------------------------------------- the 8-digit family + date
def tender_meta(tenders_path, refs):
    """reference -> (unspsc8, pub_date). One streaming pass, stdlib csv.

    The 8-digit code is the product family and it is already in the feed -
    4110 is only the first four digits. No classifier is needed: the tree
    exists and is official.
    """
    C_REF = "referenceNumber-numeroReference"
    C_UN = "unspsc"
    C_PUB = "publicationDate-datePublication"
    meta = {}
    if not os.path.exists(tenders_path):
        print(f"  WARNING: {tenders_path} not found - no product family or "
              f"date. Census will group on the 4-digit category only.")
        return meta
    with io.open(tenders_path, encoding="utf-8-sig", errors="replace",
                 newline="") as f:
        for row in csv.DictReader(f):
            ref = (row.get(C_REF) or "").strip()
            if ref not in refs:
                continue
            raw = row.get(C_UN) or ""
            code = ""
            for t in re.split(r"[\r\n,;|\s]+", raw):
                d = re.sub(r"\D", "", t)
                if len(d) >= 4:
                    code = d
                    break
            meta[ref] = (code[:8], (row.get(C_PUB) or "").strip()[:10])
    return meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="4110")
    ap.add_argument("--text-dir", default="reports/text")
    ap.add_argument("--tenders", default="data/tenders.csv")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--min-peers", type=int, default=8,
                    help="distinct tenders an attribute needs before an "
                         "outlier test is allowed to speak. Below this the "
                         "agent must return 'insufficient peers', not a weak "
                         "score - decision 13.")
    ap.add_argument("--family", choices=["unspsc8", "unspsc6", "unspsc4"],
                    default="unspsc6",
                    help="which level to group peers at. MEASURED on 2510: "
                         "8-digit shatters into 42 groups with a median of 2 "
                         "tenders; 6-digit gives 7 groups, 6 of them clearing "
                         "n>=8. Pick the level from the data, once, and keep "
                         "it fixed - adapting it per finding is decision 13.")
    ap.add_argument("--top", type=int, default=40,
                    help="labels to print in the summary")
    ap.add_argument("--no-convert", dest="convert", action="store_false",
                    help="skip unit conversion. The default converts to one "
                         "base per dimension (mm, kg, L, degC, km/h) and "
                         "folds metric/imperial restatements of the same "
                         "requirement. Ambiguous units are left unconverted "
                         "rather than guessed - decision 15.")
    ap.add_argument("--no-dedup", dest="dedup", action="store_false",
                    help="keep every restatement of a requirement. The "
                         "default folds them (decision 22): the same value "
                         "repeated in an amendment is not a second "
                         "observation, and leaving them in lets two "
                         "catalogue RFSOs decide a third of every median.")
    ap.add_argument("--no-collapse-bands", dest="collapse_bands",
                    action="store_false",
                    help="keep a two-sided band as two rows, one per bound. "
                         "The default collapses it to one observation "
                         "carrying value_lo, value_hi and band_width.")
    ap.add_argument("--scope", choices=["section", "all"], default="section",
                    help="'section' extracts only inside the requirement "
                         "sections of each document, falling back to the "
                         "whole document when none is found (decision 21c). "
                         "'all' is the pre-2026-10-14 behaviour, kept so the "
                         "baseline stays reproducible.")
    ap.add_argument("--map", nargs="?", const="reports/attribute_map.json",
                    default="",
                    help="apply an attribute map built by prepare/05_attribute_map.py "
                         "before counting, so synonyms count as one attribute "
                         "and labels marked 'unknown' are dropped. Without "
                         "this the census reports RAW labels, which is the "
                         "honest pre-map floor.")
    args = ap.parse_args()

    # Fail before the expensive work, not after it: extraction plus the
    # tenders.csv join is ~2 minutes, and erroring at the end of that is just
    # two wasted minutes.
    if args.map and not os.path.exists(args.map):
        sys.exit(f"missing {args.map} - run prepare/05_attribute_map.py first")

    tdir = os.path.join(args.text_dir, args.group)
    if not os.path.isdir(tdir):
        sys.exit(f"missing {tdir} - run prepare/02_fetch_docs.py --only {args.group} "
                 f"--per-cat 0, then prepare/03_inspect_specs.py")
    files = sorted(f for f in os.listdir(tdir) if f.endswith(".txt"))
    if not files:
        sys.exit(f"no cached text in {tdir}")
    print(f"{len(files):,} cached documents in {tdir}")

    # ---------------------------------------------------------- extraction
    rows = []
    per_doc = {}
    for n, fn in enumerate(files, 1):
        ref = fn.split("__", 1)[0]
        try:
            with io.open(os.path.join(tdir, fn), encoding="utf-8",
                         errors="replace") as fh:
                txt = fh.read()
        except Exception as e:
            print(f"  unreadable {fn}: {e}")
            continue
        got = extract(txt, scoped=(args.scope == "section"))
        for r in got:
            r["reference"] = ref
            r["file"] = fn
        rows.extend(got)
        per_doc[fn] = (ref, Counter(r["comparator"] for r in got))
        if n % 100 == 0:
            print(f"  {n:5d}/{len(files)} ...")

    if not rows:
        sys.exit("no candidates extracted - check the text cache")

    if args.collapse_bands:
        rows, _nb = collapse_bands(rows)
        print(f"  {_nb:,} two-sided bands collapsed to one observation each")

    if args.convert:
        _nc = convert_units(rows)
        print(f"  {_nc:,}/{len(rows):,} converted to a base unit "
              f"({len(rows)-_nc:,} left unconverted - ambiguous unit, "
              f"decision 15)")
        rows, _nf = fold_restatements(rows)
        print(f"  {_nf:,} metric/imperial restatements of one requirement "
              f"folded")

    refs = {r["reference"] for r in rows}
    print(f"\n{len(rows):,} candidates from {len(refs):,} tenders")
    print(f"joining {args.tenders} for the 8-digit family and date ...")
    meta = tender_meta(args.tenders, refs)
    for r in rows:
        u8, date = meta.get(r["reference"], ("", ""))
        r["unspsc8"] = u8 or args.group
        r["pub_date"] = date
    family_of = {r["reference"]: r["unspsc8"] for r in rows}

    # Applied BEFORE the per-row CSV is written, so census_<grp>_mapped.csv
    # really holds canonical labels. The raw_label column survives either
    # way, so a wrong merge stays traceable to what the tender actually
    # said, and the unmapped census_<grp>.csv is kept beside it.
    # in census_<grp>.csv. The map is a frozen judgement that may be wrong,
    # and a wrong merge must stay traceable back to what the tender said.
    if args.map:
        with io.open(args.map, encoding="utf-8") as f:
            _m = json.load(f)
        _m = _m.get("map", _m)
        _unknown = _kept = _renamed = 0
        _out = []
        for r in rows:
            canon = _m.get(r["label"])
            if canon == "unknown":
                _unknown += 1
                continue
            if canon and canon != r["label"]:
                r["label"] = canon
                _renamed += 1
            _kept += 1
            _out.append(r)
        rows = _out
        print(f"map applied: {_renamed:,} rows renamed, {_unknown:,} dropped "
              f"as boilerplate, {_kept:,} kept")
        if not rows:
            sys.exit("the map dropped every row - check it by hand")

    _cut = {"unspsc8": 8, "unspsc6": 6, "unspsc4": 4}[args.family]
    if args.dedup:
        _before = len(rows)
        rows = dedup(rows, _cut)
        print(f"dedup: {_before:,} -> {len(rows):,} observations "
              f"({_before - len(rows):,} restatements folded)")

    # ------------------------------------------------------------- per-row
    _sfx = "_mapped" if args.map else ""
    full = os.path.join(args.out, f"census_{args.group}{_sfx}.csv")
    cols = ["reference", "unspsc8", "pub_date", "label", "raw_label",
            "label_src", "comparator", "comp_src", "value", "value_text",
            "unit", "unit_resolved", "dimension",
            "base_value", "base_unit", "value_lo", "value_hi", "base_lo",
            "base_hi", "band_width", "restated", "also_stated_as",
            "soft_unit", "part_number_line", "scope", "file", "line_no",
            "section", "source_line", "context"]
    with io.open(full, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # -------------------------------------------------------- the grouping
    # One tender counts ONCE per attribute. Departments reuse templates and
    # repeat the same requirement across lots; counting every repetition
    # would fabricate the denominator and make the test lie in our favour.
    key_of = lambda r: (r["unspsc8"][:_cut] or args.group,
                        r["dimension"], r["label"])

    tenders_per_key = defaultdict(set)
    comp_per_key = defaultdict(Counter)
    src_per_key = defaultdict(Counter)
    vals_per_key = defaultdict(list)
    raws_per_key = defaultdict(Counter)
    dates_per_key = defaultdict(list)
    for r in rows:
        k = key_of(r)
        tenders_per_key[k].add(r["reference"])
        comp_per_key[k][r["comparator"]] += 1
        src_per_key[k][r["label_src"]] += 1
        raws_per_key[k][r["raw_label"]] += 1
        if r["pub_date"]:
            dates_per_key[k].append(r["pub_date"])
        # ONLY converted values. A median over a column holding millimetres,
        # inches and metres at once is the number that made `overall width`
        # read as 37 - decision 15's whole point.
        if r.get("base_value") != "":
            try:
                vals_per_key[k].append(float(r["base_value"]))
            except (TypeError, ValueError):
                pass

    lab_path = os.path.join(args.out,
                            f"census_{args.group}{_sfx}_labels.csv")
    with io.open(lab_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["unspsc8", "dimension", "label", "tenders", "obs",
                    "GE", "LE", "RANGE", "EQ", "NONE", "label_src_above",
                    "base_unit", "n_converted", "median_base",
                    "date_min", "date_max", "variants"])
        for k in sorted(tenders_per_key,
                        key=lambda x: -len(tenders_per_key[x])):
            u8, dim, lab = k
            c = comp_per_key[k]
            v = vals_per_key[k]
            d = sorted(dates_per_key[k])
            w.writerow([
                u8, dim, lab, len(tenders_per_key[k]), sum(c.values()),
                c["GE"], c["LE"], c["RANGE"], c["EQ"], c["NONE"],
                src_per_key[k]["above"],
                BASE.get(dim, ""), len(v),
                round(statistics.median(v), 3) if v else "",
                d[0] if d else "", d[-1] if d else "",
                " | ".join(x for x, _ in raws_per_key[k].most_common(4)),
            ])

    # ----------------------------------------------------------- summary
    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say("=" * 74)
    say(f"VOCABULARY CENSUS - {args.group}   "
        f"(peers grouped at {args.family}, scope={args.scope}"
        + (", attribute map APPLIED)" if args.map else ", RAW labels)"))
    say("=" * 74)

    say(f"\n  documents ............ {len(files):,}")
    say(f"  tenders .............. {len(refs):,}")
    say(f"  candidates extracted . {len(rows):,}")
    say(f"  distinct labels ...... {len(tenders_per_key):,}")

    fams = Counter(v[:_cut] for v in family_of.values())
    say(f"\n  product families (UNSPSC-8) in this category: {len(fams)}")
    for fam, c in fams.most_common(12):
        say(f"    {fam:10s} {c:4,} tenders")
    if len(fams) > 12:
        say(f"    ... and {len(fams)-12} more")
    say("\n  Splitting by family is what makes a comparison meaningful, and"
        "\n  it is also what costs peers. The count above is how fine the"
        "\n  split can go before the n >= %d floor bites." % args.min_peers)

    # -------------------------------------------- THE question: peer depth
    sizes = sorted((len(v) for v in tenders_per_key.values()), reverse=True)
    reach = [k for k, v in tenders_per_key.items()
             if len(v) >= args.min_peers]
    say("\n" + "=" * 74)
    say(f"PEER DEPTH - does an attribute reach n >= {args.min_peers} tenders?")
    say("=" * 74)
    say("")
    for thr in (2, 4, 6, 8, 12, 20, 30):
        c = sum(1 for s in sizes if s >= thr)
        say(f"  labels with >= {thr:3d} tenders : {c:6,}"
            f"   {100.0*c/len(sizes):5.1f}% of labels")
    say(f"\n  median label appears in {statistics.median(sizes):.0f} tender(s)")
    say(f"  labels seen in exactly 1 tender: "
        f"{sum(1 for s in sizes if s == 1):,} "
        f"({100.0*sum(1 for s in sizes if s==1)/len(sizes):.1f}%)")

    covered = set()
    for k in reach:
        covered |= tenders_per_key[k]
    say(f"\n  {len(reach):,} labels clear the floor, covering "
        f"{len(covered):,}/{len(refs):,} tenders "
        f"({100.0*len(covered)/max(len(refs),1):.0f}%)")
    say("\n  That coverage is the honest ceiling on the peer detector: a"
        "\n  tender with no attribute above the floor can only be judged by"
        "\n  the within-document test below.")

    # ------------------------------------------------------- what they are
    say("\n" + "=" * 74)
    say(f"TOP {args.top} LABELS BY TENDER COUNT")
    say("=" * 74)
    say("\n  The near-duplicates here are the finding, not the noise - they")
    say("  are what the attribute map has to merge, and merging them is what")
    say("  pushes labels above the floor.\n")
    say(f"  {'n':>4} {'fam':>8} {'dim':12s} {'GE/LE':>7} {'EQ':>4} "
        f"{'RNG':>4}  label")
    for k in sorted(tenders_per_key,
                    key=lambda x: -len(tenders_per_key[x]))[:args.top]:
        u8, dim, lab = k
        c = comp_per_key[k]
        say(f"  {len(tenders_per_key[k]):4d} {u8:>8} {dim:12s} "
            f"{c['GE']+c['LE']:7d} {c['EQ']:4d} {c['RANGE']:4d}  {lab[:38]}")

    # ------------------------------------------------------------- hygiene
    say("\n" + "=" * 74)
    say("EXTRACTION HYGIENE - how much of this is trustworthy?")
    say("=" * 74)
    csrc = Counter(r["comp_src"] for r in rows)
    say(f"\n  comparator on the same line . {csrc['inline']:6,} "
        f"{100.0*csrc['inline']/len(rows):5.1f}%")
    say(f"  comparator from the window .. {csrc['window']:6,} "
        f"{100.0*csrc['window']/len(rows):5.1f}%   <- can bleed across rows")
    src = Counter(r["label_src"] for r in rows)
    say(f"\n  label read on the same line . {src['inline']:6,} "
        f"{100.0*src['inline']/len(rows):5.1f}%")
    say(f"  label taken from above ...... {src['above']:6,} "
        f"{100.0*src['above']/len(rows):5.1f}%   <- inferred, check these")
    scp = Counter(r["scope"] for r in rows)
    if args.scope == "section":
        say(f"\n  from a requirement section  {scp['section']:6,} "
            f"{100.0*scp['section']/len(rows):5.1f}%")
        say(f"  from a document with none .. {scp['whole']:6,} "
            f"{100.0*scp['whole']/len(rows):5.1f}%   <- unscoped fallback")
        say(f"  section found but empty .... {scp['whole_empty']:6,} "
            f"{100.0*scp['whole_empty']/len(rows):5.1f}%   <- scoping failed")
    soft = sum(r["soft_unit"] for r in rows)
    part = sum(r["part_number_line"] for r in rows)
    unk = sum(1 for r in rows if r["dimension"] == "?")
    say(f"\n  ambiguous unit (in/m/l/g/A/cc) {soft:6,} "
        f"{100.0*soft/len(rows):5.1f}%   <- decision 15 territory")
    say(f"  line also looks like a part no. {part:6,} "
        f"{100.0*part/len(rows):5.1f}%   <- drop candidates")
    say(f"  unit with no dimension mapped . {unk:6,} "
        f"{100.0*unk/len(rows):5.1f}%")
    say("\n  'taken from above' is the number to watch. Those labels were"
        "\n  inferred from a neighbouring line, and a wrong one silently"
        "\n  merges two unrelated attributes. Spot-check them in"
        f"\n  census_{args.group}.csv before trusting any group built on them.")
    say("\n  unit distribution:")
    for u, c in Counter(r["unit"] for r in rows).most_common(15):
        say(f"    {u:6s} {c:6,}  {DIMENSION.get(u,'?')}")

    # --------------------------------------------------------------- dates
    dates = sorted(d for d in (r["pub_date"] for r in rows) if d)
    say("\n" + "=" * 74)
    say("DATE SPREAD")
    say("=" * 74)
    if dates:
        yrs = Counter(d[:4] for d in dates)
        say(f"\n  {dates[0]} .. {dates[-1]}\n")
        for y in sorted(yrs):
            say(f"    {y}  {yrs[y]:6,}  {'#'*int(40*yrs[y]/max(yrs.values()))}")
        say("\n  Physical dimensions barely drift; performance specs do."
            "\n  Decide the comparison window from this spread, not in"
            "\n  advance - a sliding window cuts n, the scarce resource.")
    else:
        say("\n  no dates joined (is data/tenders.csv present?)")

    # ------------------------------------------- the no-peer baseline
    say("\n" + "=" * 74)
    say("WITHIN-DOCUMENT BASELINE - the detector that needs no peers")
    say("=" * 74)
    per_tender = defaultdict(Counter)
    for r in rows:
        per_tender[r["reference"]][r["comparator"]] += 1
    thr_tot = sum(c["GE"] + c["LE"] for c in per_tender.values())
    eq_tot = sum(c["EQ"] for c in per_tender.values())
    rng_tot = sum(c["RANGE"] for c in per_tender.values())
    none_tot = sum(c["NONE"] for c in per_tender.values())
    all_tot = max(thr_tot + eq_tot + rng_tot + none_tot, 1)
    say(f"\n  threshold (GE/LE) .. {thr_tot:6,}  {100.0*thr_tot/all_tot:5.1f}%")
    say(f"  band (RANGE) ....... {rng_tot:6,}  {100.0*rng_tot/all_tot:5.1f}%")
    say(f"  exact (EQ) ......... {eq_tot:6,}  {100.0*eq_tot/all_tot:5.1f}%"
        "   <- the signal")
    say(f"  no comparator ...... {none_tot:6,}  "
        f"{100.0*none_tot/all_tot:5.1f}%")

    with_eq = [(ref, c) for ref, c in per_tender.items() if c["EQ"]]
    say(f"\n  tenders with >= 1 exact value: {len(with_eq):,}/"
        f"{len(per_tender):,} ({100.0*len(with_eq)/max(len(per_tender),1):.0f}%)")
    say("\n  Highest exact-to-threshold ratio - level-2 candidates needing no"
        "\n  peer group at all:\n")
    say(f"  {'EQ':>4} {'THR':>5} {'ratio':>7}  tender")
    ranked = sorted(with_eq,
                    key=lambda x: -(x[1]["EQ"] /
                                    float(x[1]["EQ"] + x[1]["GE"] +
                                          x[1]["LE"] + 1)))
    for ref, c in ranked[:15]:
        thr = c["GE"] + c["LE"]
        say(f"  {c['EQ']:4d} {thr:5d} {c['EQ']/float(c['EQ']+thr):7.2f}  {ref}")
    say("\n  This is the number the peer model has to beat. It needs no"
        "\n  attribute map, no unit conversion and no peers, and it works on"
        "\n  the thin categories too.")

    out_txt = os.path.join(args.out, f"census_{args.group}{_sfx}.txt")
    with io.open(out_txt, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n  every candidate -> {full}")
    print(f"  per label       -> {lab_path}   <- read this one")
    print(f"  summary         -> {out_txt}")


if __name__ == "__main__":
    main()
