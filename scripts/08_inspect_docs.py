"""
STEP 7 - Look inside the specification documents.

This is the question that decides whether the Spec Auditor agent is buildable:
when a Ukrainian buyer writes a technical requirement, what does it look like?

It measures four things:

  A. STRUCTURE  - prose, numbered lists, or tables? Tables are easy to parse,
                  prose is hard. This sets the difficulty of the whole agent.

  B. COMPARATORS - "not less than 70 cm" is an open requirement. "= 78 cm" is
                  closed, and closed requirements are what tailoring looks like.
                  The ratio of open to closed is our core feature.

  C. "OR EQUIVALENT" - the legally required phrase. In Ukrainian:
                  "або еквівалент" / "чи еквівалент" / "або аналог".
                  A brand name without it is a violation on its face.

  D. BRAND NAMES - Latin-script words inside Cyrillic text are usually
                  manufacturer or model names.

It also writes real excerpts to a text file so you can read them yourself.
Do read them - no statistic replaces looking at ten actual requirements.

Install first:
    pip install python-docx

Run:
    python 08_inspect_docs.py
    python 08_inspect_docs.py --samples 40
"""

import argparse
import csv
import os
import re
from collections import Counter

try:
    import docx  # python-docx
except ImportError:
    raise SystemExit("Install python-docx first:  pip install python-docx")

# ---------------------------------------------------------------------------
# Ukrainian requirement language. These phrases are the backbone of the whole
# analysis, so they are listed explicitly rather than hidden in a model.
# ---------------------------------------------------------------------------

OPEN_TERMS = [           # open requirement: a floor or a ceiling, not a point
    "не менше", "не менш ніж", "не менш як", "щонайменше",
    "не більше", "не більш ніж", "не більш як",
    "не гірше", "не нижче", "не вище",
    "від", "до", "мін.", "макс.", "мінімум", "максимум",
]
OPEN_SYMBOLS = ["≥", "≤", ">=", "<=", ">", "<"]

EQUIVALENT_TERMS = [     # the legally required escape hatch
    "або еквівалент", "чи еквівалент", "або аналог", "еквівалент",
]

# A number followed by a unit - the shape of a technical parameter.
UNIT = (r"(?:мм|см|м|кг|г|т|л|мл|вт|квт|в|а|гц|кгц|мгц|ггц|"
        r"гб|мб|тб|дюйм\w*|год|хв|с|°c|%|шт)")
NUM_UNIT = re.compile(r"\d+(?:[.,]\d+)?\s*" + UNIT, re.IGNORECASE)

# Latin words of 3+ chars: in a Cyrillic document these are usually brands
# or model numbers. The stoplist removes technical vocabulary that is not.
LATIN_WORD = re.compile(r"\b[A-Za-z][A-Za-z0-9\-]{2,}\b")
NOT_BRANDS = {
    "usb", "hdmi", "vga", "dvi", "led", "lcd", "oled", "ips", "tft",
    "ram", "rom", "ssd", "hdd", "ddr", "ddr3", "ddr4", "ddr5", "nvme",
    "ghz", "mhz", "khz", "rpm", "dpi", "ppm", "cmyk", "rgb", "ip",
    "wifi", "wi-fi", "bluetooth", "ethernet", "rj", "lan", "wan", "poe",
    "pdf", "doc", "docx", "xls", "xlsx", "jpg", "png", "html", "xml",
    "din", "iso", "gost", "dstu", "din-", "en", "ce", "rohs", "ul",
    "max", "min", "type", "class", "mode", "pro", "plus", "ultra",
    "windows", "linux", "android", "office",  # OS names: debatable, excluded
    "www", "http", "https", "com", "ua", "net", "org",
}


def doc_text_blocks(path):
    """Return (paragraph_texts, table_cell_texts) from a .docx."""
    d = docx.Document(path)
    paras = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    cells = []
    for table in d.tables:
        for row in table.rows:
            for cell in row.cells:
                t = cell.text.strip()
                if t:
                    cells.append(t)
    return paras, cells


