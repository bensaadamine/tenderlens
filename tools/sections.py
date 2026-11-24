"""
SECTIONS - where a line sits in a tender document.

    scope_mask(lines)       for each line: is it inside the requirements
                            section? (brand search and number extraction
                            read only there)
    section_index(lines)    for each line: the nearest heading above it
    RE_ADMIN_SECTION        headings whose content is about packaging,
                            shipping, payment or warranty, not the product
"""


import re


def alpha_count(s):
    """How many letters a string holds."""
    return sum(c.isalpha() for c in s)


# headings that open the requirements section (incl. "<word> REQUIREMENTS")
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
  | ^(?:[a-zà-ÿ]+\ ){0,2}requirements?$
""", re.I | re.X)

# administrative headings: they close it (tested first)
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

# PART / ANNEX / SECTION n: closes it, unless the heading names the
# requirements
RE_STRUCT_HEAD = re.compile(
    r"^(?:part|partie|annex|annexe|appendix|appendice|attachment|section)"
    r"\s*[-–—:]?\s*[A-Za-z0-9]{1,3}\b", re.I)

# page headers repeated on every page, not headings
RE_RUNNING_HEAD = re.compile(r"^(?:A/)?(?:DLP|DAAT|RDIMS|TBIPS|SACC|CCC)\b", re.I)

# table-of-contents lines (dot leaders, page number)
RE_TOC_ENTRY = re.compile(r"(?:\.{3,}|…)\s*\d{1,4}$|\s{2,}\d{1,4}$")

# a numbered clause title: "10.  Evaluation Procedures"
RE_NUM_HEAD = re.compile(r"^\d{1,2}(?:\.\d{1,2})*\.?\s{2,}[A-Z]")


def is_heading(line):
    """Could this line be a section heading? Only its shape counts: 5-90
    characters, mostly upper case or a numbered clause title.
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
    """The heading at line i, joined with the next line when the PDF split
    it ("ANNEX C" + "TECHNICAL EVALUATION MATRIX").
    """
    s = body[i].strip()
    if RE_STRUCT_HEAD.match(s) and alpha_count(RE_STRUCT_HEAD.sub("", s, 1)) < 3:
        for k in (i + 1, i + 2):
            if k < len(body) and is_heading(body[k]):
                return s + " - " + body[k].strip()
    return s


# sections about packaging, shipping, payment or warranty, not the product
RE_ADMIN_SECTION = re.compile(
    r"palletiz|packag|\bpacking\b|transportation cost|carrier\b"
    r"|\bshipping\b|\bshipment|\bmarking\b|preservation"
    r"|basis of payment|invoic|\bwarrant(?:y|ies)\b", re.I)


def section_index(body):
    """For each line, the nearest heading at or above it."""
    heads, cur = [""] * len(body), ""
    for i, line in enumerate(body):
        if is_heading(line):
            cur = heading_title(body, i)[:90]
        heads[i] = cur
    return heads


def scope_mask(body):
    """For each line: is it inside the part that describes what is bought?

    Reads the headings in order, like a switch: a requirements heading
    (STATEMENT OF REQUIREMENT, SPECIFICATIONS...) turns it on, an
    administrative heading (INSURANCE, BASIS OF PAYMENT...) or a new
    PART/ANNEX turns it off; other lines keep the current state.

    Returns (mask, mode). mode is "section" when a requirements heading was
    found; otherwise "whole" and every line is kept - reading everything is
    safer than cutting at random.
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
                inside = False
        mask[i] = inside
    if not found:
        return [True] * len(body), "whole"
    return mask, "section"
