"""
STEP 6 - Download the specification documents themselves.

Up to now we only looked at the structured record. This pulls the actual Word
files so we can see how requirements are written.

Three things make this trickier than it looks:

  1. Document URLs EXPIRE. The url in our cached tenders.jsonl is a signed link
     that is already dead. We must re-fetch the tender to get a fresh one, right
     before downloading.

  2. documents[] contains REVISIONS. The same document id appears several times
     with different dateModified. We keep only the newest of each.

  3. .p7s files are detached digital signatures, not content. Skip them.

Run:
    python 07_fetch_docs.py --max 60
    python 07_fetch_docs.py --max 200 --cpv 3021,3023

Output: data/docs/<cpv4>/<tenderID>__<filename>  plus data/docs/manifest.csv
"""

import argparse
import csv
import json
import os
import re
import time

import requests

from prozorro import get_tender, make_session, SLEEP

# Documents that plausibly contain the technical requirements.
# Ordered by how likely they are to be the real specification.
PREFERRED_TYPES = ["technicalSpecifications", "biddingDocuments",
                   "eligibilityCriteria", "evaluationCriteria"]

READABLE_EXT = {"docx", "doc", "pdf", "rtf", "odt"}


def safe_name(s, limit=80):
    s = re.sub(r"[^\w\s.\-()]", "_", str(s), flags=re.UNICODE)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:limit] or "file"


def ext_of(doc):
    title = str(doc.get("title") or "").lower()
    if "." in title:
        e = title.rsplit(".", 1)[-1].strip()
        if 1 < len(e) <= 5:
            return e
    fmt = str(doc.get("format") or "").lower()
    if "pdf" in fmt:
        return "pdf"
    if "openxmlformats" in fmt and "word" in fmt:
        return "docx"
    if "msword" in fmt:
        return "doc"
    return "bin"


def latest_versions(documents):
    """documents[] holds revisions - keep the newest entry per document id."""
    best = {}
    for d in documents:
        did = d.get("id") or d.get("title")
        prev = best.get(did)
        if prev is None or str(d.get("dateModified") or "") > str(prev.get("dateModified") or ""):
            best[did] = d
    return list(best.values())


def pick_spec_docs(tender, per_tender=2):
    """Choose the documents most likely to be the technical requirements."""
    docs = latest_versions(tender.get("documents") or [])
    chosen = []
    for want in PREFERRED_TYPES:
        for d in docs:
            if d.get("documentType") != want:
                continue
            if ext_of(d) not in READABLE_EXT:
                continue
            chosen.append(d)
            if len(chosen) >= per_tender:
                return chosen
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/tenders.jsonl")
    ap.add_argument("--scope", default="reports/selected_cpv4.json")
    ap.add_argument("--outdir", default="data/docs")
    ap.add_argument("--max", type=int, default=60, help="tenders to download from")
    ap.add_argument("--per-tender", type=int, default=2)
    ap.add_argument("--cpv", default=None,
                    help="override the scope file, e.g. 3021,3023,3433")
    args = ap.parse_args()

    if args.cpv:
        scope = {c.strip() for c in args.cpv.split(",")}
    else:
        with open(args.scope, encoding="utf-8") as f:
            scope = set(json.load(f))
    print(f"Scope: {len(scope)} categories\n")

    # Candidate tenders: in scope, goods, with at least one readable spec doc.
    candidates = []
    with open(args.inp, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            t = json.loads(line)
            cpv = t.get("_cpv")
            if not cpv or cpv[:4] not in scope:
                continue
            if t.get("mainProcurementCategory") != "goods":
                continue
            if not pick_spec_docs(t, args.per_tender):
                continue
            candidates.append((t["id"], cpv[:4], t.get("tenderID")))

    print(f"Candidate tenders in scope: {len(candidates)}")

    # Spread the sample across categories instead of taking the first N,
    # which would all come from one category.
    by_cat = {}
    for tid, cpv4, tref in candidates:
        by_cat.setdefault(cpv4, []).append((tid, cpv4, tref))
    picked, i = [], 0
    while len(picked) < args.max:
        added = False
        for cat in sorted(by_cat):
            if i < len(by_cat[cat]):
                picked.append(by_cat[cat][i])
                added = True
                if len(picked) >= args.max:
                    break
        if not added:
            break
        i += 1

    print(f"Downloading documents from {len(picked)} tenders "
          f"across {len({p[1] for p in picked})} categories\n")

    os.makedirs(args.outdir, exist_ok=True)
    session = make_session()
    manifest, ok, fail = [], 0, 0

    for n, (tid, cpv4, tref) in enumerate(picked, start=1):
        try:
            # Re-fetch: the cached URLs are expired signed links.
            fresh = get_tender(tid, session=session)
        except Exception as e:
            print(f"  tender {tid} failed: {e}")
            fail += 1
            continue

        docs = pick_spec_docs(fresh, args.per_tender)
        cat_dir = os.path.join(args.outdir, cpv4)
        os.makedirs(cat_dir, exist_ok=True)

        for d in docs:
            url = d.get("url")
            if not url:
                continue
            ext = ext_of(d)
            fname = f"{safe_name(tref or tid, 40)}__{safe_name(d.get('title'), 50)}"
            if not fname.lower().endswith("." + ext):
                fname += "." + ext
            path = os.path.join(cat_dir, fname)

            if os.path.exists(path) and os.path.getsize(path) > 0:
                continue
            try:
                r = session.get(url, timeout=120)
                r.raise_for_status()
                with open(path, "wb") as f:
                    f.write(r.content)
                ok += 1
                manifest.append({
                    "tender_id": tid, "tenderID": tref, "cpv4": cpv4,
                    "documentType": d.get("documentType"), "ext": ext,
                    "title": d.get("title"), "bytes": len(r.content),
                    "path": path,
                })
            except Exception as e:
                print(f"  doc failed ({d.get('title')}): {e}")
                fail += 1
            time.sleep(SLEEP)

        if n % 10 == 0:
            print(f"  {n}/{len(picked)} tenders | {ok} files saved")
        time.sleep(SLEEP)

    mpath = os.path.join(args.outdir, "manifest.csv")
    if manifest:
        with open(mpath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(manifest[0].keys()))
            w.writeheader()
            w.writerows(manifest)

    print(f"\nSaved {ok} files, {fail} failures.")
    print(f"Manifest: {mpath}")
    ext_count = {}
    for m in manifest:
        ext_count[m["ext"]] = ext_count.get(m["ext"], 0) + 1
    print(f"By format: {ext_count}")
    print("\nNext: python 08_inspect_docs.py")


if __name__ == "__main__":
    main()
