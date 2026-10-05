"""
CANADA - STEP 2: download the specification attachments.

Scope comes from reports/selected_unspsc4.json (written by 01). This script
re-counts usable tenders per UNSPSC-4 group, takes the TOP N groups by that
count, samples tenders inside each, and downloads their attachments.

Usable = goods (*GD) + attachment on canadabuys.canada.ca + a UNSPSC code.
Attachments on sscp2pspc.ssc-spc.gc.ca are a login wall and are skipped.

Content-Type is application/octet-stream for everything, so every file is
identified by its magic bytes after download and renamed to match. An 86 KB
HTML login page is detected and discarded.

Resumable. Re-running skips every URL already in the manifest with a file on
disk, so it is safe to stop with Ctrl-C and start again. Nothing is ever
re-downloaded.

Run:
    pip install requests
    python 02_fetch_docs.py                  # top 5 groups, 60 tenders each
    python 02_fetch_docs.py --top 5 --per-cat 0     # everything in top 5
    python 02_fetch_docs.py --include-zip --jobs 6

Writes:
    data/docs/<unspsc4>/<reference>/<file>
    reports/docs_manifest.csv      one row per attachment URL
"""

import argparse
import csv
import json
import os
import re
import sys
import threading
import time
import zipfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests

csv.field_size_limit(10_000_000)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

C = {
    "ref": "referenceNumber-numeroReference",
    "sol": "solicitationNumber-numeroSollicitation",
    "title": "title-titre-eng",
    "pub": "publicationDate-datePublication",
    "unspsc": "unspsc",
    "unspsc_desc": "unspscDescription-eng",
    "category": "procurementCategory-categorieApprovisionnement",
    "method": "procurementMethod-methodeApprovisionnement-eng",
    "att": "attachment-piecesJointes-eng",
}

GOODS = {"GD"}
GOOD_HOST = "canadabuys.canada.ca"

MANIFEST_COLS = [
    "unspsc4", "unspsc_desc", "reference", "solicitation", "pub_date",
    "method", "title", "url", "local_path", "http_status", "bytes",
    "sniffed", "error",
]


# ----------------------------------------------------------------- parsing
def categories(raw):
    """'*GD\\n*SRV' -> {'GD', 'SRV'}"""
    if not raw:
        return set()
    return {t.strip().lstrip("*").strip().upper()
            for t in re.split(r"[\r\n,;|]+", raw) if t.strip()}


def first_unspsc(raw):
    """The feed may hold several codes; take the first all-digit one."""
    if not raw:
        return None
    for t in re.split(r"[\r\n,;|\s]+", raw):
        d = re.sub(r"\D", "", t)
        if len(d) >= 4:
            return d
    return None


def attachment_urls(raw):
    """The field is comma/newline separated; keep only direct-download hosts."""
    if not raw:
        return []
    out, seen = [], set()
    for u in re.split(r"[,;|\s]+", raw.strip()):
        u = u.strip().rstrip(",;")
        if not u.startswith("http") or GOOD_HOST not in u:
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ------------------------------------------------------------- file naming
WIN_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_component(s, maxlen=90):
    s = WIN_BAD.sub("_", (s or "").strip())
    s = re.sub(r"\s+", "_", s).strip(" ._")
    return s[:maxlen] or "unnamed"


def url_filename(url, index):
    base = url.rsplit("/", 1)[-1].split("?")[0]
    base = safe_component(base, 90)
    root, ext = os.path.splitext(base)
    return f"{index:02d}_{root}{ext.lower()}"


MAGIC = (
    (b"%PDF", "pdf"),
    (b"PK\x03\x04", "zip"),
    (b"\xd0\xcf\x11\xe0", "ole"),      # legacy .doc / .xls
    (b"{\\rtf", "rtf"),
    (b"\x89PNG", "png"),
    (b"\xff\xd8\xff", "jpg"),
)


def sniff(head):
    for sig, kind in MAGIC:
        if head.startswith(sig):
            return kind
    low = head.lstrip()[:400].lower()
    if low.startswith(b"<!doctype html") or low.startswith(b"<html") \
            or b"<head" in low:
        return "html"
    return "unknown"


