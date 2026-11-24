"""
The census: every numeric requirement in a category's tender documents.

Reads the requirements section of each document, extracts every
(label, comparator, value, unit), cleans the rows (bands, unit conversion,
metric/imperial restatements, repeats). The level-2 peer comparison reads
these rows (tools/level2.py), and the attribute map is built from them
(prepare/attribute_map.py). The same extraction runs on each new tender in
`python tenderlens.py run` (pipeline/stream.py).

Labels are kept as the tender writes them; prepare/attribute_map.py merges
synonyms into one attribute, and --map applies its map here.

Run:
    python tenderlens.py prepare census --group 2510 [--map]

Writes reports/census_<grp>[_mapped].csv, one row per observation.
"""


import argparse
import csv
import io
import json
import os
import re
import sys
from collections import defaultdict

from tools.sections import (RE_ADMIN_SECTION, alpha_count, scope_mask, section_index)

csv.field_size_limit(10_000_000)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# unit -> physical dimension (length, mass...)
DIMENSION = {}
for _dim, _units in {
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

# units that can also be a word or an abbreviation (the soft_unit column)
SOFT_UNITS = {"in", "inch", "inches", "m", "l", "g", "A", "cc"}

# a compound unit must come before its prefix (km/h before km): the first
# branch that matches wins
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

# a number: grouped thousands (13,600 / 26 300) or plain (3.96 / 0,5)
NUM = (r"\d{1,3}(?:[,   ]\d{3})+(?:\.\d+)?"
       r"|\d{1,6}(?:[.,]\d+)?")

RE_VALUE = re.compile(
    r"(?<![\w.\-/])(" + NUM + r")\s?(" + UNIT + r")\b")

RE_GROUPED = re.compile(r"^\d{1,3}(?:[,   ]\d{3})+(?:\.\d+)?$")
RE_DEC_COMMA = re.compile(r"^\d+,\d{1,2}$")


def parse_number(raw):
    """Number text -> decimal string, or '' if it is not a number.
    A comma or space before 3 digits separates thousands (13,600 / 26 300);
    a comma before 1 or 2 digits is a decimal comma (3,96).
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


# comparator words: at least / at most / a band / exactly
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

RE_INTERVAL = re.compile(
    r"\d{1,6}(?:[.,]\d+)?\s?" + UNIT + r"?\s*(?:to|through|–|—|à)\s*"
    r"\d{1,6}(?:[.,]\d+)?\s?" + UNIT + r"\b", re.I)

RE_EQ = re.compile(
    r"(?<![<>=!≥≤])=\s*\d|\bexactly\s+\d|\bprecisely\s+\d", re.I)

# lines about the bid, payment, packaging, people's body sizes or a clause
# number - not the product
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
    r"|general conditions|supplemental conditions|standard instructions"
    r"|conditions g\u00e9n\u00e9rales|instructions uniformis\u00e9es"
    r"|\b20\d{2}[A-Z]?\s*\(\d{4}-\d{2}-\d{2}\)"
    r"|\b[A-Z]\d{4,}-\d{5,}"
    r"|net.?zero|greenhouse gas|\bghg\b"
    r"|zip files|file attachments|inch format"
    r"|8\.5\s?[xX]\s?11|letter.?size|\bpaper\b.{0,25}(?:mm|inch)"
    r"|\bpallets?\b|\bskids?\b|\bcrates?\b|\bdunnage\b|\bISPM\b"
    r"|\bshipments?\b|\bpacking list\b|\bpackaging\b|\bcartons?\b"
    r"|\bstrap(?:ped|ping)?\b|\bshrink.?wrap\b|\bpalletiz"
    r"|\bpopliteal\b|\bbuttock\b|\bsitting height\b|\belbow rest\b"
    r"|\bchest (?:depth|circumference)\b|\bhip breadth\b|\bstature\b"
    r"|\banthropometric\b|\bbust circumference\b|\bwaist circumference\b"
    r"|repair and overhaul|\bRMA\b", re.I)

# looks like a part number (30x9.5R16), not a measurement
RE_PARTNUM = re.compile(
    r"\b\d{1,3}\s?[xX×]\s?\d{1,3}(?:\.\d)?\s?[A-Z]\s?\d{2}\b"
    r"|\b[A-Z]{1,3}\d{3,}[A-Z\d-]*\b"
    r"|\b\d{2,}-\d{3,}\b")

RE_PAGENUM = re.compile(r"^\s*(?:page\s*)?\d+\s*(?:of|/|de)\s*\d+\s*$", re.I)

# words removed from a label before counting
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

# table furniture, not a label
JUNK_LABELS = {"", "yes", "no", "n a", "na", "tbd", "see annex", "as per",
               "item", "description", "qty", "quantity", "unit", "units",
               "comply", "compliance", "y n", "oui non", "s o"}

# lines either side searched for a comparator, and above for a label
WINDOW = 2
MIN_LABEL_ALPHA = 3

# lines either side saved with each row (its `context`), for the attribute
# map's LLM and for a reader
CONTEXT = 3
CONTEXT_CHARS = 400


def norm_label(raw):
    """A label as a counting key: lower case, no numbers, units,
    punctuation or filler words.
    """
    s = raw.lower()
    s = RE_VALUE.sub(" ", s)
    s = re.sub(r"[^a-z0-9àâçéèêëîïôûùüÿñæœ]+", " ", s)
    toks = [t for t in s.split()
            if len(t) > 1 and t not in STOP and not t.isdigit()]
    return " ".join(toks)[:80]


def label_from(text):
    """(label as written, counting key) for a line, or None when no
    label is left once its numbers are removed.
    """
    cand = re.sub(r"\s+", " ", RE_VALUE.sub(" ", text)).strip(" :;.-|\t")
    if alpha_count(cand) < MIN_LABEL_ALPHA:
        return None
    norm = norm_label(cand)
    if norm in JUNK_LABELS or alpha_count(norm) < MIN_LABEL_ALPHA:
        return None
    return cand, norm


def classify(window_text):
    """The comparator a text states: GE (at least), LE (at most), RANGE
    (both bounds or a band), EQ (exactly) or NONE.
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


def extract(txt):
    """Every (label, comparator, value, unit) in one document, read in its
    requirements section - or in the whole document if that gives nothing.
    """
    lines = [l.strip() for l in txt.splitlines()]
    body = [l for l in lines if l and not RE_PAGENUM.match(l)]
    mask, mode = scope_mask(body)
    out = _harvest(body, mask, mode)
    if not out and mode == "section":
        out = _harvest(body, [True] * len(body), "whole_empty")
    return out


def _harvest(body, mask, mode):
    """The rows of one document, from the lines `mask` keeps.
    Tables give one cell per line, so a value with no comparator or label on
    its line takes them from the WINDOW lines around it. Skipped: packaging,
    shipping, payment and warranty sections, administrative lines, a bare `in`
    that is not inches, and a year read as amperes (clause "2010A").
    """
    out = []
    heads = section_index(body)

    for i, line in enumerate(body):
        if not mask[i]:
            continue
        if heads[i] and RE_ADMIN_SECTION.search(heads[i]):
            continue
        if not (3 <= len(line) <= 300):
            continue
        if RE_BOILERPLATE.search(line):
            continue
        vals = [(m.group(1), m.group(2), line[m.end():m.end() + 12])
                for m in RE_VALUE.finditer(line)]
        if not vals:
            continue
        comparator = classify(line)
        comp_src = "inline"
        if comparator == "NONE":
            near = body[max(0, i - WINDOW):i + WINDOW + 1]
            comparator = classify(" | ".join(near))
            comp_src = "window" if comparator != "NONE" else "none"
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


# the base unit of each dimension, and each unit's factor to it
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

# never converted: each can also be a word or an abbreviation
AMBIGUOUS = {"in", "m", "l", "g", "A"}

# a bare `in` is inches when followed by ')', ',', end of line or 'x 4'
RE_IN_UNIT = re.compile(r"^\s*\.?\s*(?:[)\];,]|$|[xX×]\s*\d|\(\s*\d)")
RE_IN_TORQUE = re.compile(r"^\s*\.?\s*[-–]\s*lbs?\b", re.I)


def resolve_in(tail):
    """'inch' when a bare `in` is a unit, '' when it is a word or part of
    'in-lbs'. `tail` is the text right after it.
    """
    if RE_IN_TORQUE.match(tail):
        return ""
    return "inch" if RE_IN_UNIT.match(tail) else ""


def to_base(value, unit, dim):
    """(value in the dimension's base unit, base unit), or ('', '') when
    the unit is ambiguous or unknown: convert or drop, never guess.
    """
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
    """Fill each row's base_value / base_unit (and band bounds) in place;
    returns how many rows were converted.
    """
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
    """One requirement written twice on the same line, metric and imperial
    ("13,600 kg (30,000 lbs)"), becomes one row: the metric one.
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


def collapse_bands(rows):
    """The two bounds of a band on the same line ("1850 to 2500 lbs")
    become one row: value_lo, value_hi, band_width, and the midpoint as value.
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
    """One row per tender, attribute and value. Repeats (amendments, lines
    repeated in a document) only add to `restated`.
    """
    seen, out = {}, []
    for r in rows:
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


def tender_meta(tenders_path, refs):
    """reference -> (8-digit UNSPSC code, publication date), read from the
    notices file.
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


CENSUS_COLS = ["reference", "unspsc8", "pub_date", "label", "raw_label",
               "label_src", "comparator", "comp_src", "value", "value_text",
               "unit", "unit_resolved", "dimension",
               "base_value", "base_unit", "value_lo", "value_hi", "base_lo",
               "base_hi", "band_width", "restated", "also_stated_as",
               "soft_unit", "part_number_line", "scope", "file", "line_no",
               "section", "source_line", "context"]


def parse_args():
    """The command-line options."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="4110")
    ap.add_argument("--text-dir", default="reports/text")
    ap.add_argument("--tenders", default="data/tenders.csv")
    ap.add_argument("--out", default="reports")
    ap.add_argument("--map", nargs="?", const="reports/attribute_map.json",
                    default="",
                    help="apply the attribute map (prepare/attribute_map.py): "
                         "synonyms count as one attribute, 'unknown' is dropped")
    return ap.parse_args()


def main():
    """The census of one category, step by step:
    read the text -> extract every numeric requirement -> clean (bands,
    units, restatements) -> join the notices (family, date) -> apply the
    label map -> one observation per tender -> write the rows."""
    args = parse_args()
    if args.map and not os.path.exists(args.map):
        sys.exit(f"missing {args.map} - run prepare/attribute_map.py first")

    tdir, files = text_files(args.text_dir, args.group)
    rows = extract_documents(tdir, files)
    rows = clean_rows(rows)
    refs = {r["reference"] for r in rows}
    print(f"\n{len(rows):,} candidates from {len(refs):,} tenders")
    join_notices(rows, args.tenders, args.group)
    if args.map:
        rows = apply_map(rows, args.map)
    before = len(rows)
    rows = dedup(rows, 8)
    print(f"dedup: {before:,} -> {len(rows):,} observations "
          f"({before - len(rows):,} restatements folded)")

    sfx = "_mapped" if args.map else ""
    full = os.path.join(args.out, f"census_{args.group}{sfx}.csv")
    write_rows(rows, full)
    print(f"\n  every observation -> {full}")


def text_files(text_dir, group):
    """(folder, the category's extracted .txt files) - or exit saying
    which preparation step is missing."""
    tdir = os.path.join(text_dir, group)
    if not os.path.isdir(tdir):
        sys.exit(f"missing {tdir} - run prepare/fetch_docs.py --only {group} "
                 f"--per-cat 0, then prepare/extract_text.py")
    files = sorted(f for f in os.listdir(tdir) if f.endswith(".txt"))
    if not files:
        sys.exit(f"no cached text in {tdir}")
    print(f"{len(files):,} cached documents in {tdir}")
    return tdir, files


def extract_documents(tdir, files):
    """The rows of every document of the category, each tagged with its
    tender and file.
    """
    rows = []
    for n, fn in enumerate(files, 1):
        ref = fn.split("__", 1)[0]
        try:
            with io.open(os.path.join(tdir, fn), encoding="utf-8",
                         errors="replace") as fh:
                txt = fh.read()
        except Exception as e:
            print(f"  unreadable {fn}: {e}")
            continue
        got = extract(txt)
        for r in got:
            r["reference"] = ref
            r["file"] = fn
        rows.extend(got)
        if n % 100 == 0:
            print(f"  {n:5d}/{len(files)} ...")
    if not rows:
        sys.exit("no candidates extracted - check the text cache")
    return rows


def clean_rows(rows):
    """Bands to one row, values to base units, metric/imperial
    restatements to one row.
    """
    rows, nb = collapse_bands(rows)
    print(f"  {nb:,} two-sided bands collapsed to one observation each")
    nc = convert_units(rows)
    print(f"  {nc:,}/{len(rows):,} converted to a base unit "
          f"({len(rows)-nc:,} left unconverted - ambiguous unit, "
          f"decision 15)")
    rows, nf = fold_restatements(rows)
    print(f"  {nf:,} metric/imperial restatements of one requirement "
          f"folded")
    return rows


def join_notices(rows, tenders_path, group):
    """Each row gets its tender's 8-digit UNSPSC family and publication
    date from the notices file."""
    print(f"joining {tenders_path} for the 8-digit family and date ...")
    meta = tender_meta(tenders_path, {r["reference"] for r in rows})
    for r in rows:
        u8, date = meta.get(r["reference"], ("", ""))
        r["unspsc8"] = u8 or group
        r["pub_date"] = date


def apply_map(rows, map_path):
    """Rename each label to its attribute from the map; drop the rows the
    map calls 'unknown'. raw_label keeps the tender's wording.
    """
    with io.open(map_path, encoding="utf-8") as f:
        m = json.load(f)
    m = m.get("map", m)
    unknown = kept = renamed = 0
    out = []
    for r in rows:
        canon = m.get(r["label"])
        if canon == "unknown":
            unknown += 1
            continue
        if canon and canon != r["label"]:
            r["label"] = canon
            renamed += 1
        kept += 1
        out.append(r)
    print(f"map applied: {renamed:,} rows renamed, {unknown:,} dropped "
          f"as boilerplate, {kept:,} kept")
    if not out:
        sys.exit("the map dropped every row - check it by hand")
    return out


def write_rows(rows, path):
    """Write the rows to a CSV file."""
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CENSUS_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


if __name__ == "__main__":
    main()
