"""
CANADA - STEP 3: do the downloaded documents contain parseable technical
specifications?

This is the go / no-go test for the Spec Auditor agent (M2). It does not
detect anything yet - it measures whether there is anything to detect.

Per document it answers:
  1. Is there extractable text at all, or is it a scan that would need OCR?
  2. Does the text carry NUMERIC REQUIREMENTS - a comparator, a number and a
     unit (">= 70 cm", "minimum 230 V", "+/- 0.5 mm")? That is the raw
     material for detection level 2, the disguised specification.
  3. Does it name BRANDS, and is "or equivalent" present? That is detection
     level 1, and the ratio between the two is the first thing to measure.
  4. Does it have a technical-specification section at all?

Then it rolls up per UNSPSC-4 group and prints a verdict, because the
decision is per category: the comparator only works where enough peers in the
same group carry comparable numbers.

PDF text comes from pdfminer.six - pure Python, no compiled DLL, so unlike
pandas it is not blocked by Application Control. .docx is read with the
stdlib zipfile module, no dependency at all.

Extracted text is cached under reports/text/, so re-runs are instant and the
text is reusable by later steps.

Run:
    pip install pdfminer.six
    python 03_inspect_specs.py
    python 03_inspect_specs.py --max-pages 60 --limit 50

Writes:
    reports/text/<unspsc4>/<reference>__<file>.txt   cached plain text
    reports/spec_inspection.csv                      one row per document
    reports/spec_inspection_summary.txt              the verdict
    reports/spec_samples.txt                         real requirement lines
"""

import argparse
import csv
import json
import os
import re
import statistics
import sys
import zipfile
from collections import Counter, defaultdict

csv.field_size_limit(10_000_000)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ------------------------------------------------------------- the signals
# A unit vocabulary deliberately kept narrow. Bare "C" and "t" are left out:
# they match prose constantly and would inflate every count.
UNIT = (r"(?:mm|cm|dm|km|m|nm|um|µm|in|inch|inches|ft|"
        r"kg|mg|g|lb|lbs|oz|"
        r"ml|mL|l|L|cc|"
        r"kV|mV|V|mA|A|kW|MW|W|VA|kVA|"
        r"GHz|MHz|kHz|Hz|rpm|"
        r"psi|kPa|MPa|bar|mbar|"
        r"°C|°F|degC|"
        r"%|dB|lm|lx|cd|"
        r"GB|TB|MB|Mbps|Gbps|dpi|ppi|px|"
        r"Nm|BTU|CFM|lpm|gpm)")

RE_NUM_UNIT = re.compile(r"(?<![\w.])\d{1,6}(?:[.,]\d+)?\s?" + UNIT + r"\b")

# Tight form: the comparator sits directly on the number ("minimum 15000 rpm").
# Kept as its own count, but it is NOT what decides anything - see below.
RE_COMPARATOR = re.compile(
    r"(?:≥|≤|>=|=<|<=|>|<|±|\+/-|"
    r"\bmin\.?\b|\bmax\.?\b|\bminimum\b|\bmaximum\b|"
    r"\bat least\b|\bnot less than\b|\bno less than\b|\bnot more than\b|"
    r"\bgreater than\b|\bless than\b|\bup to\b|\bor more\b|\bor less\b|"
    r"\bminimale?\b|\bmaximale?\b|\bau moins\b|\bau plus\b)"
    r"\s*(?:of\s+|de\s+)?\d", re.I)

# Real procurement prose puts the noun between the comparator and the number:
#   "the turning radius must not exceed 3.96 m"
#   "must have a nominal overall length of 16,150 mm"
# So the constraint is detected AT LINE LEVEL - the token anywhere on a line
# that also carries a number and a unit. Measured against the cached text,
# this finds 2.3x the requirement lines the tight form does.
RE_CONSTRAINT = re.compile(r"""
    ≥|≤|>=|=<|<=|±|\+/-
  | \bmin\.?\b|\bmax\.?\b|\bminimum\b|\bmaximum\b|\bminimale?\b|\bmaximale?\b
  | \bat\ least\b|\bno(?:t)?\ (?:less|more)\ than\b|\bnot\ (?:to\ )?exceed\b
  | \bgreater\ than\b|\bless\ than\b|\bup\ to\b
  | \bor\ (?:more|less|greater|higher|lower|better)\b
  | \bshall\ (?:be|have|not|provide|deliver|meet|withstand|measure)\b
  | \bmust\ (?:be|have|not|provide|deliver|meet|withstand|measure)\b
  | \bcapable\ of\b|\brated\ (?:at|for)\b|\bnominal\b|\btolerance\b
  | \bau\ moins\b|\bau\ plus\b|\bdoit\ être\b|\bne\ doit\ pas\ dépasser\b
""", re.I | re.X)