def classify(line):
    low = line.lower()
    has_num = bool(NUM_UNIT.search(low))
    has_open = (any(t in low for t in OPEN_TERMS)
                or any(s in line for s in OPEN_SYMBOLS))
    has_equiv = any(t in low for t in EQUIVALENT_TERMS)
    brands = [w for w in LATIN_WORD.findall(line)
              if w.lower() not in NOT_BRANDS and not w.isdigit()]
    return has_num, has_open, has_equiv, brands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", default="data/docs")
    ap.add_argument("--out", dest="outdir", default="reports")
    ap.add_argument("--samples", type=int, default=30,
                    help="requirement lines to write out for reading")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    files = []
    for root, _, names in os.walk(args.docs):
        for nm in names:
            if nm.lower().endswith(".docx"):
                files.append(os.path.join(root, nm))

    others = Counter()
    for root, _, names in os.walk(args.docs):
        for nm in names:
            e = nm.rsplit(".", 1)[-1].lower() if "." in nm else "?"
            if e != "docx" and e != "csv":
                others[e] += 1

    print(f"Readable .docx files: {len(files)}")
    if others:
        print(f"Other formats present (not parsed here): {dict(others)}")
        print("  .doc is legacy binary Word. Convert with LibreOffice:")
        print('  soffice --headless --convert-to docx --outdir <dir> <files>')
    print()

    if not files:
        raise SystemExit("No .docx found. Run 07_fetch_docs.py first.")

    per_file, samples = [], []
    tot_para = tot_cell = 0
    n_num = n_open = n_closed = 0
    brand_counter = Counter()
    lines_with_brand = brand_with_equiv = 0
    failed = 0

    for path in files:
        try:
            paras, cells = doc_text_blocks(path)
        except Exception as e:
            failed += 1
            continue

        tot_para += len(paras)
        tot_cell += len(cells)

        f_num = f_open = f_closed = f_brand = f_equiv = 0

        for line in paras + cells:
            if len(line) < 3 or len(line) > 600:
                continue
            has_num, has_open, has_equiv, brands = classify(line)

            if has_num:
                f_num += 1
                n_num += 1
                if has_open:
                    f_open += 1
                    n_open += 1
                else:
                    f_closed += 1
                    n_closed += 1
                if len(samples) < args.samples and len(line) < 220:
                    samples.append((os.path.basename(path),
                                    "OPEN " if has_open else "CLOSED",
                                    line))
            if brands:
                f_brand += 1
                lines_with_brand += 1
                for b in brands:
                    brand_counter[b] += 1
                if has_equiv:
                    brand_with_equiv += 1
                    f_equiv += 1

        per_file.append({
            "file": os.path.basename(path),
            "cpv4": os.path.basename(os.path.dirname(path)),
            "paragraphs": len(paras), "table_cells": len(cells),
            "numeric_reqs": f_num, "open": f_open, "closed": f_closed,
            "lines_with_brand": f_brand, "brand_with_equivalent": f_equiv,
        })

    # ---------------- A. STRUCTURE ----------------
    print("=" * 66)
    print("A. HOW ARE THESE DOCUMENTS BUILT?")
    print("=" * 66)
    total_blocks = tot_para + tot_cell
    print(f"\n  files parsed .............. {len(files) - failed}"
          f"{f'  ({failed} unreadable)' if failed else ''}")
    print(f"  paragraphs ................ {tot_para:7d}  "
          f"{100*tot_para/total_blocks:5.1f}%")
    print(f"  table cells ............... {tot_cell:7d}  "
          f"{100*tot_cell/total_blocks:5.1f}%")
    print("\n  Table-heavy documents are good news: a table row is already a")
    print("  structured requirement. Prose-heavy means real NLP work.")

    # ---------------- B. OPEN vs CLOSED ----------------
    print("\n" + "=" * 66)
    print("B. OPEN vs CLOSED REQUIREMENTS  (the core feature)")
    print("=" * 66)
    print(f"\n  lines with a number + unit ... {n_num}")
    if n_num:
        print(f"  OPEN   (не менше, ≥, від-до) . {n_open:6d}  {100*n_open/n_num:5.1f}%")
        print(f"  CLOSED (exact value) ......... {n_closed:6d}  {100*n_closed/n_num:5.1f}%")
        print("\n  A high CLOSED share in one tender, compared to other tenders")
        print("  in the same category, is the over-precision signal.")

    # ---------------- C. BRANDS AND "OR EQUIVALENT" ----------------
    print("\n" + "=" * 66)
    print("C. BRAND NAMES AND THE LEGAL ESCAPE PHRASE")
    print("=" * 66)
    print(f"\n  lines containing a Latin word .......... {lines_with_brand}")
    print(f"  ...of those, with 'або еквівалент' ..... {brand_with_equiv}"
          f"  ({100*brand_with_equiv/lines_with_brand:.1f}%)"
          if lines_with_brand else "")
    print("\n  Most frequent Latin tokens (check by eye which are real brands):")
    for w, c in brand_counter.most_common(25):
        print(f"    {w:22s} {c}")
    print("\n  Add anything that is NOT a brand to NOT_BRANDS at the top of")
    print("  this script, then re-run. Two or three passes is normal.")

    # ---------------- OUTPUTS ----------------
    with open(f"{args.outdir}/doc_inspection.csv", "w", newline="",
              encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(per_file[0].keys()))
        w.writeheader()
        w.writerows(per_file)

    spath = f"{args.outdir}/sample_requirements.txt"
    with open(spath, "w", encoding="utf-8") as f:
        f.write("REAL REQUIREMENT LINES - read these yourself.\n")
        f.write("CLOSED = exact value, OPEN = threshold.\n\n")
        for fn, kind, line in samples:
            f.write(f"[{kind}] {fn}\n    {line}\n\n")

    print(f"\n  Written: {args.outdir}/doc_inspection.csv")
    print(f"  Written: {spath}")
    print("\n  >>> Open sample_requirements.txt and read it. The statistics")
    print("      above are useless until you have seen ten real lines.")


if __name__ == "__main__":
    main()