def zip_flavour(path):
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
    except Exception:
        return "zip"
    if any(n.startswith("word/") for n in names):
        return "docx"
    if any(n.startswith("xl/") for n in names):
        return "xlsx"
    if any(n.startswith("ppt/") for n in names):
        return "pptx"
    return "zip"


def retype(path, kind):
    """Rename so the extension matches the magic bytes. Returns the new path."""
    want = {"pdf": ".pdf", "docx": ".docx", "xlsx": ".xlsx", "pptx": ".pptx",
            "zip": ".zip", "ole": ".doc", "rtf": ".rtf", "png": ".png",
            "jpg": ".jpg"}.get(kind)
    if not want:
        return path
    root, ext = os.path.splitext(path)
    if ext.lower() == want:
        return path
    new = root + want
    try:
        if os.path.exists(new):
            os.remove(path)
        else:
            os.replace(path, new)
        return new
    except OSError:
        return path


# ----------------------------------------------------------------- pass 1
def scan(tenders_path, scope):
    """One streaming pass. Returns (counts, candidates, labels)."""
    counts = defaultdict(int)
    cand = defaultdict(list)
    labels = {}
    n = 0
    with open(tenders_path, encoding="utf-8-sig", errors="replace",
              newline="") as f:
        for row in csv.DictReader(f):
            n += 1
            if not (categories(row.get(C["category"])) & GOODS):
                continue
            att = row.get(C["att"]) or ""
            if GOOD_HOST not in att:
                continue
            u = first_unspsc(row.get(C["unspsc"]))
            if not u or len(u) < 4:
                continue
            g = u[:4]
            counts[g] += 1
            # the description field may hold several newline-separated values
            labels.setdefault(g, re.sub(
                r"\s+", " ", (row.get(C["unspsc_desc"]) or "")
                .replace("*", " ")).strip()[:48])
            if scope and g not in scope:
                continue
            urls = attachment_urls(att)
            if not urls:
                continue
            cand[g].append({
                "unspsc4": g,
                "unspsc_desc": labels.get(g, ""),
                "reference": (row.get(C["ref"]) or "").strip(),
                "solicitation": (row.get(C["sol"]) or "").strip(),
                "pub_date": (row.get(C["pub"]) or "").strip(),
                "method": (row.get(C["method"]) or "").strip(),
                "title": (row.get(C["title"]) or "").strip()[:160],
                "urls": urls,
            })
    print(f"Scanned {n:,} tender notices")
    return counts, cand, labels


# When --max-att trims a tender's attachments, the specification must not be
# the one that gets cut. Lower number = fetched first.
ATT_PRIORITY = (
    (0, r"annex|appendi|spec|sow|statement[_-]?of[_-]?work|technical|"
        r"requirement|exigence|devis|attach"),
    (1, r"rfp|rfq|itq|ital|rfsa|acan|solicit|tender|invitation"),
    (3, r"amend|modif|addend|revis"),
    (4, r"form|certif|checklist|pricing|price|questionnaire"),
)


def att_rank(url):
    low = url.rsplit("/", 1)[-1].lower()
    for rank, pat in ATT_PRIORITY:
        if re.search(pat, low):
            return rank
    return 2


def pick(rows, per_cat, order):
    rows = [r for r in rows if r["reference"]]
    rows.sort(key=lambda r: (r["pub_date"], r["reference"]),
              reverse=(order == "recent"))
    if per_cat <= 0 or len(rows) <= per_cat:
        return rows
    if order == "spread":
        step = len(rows) / float(per_cat)
        return [rows[int(i * step)] for i in range(per_cat)]
    return rows[:per_cat]