# "38 mm to 100 mm", "0 degC to 40 degC" - an interval, also a constraint.
RE_INTERVAL = re.compile(
    r"\d{1,6}(?:[.,]\d+)?\s?" + UNIT + r"?\s*(?:to|–|—|-|à|and)\s*"
    r"\d{1,6}(?:[.,]\d+)?\s?" + UNIT + r"\b", re.I)

# Administrative numbers that look like requirements and are not: bid file
# size limits, page counts, payment terms, insurance, delivery deadlines.
RE_BOILERPLATE = re.compile(
    r"(?:file|e-?mail|attachment|message|upload|submission)s?\b.{0,40}"
    r"\b(?:KB|MB|GB)\b"
    r"|\b(?:KB|MB|GB)\b.{0,40}\b(?:limit|maximum|attachment)"
    r"|\bfont\b|\bmargins?\b|\bdouble.?spaced\b|\bpage limit\b"
    r"|\bno more than \d+\s*pages?\b|\b\d+\s*pages? (?:maximum|or less)\b"
    r"|\binsurance\b|\bliabilit|\bbid security\b|\bperformance bond\b"
    r"|\bGST\b|\bHST\b|\bQST\b|\bwithholding\b"
    r"|\b(?:calendar|business|working) days?\b|\bwithin \d+ days?\b"
    r"|\b\d+\s?% of the (?:contract|bid|total|value)", re.I)

# What kind of document is this? Amendments and bid forms carry no spec and
# would otherwise drag every document-level percentage down.
ROLE_PATTERNS = [
    ("amendment", r"amend|modif|addend|revis"),
    ("spec", r"annex|appendi|spec|sow|statement[_-]?of[_-]?work|technical|"
             r"requirement|exigence|devis"),
    ("solicitation", r"rfp|rfq|itq|ital|rfsa|acan|pwgsc|solicit|tender|invitation"),
    ("form", r"form|certif|checklist|bid[_-]?submis|pricing|price"),
]


def doc_role(filename):
    low = filename.lower()
    for role, pat in ROLE_PATTERNS:
        if re.search(pat, low):
            return role
    return "other"

# An exact value where a threshold belongs - the disguised specification.
RE_EXACT = re.compile(
    r"(?<![<>=!≥≤])=\s*\d|\bexactly\s+\d|\bprecisely\s+\d|"
    r"\bshall be\s+\d+(?:[.,]\d+)?\s?" + UNIT + r"\b", re.I)

RE_OR_EQUIV = re.compile(
    r"or\s+(?:an\s+)?equivalent|or\s+equal\b|ou\s+(?:l[''])?équivalent",
    re.I)
RE_TRADEMARK = re.compile(r"[®™]")
# "Brand Model" shapes: Capitalised word + an alphanumeric model token.
# "Agilent 8890", "Eppendorf 5920R". The model token must mix letters and
# digits or be long, and the leading word must not be structural vocabulary -
# without the stoplist this matched "Annex 3" and fired on 90% of documents.
RE_MODELNUM = re.compile(
    r"\b([A-Z][A-Za-z]{2,})\s+(?:model\s+)?"
    r"((?=[A-Z0-9-]*[A-Z])[A-Z]{1,4}-?\d{2,5}[A-Z\d-]{0,4}|\d{3,5}[A-Z]{1,4})\b")

NOT_A_BRAND = {
    "annex", "annexe", "appendix", "appendice", "article", "attachment",
    "section", "clause", "paragraph", "page", "part", "phase", "item",
    "table", "figure", "note", "schedule", "amendment", "revision",
    "canada", "canadian", "ottawa", "quebec", "ontario", "january",
    "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "number", "reference",
    "solicitation", "tender", "contract", "request", "standard", "iso",
    "class", "type", "group", "level", "option", "line", "column", "row",
    "total", "version", "form", "chapter", "exhibit", "addendum",
    # standards bodies - a cited standard is the opposite of a brand lock-in
    "csa", "ulc", "ansi", "astm", "nfpa", "iec", "din", "nema", "sae",
    "mil", "ieee", "nist", "cgsb", "onsgc", "iso", "en", "ul",
    # document and building vocabulary that reads like "Brand Model"
    "pdf", "doc", "docx", "xls", "bldg", "building", "room", "suite",
    "states", "conditions", "features", "drawing", "model", "series",
    "grade", "size", "width", "height", "length", "weight", "capacity",
    "rfp", "rfq", "itq", "rfsa", "acan", "sow", "nsn", "gsin", "unspsc",
}


def brand_models(txt):
    out = set()
    for lead, model in RE_MODELNUM.findall(txt):
        if lead.lower() in NOT_A_BRAND:
            continue
        # Canadian solicitation numbers (W8486-227231, E60HP-11000x) read
        # exactly like a model number and are everywhere in these documents.
        if model.endswith("-") or re.fullmatch(r"[A-Z]\d{4}", model):
            continue
        out.add((lead, model))
    return out

SECTION_WORDS = [
    "technical specification", "technical requirement", "technical criteria",
    "statement of work", "scope of work", "mandatory requirement",
    "mandatory technical", "specifications", "specification sheet",
    "requirements matrix", "annex a", "appendix a", "attachment 1",
    "spécifications techniques", "exigences techniques",
    "énoncé des travaux", "besoin technique",
]

RE_FRENCH = re.compile(
    r"\b(?:les|des|une|pour|avec|doit|soumissionnaire|exigences)\b")

# Non-blank lines either side of a number in which a constraint still counts.
WINDOW = 2

# A requirement line: short, carries a number with a unit, not a page header.
RE_PAGENUM = re.compile(r"^\s*(?:page\s*)?\d+\s*(?:of|/|de)\s*\d+\s*$", re.I)


# ----------------------------------------------------------- text getters
def pdf_text(path, max_pages):
    from pdfminer.high_level import extract_text
    from pdfminer.layout import LAParams
    return extract_text(path, maxpages=max_pages or 0,
                        laparams=LAParams(line_margin=0.4, char_margin=2.0))


def pdf_pages(path):
    try:
        with open(path, "rb") as f:
            blob = f.read()
        n = blob.count(b"/Type /Page") + blob.count(b"/Type/Page")
        n -= blob.count(b"/Type /Pages") + blob.count(b"/Type/Pages")
        return max(n, 0)
    except Exception:
        return 0


RE_XML_TAG = re.compile(r"<[^>]+>")


def docx_text(path):
    """stdlib only: a .docx is a zip of XML."""
    out = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist()
                 if n == "word/document.xml"
                 or re.match(r"word/(header|footer)\d*\.xml$", n)]
        for n in sorted(names):
            xml = z.read(n).decode("utf-8", "replace")
            xml = re.sub(r"</w:p>", "\n", xml)
            xml = re.sub(r"</w:tc>", "\t", xml)
            xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
            xml = re.sub(r"<w:br[^>]*/>", "\n", xml)
            out.append(RE_XML_TAG.sub("", xml))
    txt = "\n".join(out)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        txt = txt.replace(a, b)
    return txt


def xlsx_text(path):
    """Shared strings are enough to see whether a spec table lives here."""
    try:
        with zipfile.ZipFile(path) as z:
            if "xl/sharedStrings.xml" not in z.namelist():
                return ""
            xml = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        xml = re.sub(r"</si>", "\n", xml)
        return RE_XML_TAG.sub("", xml)
    except Exception:
        return ""


def get_text(path, kind, max_pages):
    if kind == "pdf":
        return pdf_text(path, max_pages), pdf_pages(path)
    if kind == "docx":
        return docx_text(path), 0
    if kind == "xlsx":
        return xlsx_text(path), 0
    raise ValueError(f"no extractor for {kind}")