# -------------------------------------------------------------- manifest
class Manifest:
    def __init__(self, path):
        self.path = path
        self.done = {}
        self.lock = threading.Lock()
        if os.path.exists(path):
            with open(path, encoding="utf-8", newline="") as f:
                for r in csv.DictReader(f):
                    self.done[r.get("url", "")] = r
        new = not os.path.exists(path)
        self.fh = open(path, "a", encoding="utf-8", newline="")
        self.w = csv.DictWriter(self.fh, fieldnames=MANIFEST_COLS,
                                extrasaction="ignore")
        if new:
            self.w.writeheader()
            self.fh.flush()

    def already(self, url):
        r = self.done.get(url)
        if not r:
            return False
        lp = r.get("local_path") or ""
        if r.get("error"):
            return False
        return bool(lp) and os.path.exists(lp) and os.path.getsize(lp) > 0

    def add(self, row):
        with self.lock:
            self.w.writerow(row)
            self.fh.flush()
            self.done[row["url"]] = row

    def close(self):
        self.fh.close()


# -------------------------------------------------------------- download
def fetch_one(sess, meta, url, index, out_root, args, man, stats):
    folder = os.path.join(out_root, meta["unspsc4"],
                          safe_component(meta["reference"], 60))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, url_filename(url, index))

    row = dict(meta)
    row.pop("urls", None)
    row.update({"url": url, "local_path": "", "http_status": "",
                "bytes": "", "sniffed": "", "error": ""})

    for attempt in range(args.retries + 1):
        try:
            with sess.get(url, stream=True, timeout=args.timeout,
                          headers={"User-Agent": "TenderLens/0.1"}) as r:
                row["http_status"] = r.status_code
                if r.status_code != 200:
                    row["error"] = f"HTTP {r.status_code}"
                    if r.status_code in (429, 500, 502, 503, 504) \
                            and attempt < args.retries:
                        time.sleep(2 ** attempt)
                        continue
                    break
                tmp = path + ".part"
                size = 0
                head = b""
                with open(tmp, "wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        if not chunk:
                            continue
                        if not head:
                            head = chunk[:512]
                        fh.write(chunk)
                        size += len(chunk)
                        if args.max_mb and size > args.max_mb * (1 << 20):
                            row["error"] = f"over --max-mb ({args.max_mb})"
                            break
                if row["error"]:
                    os.remove(tmp)
                    break

                kind = sniff(head)
                row["bytes"] = size
                if kind == "html":
                    os.remove(tmp)
                    row["sniffed"] = "html"
                    row["error"] = "login/HTML page, not a document"
                    break
                os.replace(tmp, path)
                if kind == "zip":
                    kind = zip_flavour(path)
                row["sniffed"] = kind
                row["local_path"] = retype(path, kind)
                break
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {e}"[:200]
            if attempt < args.retries:
                time.sleep(2 ** attempt)
                continue
            break

    man.add(row)
    with stats["lock"]:
        stats["n"] += 1
        if row["local_path"]:
            stats["ok"] += 1
            stats["bytes"] += int(row["bytes"] or 0)
            stats["kinds"][row["sniffed"]] += 1
        else:
            stats["fail"] += 1
        if stats["n"] % 25 == 0:
            print(f"  {stats['n']:5d}/{stats['total']}  ok={stats['ok']}  "
                  f"fail={stats['fail']}  {stats['bytes']/1e6:.0f} MB")
    if args.sleep:
        time.sleep(args.sleep)


# ------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tenders", default="data/tenders.csv")
    ap.add_argument("--scope", default="reports/selected_unspsc4.json")
    ap.add_argument("--out", default="data/docs")
    ap.add_argument("--manifest", default="reports/docs_manifest.csv")
    ap.add_argument("--top", type=int, default=5,
                    help="how many UNSPSC-4 groups, ranked by usable count")
    ap.add_argument("--per-cat", type=int, default=60,
                    help="tenders per group; 0 = all of them")
    ap.add_argument("--max-att", type=int, default=0,
                    help="attachments per tender; 0 = all of them")
    ap.add_argument("--order", choices=["recent", "oldest", "spread"],
                    default="recent")
    ap.add_argument("--ext", default="pdf,docx,doc,rtf,xlsx,xls",
                    help="URL extensions to download (comma separated). "
                         "xlsx matters: requirement matrices live in "
                         "spreadsheets and 03 reads them.")
    ap.add_argument("--include-zip", action="store_true",
                    help="also download .zip appendix bundles")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to pause after each download, per worker")
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--max-mb", type=int, default=80,
                    help="skip any single file larger than this; 0 = no cap")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.tenders):
        sys.exit(f"missing {args.tenders} - run 01_profile_tenders.py first")

    scope = None
    if os.path.exists(args.scope):
        with open(args.scope, encoding="utf-8") as f:
            scope = set(json.load(f))
        print(f"Scope: {len(scope)} UNSPSC-4 groups from {args.scope}")
    else:
        print(f"WARNING: {args.scope} not found - ranking over all groups")

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(os.path.dirname(args.manifest) or ".", exist_ok=True)

    counts, cand, labels = scan(args.tenders, scope)

    ranked = sorted(((counts[g], g) for g in (scope or counts)),
                    reverse=True)[:args.top]
    print(f"\nTop {len(ranked)} groups by usable count:\n")
    for v, g in ranked:
        print(f"  {g}  {v:5,} usable   {labels.get(g, '')}")

    # 03 needs these to scale a sampled rate up to the whole category.
    counts_path = os.path.join(os.path.dirname(args.manifest) or ".",
                               "category_counts.json")
    with open(counts_path, "w", encoding="utf-8") as f:
        json.dump({"usable": {g: counts[g] for g in (scope or counts)},
                   "labels": {g: labels.get(g, "") for g in (scope or counts)},
                   "sampled": {g: 0 for _v, g in ranked}}, f, indent=2)

    exts = {("." + e.strip().lstrip(".").lower())
            for e in args.ext.split(",") if e.strip()}
    if args.include_zip:
        exts.add(".zip")

    jobs = []
    sampled = {}
    print()
    for v, g in ranked:
        chosen = pick(cand.get(g, []), args.per_cat, args.order)
        sampled[g] = len(chosen)
        n_urls = 0
        for meta in chosen:
            urls = [u for u in meta["urls"]
                    if os.path.splitext(u.split("?")[0])[1].lower() in exts]
            if not urls:
                urls = meta["urls"][:1]   # unknown extension: try the first
            urls.sort(key=att_rank)       # spec-looking files first
            if args.max_att:
                urls = urls[:args.max_att]
            for i, u in enumerate(urls, 1):
                n_urls += 1
                jobs.append((meta, u, i))
        print(f"  {g}: {len(chosen):4d} tenders selected, {n_urls:5d} attachments")

    with open(counts_path, "r+", encoding="utf-8") as f:
        d = json.load(f)
        d["sampled"] = sampled
        f.seek(0), f.truncate()
        json.dump(d, f, indent=2)

    man = Manifest(args.manifest)
    todo = [(m, u, i) for (m, u, i) in jobs if not man.already(u)]
    print(f"\n{len(jobs):,} attachments in scope | "
          f"{len(jobs) - len(todo):,} already on disk | {len(todo):,} to fetch")

    if args.dry_run:
        man.close()
        print("--dry-run: nothing downloaded")
        return

    stats = {"n": 0, "ok": 0, "fail": 0, "bytes": 0, "total": len(todo),
             "kinds": defaultdict(int), "lock": threading.Lock()}

    sess = requests.Session()
    t0 = time.time()
    try:
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            for meta, url, idx in todo:
                pool.submit(fetch_one, sess, meta, url, idx, args.out,
                            args, man, stats)
    except KeyboardInterrupt:
        print("\ninterrupted - progress is in the manifest, re-run to resume")
    finally:
        man.close()

    print(f"\nDownloaded {stats['ok']:,} files "
          f"({stats['bytes']/1e6:.0f} MB) in {time.time()-t0:.0f}s | "
          f"{stats['fail']:,} failed")
    if stats["kinds"]:
        print("\n  file types (by magic bytes):")
        for k, c in sorted(stats["kinds"].items(), key=lambda kv: -kv[1]):
            print(f"    {c:6,}  {k}")
    print(f"\n  files    -> {args.out}/<unspsc4>/<reference>/")
    print(f"  manifest -> {args.manifest}")
    print("\nNext: python 03_inspect_specs.py")


if __name__ == "__main__":
    main()