# -------------------------------------------------------------- measuring
def measure(txt):
    lines = [l.strip() for l in txt.splitlines()]
    body = [l for l in lines if l and not RE_PAGENUM.match(l)]
    low = txt.lower()

    candidates = [l for l in body
                  if 8 <= len(l) <= 300 and RE_NUM_UNIT.search(l)]
    req_lines = [l for l in candidates if not RE_BOILERPLATE.search(l)]
    dropped = len(candidates) - len(req_lines)

    # Specifications arrive as TABLES. pdfminer emits a table cell per line, so
    # "Minimum chamber depth" and "70 cm" land on different lines and a
    # same-line test throws away 42% of the real constraints (measured). So the
    # constraint is looked for in a window of +/-WINDOW non-blank lines.
    index = {l: i for i, l in enumerate(body)}
    constrained, intervals, inline = [], [], 0
    for l in req_lines:
        if RE_INTERVAL.search(l):
            intervals.append(l)
        if RE_CONSTRAINT.search(l) or RE_INTERVAL.search(l):
            inline += 1
            constrained.append(l)
            continue
        i = index.get(l)
        if i is None:
            continue
        near = body[max(0, i - WINDOW):i + WINDOW + 1]
        if any(RE_CONSTRAINT.search(x) for x in near):
            constrained.append(l)

    return {
        "chars": len(txt),
        "lines": len(body),
        "num_unit": len(RE_NUM_UNIT.findall(txt)),
        "comparator": len(RE_COMPARATOR.findall(txt)),
        "exact_value": len(RE_EXACT.findall(txt)),
        "trademark": len(RE_TRADEMARK.findall(txt)),
        "brand_model": len(brand_models(txt)),
        "or_equivalent": len(RE_OR_EQUIV.findall(txt)),
        "spec_sections": sum(1 for w in SECTION_WORDS if w in low),
        "req_lines": len(req_lines),
        "constrained_lines": len(constrained),
        "constrained_inline": inline,
        "interval_lines": len(intervals),
        "boilerplate_dropped": dropped,
        "french": len(RE_FRENCH.findall(low)) > 40,
        "_req_samples": constrained[:6] or req_lines[:6],
    }


def verdict(m, pages, args):
    # Order matters. A scanned PDF yields a few stray characters per page, so
    # the per-page density is the test, not the total; NO_TEXT is reserved for
    # formats with no page count to divide by.
    if pages:
        if m["chars"] / pages < args.min_chars_per_page:
            return "SCANNED"
    elif m["chars"] < 200:
        return "NO_TEXT"
    if m["req_lines"] >= args.min_req_lines \
            and m["constrained_lines"] >= args.min_constrained:
        return "SPEC_RICH"
    if m["req_lines"] >= 3 or m["spec_sections"] >= 1:
        return "SPEC_THIN"
    return "NO_SPEC"


RANK = {"SPEC_RICH": 4, "SPEC_THIN": 3, "NO_SPEC": 2, "SCANNED": 1,
        "NO_TEXT": 0, "UNREADABLE": 0, "SKIPPED": 0}

OUT_COLS = [
    "unspsc4", "reference", "file", "role", "kind", "pages", "chars",
    "chars_per_page", "lines", "req_lines", "constrained_lines",
    "constrained_inline", "interval_lines", "boilerplate_dropped",
    "num_unit", "comparator",
    "exact_value", "brand_model", "trademark", "or_equivalent",
    "spec_sections", "french", "verdict", "error",
]


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", default="reports/docs_manifest.csv")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--text-dir", default="reports/text")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="pages of each PDF to read; 0 = all. Do not cap this: "
                         "the technical annex sits at the BACK of an RFP, and "
                         "a 40-page cap truncated 18%% of the first run's docs.")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N documents (quick smoke test)")
    ap.add_argument("--kinds", default="pdf,docx,xlsx")
    ap.add_argument("--min-chars-per-page", type=int, default=120,
                    help="below this a PDF is treated as a scan")
    ap.add_argument("--min-req-lines", type=int, default=8)
    ap.add_argument("--min-constrained", type=int, default=4,
                    help="numeric lines carrying a constraint or an interval")
    ap.add_argument("--min-peers", type=int, default=30,
                    help="projected spec-rich tenders a group needs to be "
                         "comparable within itself (matches 01's --min-usable)")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the cached text and re-extract")
    args = ap.parse_args()

    if not os.path.exists(args.manifest):
        sys.exit(f"missing {args.manifest} - run 02_fetch_docs.py first")

    want = {k.strip().lower() for k in args.kinds.split(",") if k.strip()}
    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.text_dir, exist_ok=True)

    have_pdfminer = True
    try:
        import pdfminer  # noqa: F401
    except ImportError:
        have_pdfminer = False
        print("WARNING: pdfminer.six is not installed - PDFs will be skipped.")
        print("         pip install pdfminer.six   (pure Python, no DLL)\n")

    docs = []
    with open(args.manifest, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            lp = r.get("local_path") or ""
            if lp:
                # a manifest written on Windows carries backslashes
                lp = lp.replace("\\", os.sep).replace("/", os.sep)
                r["local_path"] = lp
            if not lp or not os.path.exists(lp):
                continue
            docs.append(r)

    seen = set()
    uniq = []
    for r in docs:
        if r["local_path"] in seen:
            continue
        seen.add(r["local_path"])
        uniq.append(r)
    docs = uniq
    print(f"{len(docs):,} downloaded documents in the manifest")
    if not docs:
        sys.exit("No file in the manifest exists on disk - nothing to inspect.\n"
                 "Check that local_path in the manifest matches where the files "
                 "actually are, then re-run. (Nothing was overwritten.)")

    rows = []
    n = 0
    for r in docs:
        if args.limit and n >= args.limit:
            break
        kind = (r.get("sniffed") or "").lower()
        path = r["local_path"]
        out = {
            "unspsc4": r.get("unspsc4", ""),
            "reference": r.get("reference", ""),
            "file": os.path.basename(path),
            "role": doc_role(os.path.basename(path)),
            "kind": kind, "pages": 0, "chars": 0, "chars_per_page": 0,
            "verdict": "SKIPPED", "error": "",
        }
        if kind not in want or (kind == "pdf" and not have_pdfminer):
            rows.append(out)
            continue

        n += 1
        cache_dir = os.path.join(args.text_dir, out["unspsc4"])
        os.makedirs(cache_dir, exist_ok=True)
        cache = os.path.join(
            cache_dir, f"{out['reference']}__{out['file']}.txt"[:180])

        txt, pages = None, 0
        if os.path.exists(cache) and not args.refresh:
            try:
                with open(cache, encoding="utf-8") as fh:
                    txt = fh.read()
                pages = pdf_pages(path) if kind == "pdf" else 0
            except Exception:
                txt = None
        if txt is None:
            try:
                txt, pages = get_text(path, kind, args.max_pages)
                with open(cache, "w", encoding="utf-8") as fh:
                    fh.write(txt)
            except Exception as e:
                out["verdict"] = "UNREADABLE"
                out["error"] = f"{type(e).__name__}: {e}"[:160]
                rows.append(out)
                if n % 25 == 0:
                    print(f"  {n:5d}/{len(docs)} ...")
                continue

        m = measure(txt)
        out.update({k: v for k, v in m.items() if not k.startswith("_")})
        out["pages"] = pages
        out["chars_per_page"] = round(m["chars"] / pages, 1) if pages else ""
        out["verdict"] = verdict(m, pages, args)
        out["_samples"] = m["_req_samples"]
        rows.append(out)
        if n % 25 == 0:
            print(f"  {n:5d} inspected ...")

    # ---------------------------------------------------------- per-doc CSV
    csv_path = os.path.join(args.out, "spec_inspection.csv")
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # ----------------------------------------------------------- roll-ups
    inspected = [r for r in rows if r["verdict"] not in ("SKIPPED",)]
    by_cat_docs = defaultdict(list)
    best_per_tender = {}
    for r in inspected:
        by_cat_docs[r["unspsc4"]].append(r)
        key = (r["unspsc4"], r["reference"])
        cur = best_per_tender.get(key)
        if cur is None or RANK[r["verdict"]] > RANK[cur["verdict"]]:
            best_per_tender[key] = r

    by_cat_tenders = defaultdict(list)
    for (cat, _ref), r in best_per_tender.items():
        by_cat_tenders[cat].append(r)

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say("=" * 74)
    say("SPEC PARSEABILITY - can the Spec Auditor agent read these documents?")
    say("=" * 74)

    vc = Counter(r["verdict"] for r in inspected)
    tot = max(len(inspected), 1)
    say(f"\nDocuments inspected: {len(inspected):,}  "
        f"(skipped {sum(1 for r in rows if r['verdict'] == 'SKIPPED'):,} "
        f"non-text kinds)\n")
    for v in ("SPEC_RICH", "SPEC_THIN", "NO_SPEC", "SCANNED", "NO_TEXT",
              "UNREADABLE"):
        c = vc.get(v, 0)
        say(f"  {v:11s} {c:6,}  {100.0*c/tot:5.1f}%")
    say("\n  SPEC_RICH = extractable text with >= "
        f"{args.min_req_lines} numeric requirement lines, of which")
    say(f"              >= {args.min_constrained} carry a constraint "
        "(>=, min/max, 'must not exceed', an interval)")
    say("  SCANNED   = image-only PDF, would need OCR before anything else")

    say("\n  by document role (filename):\n")
    byrole = defaultdict(Counter)
    for r in inspected:
        byrole[r.get("role", "other")][r["verdict"]] += 1
    say(f"  {'role':14s} {'docs':>6} {'rich':>6} {'thin':>6} {'none':>6} "
        f"{'%rich':>7}")
    for role in sorted(byrole, key=lambda k: -sum(byrole[k].values())):
        c = byrole[role]
        nn = sum(c.values())
        say(f"  {role:14s} {nn:6,} {c['SPEC_RICH']:6,} {c['SPEC_THIN']:6,} "
            f"{c['NO_SPEC'] + c['NO_TEXT']:6,} "
            f"{100.0*c['SPEC_RICH']/nn:6.1f}%")
    say("\n  Amendments and bid forms carry no specification by design. A low"
        "\n  share there is correct, not a failure - which is why the verdict"
        "\n  counts tenders, not documents.")

    say("\n" + "=" * 74)
    say("PER CATEGORY  (one tender counts once, by its best document)")
    say("=" * 74)
    say(f"\n  {'grp':5s} {'tenders':>8} {'rich':>6} {'thin':>6} {'scan':>6} "
        f"{'none':>6} {'%rich':>7} {'bst req':>8} {'bst cst':>8}")
    cat_rich = {}
    for cat in sorted(by_cat_tenders):
        ts = by_cat_tenders[cat]
        vcat = Counter(t["verdict"] for t in ts)
        rich = vcat.get("SPEC_RICH", 0)
        share = rich / float(len(ts))
        cat_rich[cat] = (share, rich, len(ts))
        # medians over each tender's BEST document, not over every document:
        # a tender with one spec annex and four amendments is not a thin tender.
        reqs = [t["req_lines"] for t in ts if isinstance(t.get("req_lines"), int)]
        comps = [t["constrained_lines"] for t in ts
                 if isinstance(t.get("constrained_lines"), int)]
        say(f"  {cat:5s} {len(ts):8,} {rich:6,} "
            f"{vcat.get('SPEC_THIN', 0):6,} {vcat.get('SCANNED', 0):6,} "
            f"{vcat.get('NO_SPEC', 0) + vcat.get('NO_TEXT', 0):6,} "
            f"{100*share:6.1f}% "
            f"{statistics.median(reqs) if reqs else 0:8.0f} "
            f"{statistics.median(comps) if comps else 0:8.0f}")

    say("\n" + "=" * 74)
    say("BRAND NAMING  (detection level 1, previewed)")
    say("=" * 74)
    with_tm = sum(1 for r in inspected if (r.get("trademark") or 0) > 0)
    with_bm = sum(1 for r in inspected if (r.get("brand_model") or 0) > 0)
    with_oe = sum(1 for r in inspected if (r.get("or_equivalent") or 0) > 0)
    tm_no_oe = sum(1 for r in inspected
                   if (r.get("trademark") or 0) > 0
                   and not (r.get("or_equivalent") or 0))
    say(f"\n  docs with (R)/(TM) marks ........ {with_tm:6,}  "
        f"{100.0*with_tm/tot:5.1f}%")
    say(f"  docs with a 'Brand Model' shape . {with_bm:6,}  "
        f"{100.0*with_bm/tot:5.1f}%")
    say(f"  docs saying 'or equivalent' ..... {with_oe:6,}  "
        f"{100.0*with_oe/tot:5.1f}%")
    say(f"  marks but NO 'or equivalent' .... {tm_no_oe:6,}  "
        f"{100.0*tm_no_oe/tot:5.1f}%   <- level-1 candidates")
    say("\n  Heuristic only. It shows the signal exists and is not rare;"
        "\n  the real check reads the brand in context.")

    say("\n" + "=" * 74)
    say("EXACT VALUES  (detection level 2, previewed)")
    say("=" * 74)
    with_ex = sum(1 for r in inspected if (r.get("exact_value") or 0) > 0)
    say(f"\n  docs with an exact '= <number> <unit>' ... {with_ex:6,}  "
        f"{100.0*with_ex/tot:5.1f}%")
    say("  An exact value where a threshold belongs is the disguised"
        "\n  specification. Only an outlier test inside the same UNSPSC"
        "\n  group can say whether one is suspicious.")

    # ----------------------------------------------------------- verdict
    say("\n" + "=" * 74)
    say("VERDICT")
    say("=" * 74)
    # The question is NOT "what share of tenders is readable". Detection level 2
    # compares a value against its peers in the same UNSPSC group, so what
    # matters is how many PEERS each group ends up with once the sampled rate is
    # applied to the whole group. A 30%-readable group of 460 beats a
    # 70%-readable group of 40.
    counts = {}
    cpath = os.path.join(args.out, "category_counts.json")
    if os.path.exists(cpath):
        with open(cpath, encoding="utf-8") as f:
            counts = json.load(f)
    usable = counts.get("usable", {})
    labels = counts.get("labels", {})

    say(f"\n  Bar: a group needs >= {args.min_peers} tenders with a parseable"
        " specification,")
    say("  projecting the sampled rate onto the whole group.\n")
    say(f"  {'grp':5s} {'sampled':>8} {'rich':>5} {'rate':>7} {'usable':>7} "
        f"{'projected':>10}  verdict")
    ok, bad = [], []
    for c in sorted(cat_rich):
        s, r, nn = cat_rich[c]
        tot_usable = usable.get(c)
        proj = int(round(s * tot_usable)) if tot_usable else None
        peers = proj if proj is not None else r
        good = peers >= args.min_peers
        (ok if good else bad).append(c)
        say(f"  {c:5s} {nn:8,} {r:5,} {100*s:6.1f}% "
            f"{(tot_usable if tot_usable else 0):7,} "
            f"{(proj if proj is not None else 0):10,}  "
            f"{'PASS' if good else 'FAIL'}   {labels.get(c, '')}")
    if not usable:
        say("\n  (no reports/category_counts.json - re-run 02 to get the"
            " projection; judged on the sample alone)")

    scan_share = vc.get("SCANNED", 0) / float(tot)
    if ok and not bad:
        say("\n  GO. Every group clears the bar. The Spec Auditor is buildable"
            "\n  on text extraction alone - no OCR needed in the first build.")
    elif ok:
        say(f"\n  PARTIAL GO. Build on {', '.join(sorted(ok))} - that is enough"
            "\n  to train and measure the within-category comparator."
            f"\n  {', '.join(sorted(bad))} is too thin to compare inside; keep it"
            "\n  out of scope for now rather than weakening the model.")
    else:
        say("\n  NO GO as it stands.")
    if scan_share > 0.15:
        say(f"\n  {100*scan_share:.0f}% of documents are scans - OCR would be the"
            " single biggest win.")
    else:
        say(f"\n  Scans are only {100*scan_share:.1f}% of documents, so OCR is"
            " not the bottleneck.")
    thin = sum(1 for r in inspected if r["verdict"] == "SPEC_THIN")
    say(f"  {thin:,} documents are SPEC_THIN - they have some numbers but few."
        "\n  That is where the next improvement lives: check whether the"
        "\n  specification is in a .zip appendix (--include-zip in 02), an"
        "\n  .xlsx requirement matrix, or a drawing with no text layer.")

    sum_path = os.path.join(args.out, "spec_inspection_summary.txt")
    with open(sum_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # ------------------------------------------------------------ samples
    samp_path = os.path.join(args.out, "spec_samples.txt")
    with open(samp_path, "w", encoding="utf-8") as f:
        f.write("Real requirement lines pulled out of the documents.\n"
                "Read these - they are the evidence that parsing works.\n")
        for cat in sorted(by_cat_docs):
            f.write("\n" + "=" * 70 + f"\n{cat}\n" + "=" * 70 + "\n")
            shown = 0
            for r in sorted(by_cat_docs[cat],
                            key=lambda x: -(x.get("req_lines") or 0)):
                s = r.get("_samples") or []
                if not s:
                    continue
                f.write(f"\n-- {r['reference']} / {r['file']} "
                        f"({r['verdict']}, {r.get('req_lines')} req lines)\n")
                for line in s:
                    f.write("   " + re.sub(r"\s+", " ", line) + "\n")
                shown += 1
                if shown >= 8:
                    break

    print(f"\n  per document -> {csv_path}")
    print(f"  summary      -> {sum_path}")
    print(f"  samples      -> {samp_path}   <- read this one")
    print(f"  cached text  -> {args.text_dir}/")


if __name__ == "__main__":
    main()
