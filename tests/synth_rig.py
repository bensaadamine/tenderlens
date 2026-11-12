"""Synthetic rigged tenders: does the system catch a brand lock-in?

    python tenderlens.py eval synth              # build the copies, then run them
    python tenderlens.py eval synth --build      # only write the copies
    python tenderlens.py eval synth --run        # only run + report (add --online on Windows)
    python tenderlens.py eval synth --review     # list the bases' "equivalent" lines to check by hand

BASE TENDERS: real 2510 tenders now in the knowledge base (closed, with a
(--bases no-clause, default) no standard "Equivalent Products" clause, and a
detected requirement section). Each gets seven copies:
  A  one brand + model, no "or equivalent"                      expect watch
  B  two brands, no "or equivalent"                              expect alert
  C  one brand, and the contract awarded to that brand's maker   expect alert
  D  one brand WITH "or equivalent"                              expect close
  E  the brand only inside a bidder's question (amendment Q&A)   expect close
  F  untouched copy                                              expect close
  G  one brand added by an amendment's change clause            expect watch
One base tender keeps one wording style and one brand across A-E, so the
copies differ only in the rigging.

INJECTION VARIES ON PURPOSE: 7 wording styles (English and French), brands
the offline brand cache already confirms (Honda, Michelin, Palfinger) and
brands only an --online run can confirm (Cummins, Allison, ...).

NOTHING TOUCHES REAL DATA: copies live in reports/text_synth/2510/. Each
run writes its own folder, reports/synth/2510/runs/<stamp>/ (results.csv,
summary.txt, the queue, settings.json: options, models that answered, and
fingerprints of the manifest and copies); a run never overwrites another.
Each copy is compared with the real knowledge base (reports/kb/2510) minus
its own original, as a tender of a new batch would be.

ONE INVESTIGATION PER COPY: what happened to each injected brand is read from
the case itself (case["m2"]: every name M2 raised, its result, who settled
it), so the route and the brand outcome come from the same M2 run.

EXPECTED LABELS: whether a base already accepts equivalents for every brand
(then a brand added there is legal: expect close) is decided by the
detector's own clause patterns - a shared blind spot. --review lists every
"equivalent" line of the bases in reports/synth/2510/equivalent_review.csv;
a base marked there by hand (allows = yes / no) uses that verdict instead.

HONEST LIMIT: the same team wrote the injections and the detector. Two
styles are included BECAUSE the rules are known to miss them - a brand with
no model number, and a "Make and model:" field - so they measure blind
spots. The score is an upper bound until someone writes injections blind.
"""


import argparse
import collections
import csv
import hashlib
import io
import json
import os
import random
import re
import sys
import time

from agents import m1_screener
from agents import m2_spec_auditor
from agents import m4_case_builder
from tools import brands
from tools import documents
from tools import llm
from tools import names
from tools import wikidata
from tools.report import write_outputs
from pipeline import one_tender
from pipeline.settings import Settings

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


from prepare import census
from pipeline import stream  # noqa: E402

GROUP = "2510"
SYN_TEXT = "reports/text_synth"
OUT = os.path.join("reports", "synth", GROUP)
MANIFEST = os.path.join(OUT, "manifest.csv")
REVIEW = os.path.join(OUT, "equivalent_review.csv")
RE_ANY_EQUIV = re.compile(r"[ée]quivalen", re.I)   # broad on purpose
# Federal boilerplate about climate pledges, never about products: left out
# of the review list so a person reads only the lines that can matter.
RE_NOT_PRODUCTS = re.compile(r"net.zero|race to zero|science based targets",
                             re.I)

# name, model, part (en), part (fr), maker's company name, and whether the
# brand cache confirmed it when the set was written (unused: the run reads
# the cache itself - section 5)
BRANDS = [
    ("Honda", "GX390", "engine", "moteur", "Honda Canada Inc.", True),
    ("Cummins", "ISB6.7", "engine", "moteur", "Cummins Eastern Canada LP", False),
    ("Michelin", "XZE2", "tire", "pneu", "Michelin North America (Canada) Inc.", True),
    ("Allison", "2500RDS", "transmission", "transmission", "Allison Transmission Inc.", False),
    ("Palfinger", "PK11001", "crane", "grue", "Palfinger Inc.", True),
    ("Hiab", "X-HiPro 192", "crane", "grue", "Hiab Canada Inc.", False),
    ("Webasto", "TSL17", "heater", "chauffage d'appoint", "Webasto Thermo & Comfort", False),
    ("Knapheide", "KSS-96", "service body", "carrosserie de service", "Knapheide Manufacturing Company", False),
    ("Jerr-Dan", "MPL40", "wrecker body", "plateau de remorquage", "Jerr-Dan Corporation", False),
    ("Fontaine", "6000", "fifth wheel", "sellette d'attelage", "Fontaine Fifth Wheel", False),
]

STYLES = ["prose", "table", "field", "trademark", "oem", "no_model", "french"]
VARIANTS = {"A": "watch", "B": "alert", "C": "alert",
            "D": "close", "E": "close", "F": "close", "G": "watch"}
POSITIVE = {"A", "B", "C", "G"}


def is_rigged(r):
    """A copy is a real lock-in only if its base tender does not already
    accept equivalents for every brand (measured 2026-10-17: 27 of the 40
    bases carry Canada's standard "Equivalent Products" clause, so a brand
    added there is legal and the right outcome is close)."""
    return r["variant"] in POSITIVE and r.get("base_allows_equivalents") != "True"


def phrase(style, b, equiv=False):
    """Lines that name brand b in the given style."""
    name, model, part, part_fr = b[0], b[1], b[2], b[3]
    oe = " or equivalent" if equiv else ""
    if style == "prose":
        return [f"The {part} must be a {name} {model}{oe}."]
    if style == "table":
        return [part.capitalize(), f"{name} {model}{oe}"]
    if style == "field":
        return [f"{part.capitalize()} - make and model: {name} {model}{oe}"]
    if style == "trademark":
        return [f"The {part} shall be a {name}® {model}{oe}."]
    if style == "oem":
        return [f"Only a genuine {name} {model} {part}{oe} will be accepted."]
    if style == "no_model":
        return [f"The {part} must be manufactured by {name}{oe}."]
    if style == "french":
        oe = " ou équivalent" if equiv else ""
        return [f"Le {part_fr} doit être un {name} {model}{oe}."]
    raise ValueError(style)


# ----------------------------------------------------------------- build
def allows_equivalents(docs):
    """The tender already accepts an equivalent for every brand it names."""
    return any(brands.RE_EQUIV_CLAUSE.search(d["text"])
               or brands.RE_DOC_CLAUSE.search(d["text"]) for d in docs)


def pick_bases(n, seed, bases="no-clause"):
    kb = stream.KB(GROUP)
    state = kb.state()
    pool = sorted(ref for ref, ev in state.items()
                  if ev["pile"] == "base" and ev["status"] == "close"
                  and int(ev["observations"] or 0) > 0)
    random.Random(seed).shuffle(pool)
    out = []
    for ref in pool:
        docs = documents.tender_documents(ref)
        if not docs:
            continue
        # A brand added to a tender that already accepts equivalents for
        # every brand is legal - not a rigged tender. Measured 2026-10-17:
        # 27 of the first 40 bases were like that.
        if bases == "no-clause" and allows_equivalents(docs):
            continue
        body_docs = [d for d in docs if "amend" not in d["document"].lower()]
        primary = max(body_docs or docs, key=lambda d: len(d["text"]))
        lines = primary["text"].split("\n")
        mask, mode = census.scope_mask(lines)
        if mode != "section":
            continue
        cand = [i for i, m in enumerate(mask)
                if m and re.search(r"\d", lines[i]) and len(lines[i].strip()) >= 3]
        if len(cand) < 3:
            continue
        out.append((ref, docs, primary, lines, cand[len(cand) * 2 // 5]))
        if len(out) == n:
            break
    return out


def build(n, seed, bases="no-clause"):
    gdir = os.path.join(SYN_TEXT, GROUP)
    if os.path.isdir(gdir) and os.listdir(gdir):
        sys.exit(f"{gdir} is not empty - move it away to rebuild")
    os.makedirs(gdir, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    bases = pick_bases(n, seed, bases)
    print(f"{len(bases)} base tenders")
    rows = []
    for k, (ref, docs, primary, lines, at) in enumerate(bases):
        style = STYLES[k % len(STYLES)]
        b1 = BRANDS[k % len(BRANDS)]
        b2 = BRANDS[(k + 3) % len(BRANDS)]
        blanket = any(brands.RE_DOC_CLAUSE.search(d["text"]) for d in docs)
        for v in VARIANTS:
            sref = f"synth-{v}-{ref}"
            inject, extra = [], None
            if v in ("A", "C"):
                inject = phrase(style, b1)
            elif v == "B":
                inject = phrase(style, b1) + phrase(style, b2)
            elif v == "D":
                inject = phrase(style, b1, equiv=True)
            elif v == "E":
                q = phrase("prose", b1)[0].replace("The ", "the ", 1)
                extra = ("AMENDMENT 001\n\nQUESTIONS AND ANSWERS\n\n"
                         f"Question 3: We propose that {q[:-1]}. "
                         "Is this acceptable?\n"
                         f"Answer: The {b1[2]} must meet the requirements of "
                         "the purchase description.\n")
            elif v == "G":
                extra = ("SOLICITATION AMENDMENT\nAmendment No. 002\n\n"
                         "This amendment is raised to modify the Statement of "
                         "Requirement.\n\n"
                         "At Annex A, the requirement for the " + b1[2] +
                         " is deleted and replaced with the following:\n"
                         + "\n".join(phrase(style, b1)) + "\n\n"
                         "All other terms and conditions remain unchanged.\n")
            for d in docs:
                text = d["text"]
                if d is primary and inject:
                    new = lines[:at + 1] + inject + lines[at + 1:]
                    text = "\n".join(new)
                with io.open(os.path.join(gdir, f"{sref}__{d['document']}.txt"),
                             "w", encoding="utf-8") as f:
                    f.write(text)
            if extra:
                tag = "amendment_qa" if v == "E" else "amendment_002"
                with io.open(os.path.join(gdir, f"{sref}__99_synth_{tag}.txt"),
                             "w", encoding="utf-8") as f:
                    f.write(extra)
            added = ([b1] if v in ("A", "C", "D", "E", "G")
                     else [b1, b2] if v == "B" else [])
            rows.append({
                "sref": sref, "base": ref, "variant": v,
                "expected": VARIANTS[v], "style": style,
                "lang": "fr" if style == "french" else "en",
                "brands": "|".join(b[0] for b in added),
                "models": "|".join(b[1] for b in added),
                "winner": b1[4] if v == "C" else "",
                "document": primary["document"], "line_no": at + 2,
                "injected": " / ".join(inject) if inject else (extra or "").replace("\n", " ")[:200],
                "base_blanket_clause": blanket,
            })
    with io.open(MANIFEST, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} copies -> {gdir}\nmanifest -> {MANIFEST}")


# ------------------------------------------------------------------- run
OUTCOME = {"OK": "or_equivalent_nearby", "OK_BLANKET_CLAUSE": "blanket_clause",
           "OK_MAKE_LIST": "make_list",
           "OK_EQUIV_PRODUCTS_CLAUSE": "equiv_products_clause",
           "AGENT_A": "agent_a_not_a_brand", "AGENT_B": "agent_b_bidder",
           "AGENT_C": "agent_c_buyer_equipment",
           "AGENT_D": "agent_d_list_of_brands",
           "AGENT_E": "agent_e_equivalent_accepted"}


def brand_outcome(checked, brand):
    """Where M2 filed this injected brand, from its list of names (the
    case's m2.names): found / cleared_<why> / brand_not_confirmed /
    not_picked_up. A name counts when it begins with the brand's words
    (tools/names.py): "Honda GX390" is Honda."""
    rows = [n for n in checked if names.begins_with(n["name"], brand)]
    by = {n["result"]: n for n in rows}
    if "finding" in by:
        return "found"
    if "cleared" in by:
        st = by["cleared"].get("status", "")
        return "cleared_" + OUTCOME.get(st, st)
    if "open" in by:
        return "brand_not_confirmed"
    return "not_picked_up"


def review_equivalents():
    """Write every line of the bases containing "equivalent" (broad, not
    the detector's pattern), one row per base, for a person to mark
    allows = yes (a clause accepts equivalents for EVERY brand) or no.
    Verdicts already written (allows, why, reviewer) are kept."""
    marks = {}
    if os.path.exists(REVIEW):
        with io.open(REVIEW, encoding="utf-8", newline="") as f:
            marks = {r["base"]: r for r in csv.DictReader(f)}
    with io.open(MANIFEST, encoding="utf-8", newline="") as f:
        bases = sorted({m["base"] for m in csv.DictReader(f)})
    rows = []
    for ref in bases:
        hits = []
        for d in documents.tender_documents(ref, "reports/text"):
            for n, line in enumerate(d["text"].split("\n"), 1):
                if RE_ANY_EQUIV.search(line) and \
                        not RE_NOT_PRODUCTS.search(line):
                    hits.append(f"{d['document']} l.{n}: {line.strip()[:160]}")
        if hits:
            rows.append({"base": ref, "lines": len(hits),
                         "detector_says": allows_equivalents(
                             documents.tender_documents(ref, "reports/text")),
                         "first_lines": " || ".join(hits[:4]),
                         **{k: marks.get(ref, {}).get(k, "")
                            for k in ("allows", "why", "reviewer")}})
    with io.open(REVIEW, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["base", "lines", "detector_says",
                                          "first_lines", "allows", "why",
                                          "reviewer"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} of {len(bases)} bases mention 'equivalent' -> "
          f"{REVIEW}\nfill 'allows' with yes / no (all the lines: "
          f"reports/text/2510/<base>__*.txt); the next run uses it")


def reviewed_allows():
    """{base: True/False} for the bases a person marked in REVIEW."""
    if not os.path.exists(REVIEW):
        return {}
    with io.open(REVIEW, encoding="utf-8", newline="") as f:
        return {r["base"]: r["allows"].strip().lower() == "yes"
                for r in csv.DictReader(f)
                if r["allows"].strip().lower() in ("yes", "no")}


def _fingerprint(paths):
    h = hashlib.sha1()
    for p in sorted(paths):
        with open(p, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def _cache_status(cache, brand):
    """What the brand cache says about this brand when the run starts."""
    v = (cache.get(brand.lower()) or {}).get("brand")
    return {True: "confirmed", False: "not a brand"}.get(v, "unknown")


def run(a):
    if not os.path.exists(MANIFEST):
        sys.exit("no manifest - run with --build first")
    with io.open(MANIFEST, encoding="utf-8", newline="") as f:
        man = list(csv.DictReader(f))

    # Point every agent at the synthetic folder.
    documents.TEXT_DIR = SYN_TEXT
    m1_screener.TEXT_DIR = SYN_TEXT

    notices, wstats, _, graph = one_tender.load_shared(level2_on=False)
    kb = stream.KB(GROUP)
    state = kb.state()
    amap = stream.load_map()
    base = stream.base_rows(kb, state, amap)
    print(f"real knowledge base: {len({r['reference'] for r in base})} tenders as peers")

    if graph is not None:
        graph = dict(graph)
        graph["by_ref"] = dict(graph["by_ref"])
    # Does the REAL base tender already accept equivalents for every brand?
    # Read from the original text, whatever the current flags say, so the
    # expected outcome does not move with the rule being tested.
    allows = {}
    for ref in {m["base"] for m in man}:
        docs = documents.tender_documents(ref, "reports/text")
        allows[ref] = allows_equivalents(docs)
    reviewed = reviewed_allows()        # a person's verdict wins
    allows.update(reviewed)
    cache = wikidata.load_cache()       # what the brand cache says TODAY
    log_start = len(llm.usage_rows())

    if a.variants:
        keep = set(a.variants.upper().split(","))
        man = [m for m in man if m["variant"] in keep]
        print(f"only copy types {sorted(keep)}: {len(man)} copies")

    results, cases = [], []
    t0 = time.time()
    for i, m in enumerate(man, 1):
        sref, ref = m["sref"], m["base"]
        m = dict(m)
        m["base_allows_equivalents"] = str(allows[ref])
        if m["variant"] in POSITIVE and allows[ref]:
            m["expected"] = "close"
        row = dict(notices[ref])
        row[m1_screener.C_REF] = sref
        # Blank the solicitation number: otherwise M3 would find the
        # ORIGINAL tender's award for every copy.
        row["solicitationNumber-numeroSollicitation"] = ""
        # A rigged copy stands for an AWARDED contract. Some bases were
        # cancelled or are still open, and since 2026-10-22 such tenders are
        # "not an audit case" - so the copy must not inherit that status.
        if one_tender.no_contract_reason(row):
            row[m1_screener.C_STATUS] = "Expired"
            if one_tender.no_contract_reason(row):     # an RFI / RFSA base
                row[m1_screener.C_NOTICE] = "Request for Proposal"
        notices[sref] = row
        if m["variant"] == "C" and graph is not None:
            graph["by_ref"][sref] = [{
                "supplier": m["winner"].upper(), "supplier_name": m["winner"],
                "buyer": "SYNTHETIC", "buyer_name": "(synthetic test buyer)",
                "value": "", "date": "", "group": GROUP}]
        _, raw = stream.extract_tender(sref, row, GROUP, f"synth-{i:04d}")
        mine = stream.comparable(raw, amap)
        peers = [r for r in base if r["reference"] not in (ref, sref)] + mine
        # a copy without a model keeps the rules' result
        case = one_tender.run_one(sref, (notices, wstats, peers, graph),
                                  Settings(online=a.online, agent=a.agent,
                                           on_outage="keep"))
        # what happened to each injected brand: from THIS run's M2
        checked = case["m2"]["names"]
        added = [b for b in m["brands"].split("|") if b]
        found = [n["name"] for n in checked if n["result"] == "finding"]
        res = dict(m)
        res.update({"route": case["route"], "score": case["score"],
                    "m1_route": case.get("m1_route"),
                    "outcomes": "|".join(brand_outcome(checked, b)
                                         for b in added),
                    "brand_cache": "|".join(_cache_status(cache, b)
                                            for b in added),
                    "agent": (m2_spec_auditor.agent_status(
                        case["m2"]["agent"], case["trace"].get("M2"))
                        if a.agent else "not asked"),
                    "l1_findings": len(found),
                    "l1_brands": "|".join(found),
                    "correct": case["route"] == m["expected"]})
        results.append(res)
        case["synthetic"] = m["variant"]
        cases.append(case)
        if i % 30 == 0:
            print(f"  {i}/{len(man)} ({time.time() - t0:.0f}s)")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = os.path.join(OUT, "runs", stamp)
    os.makedirs(run_dir, exist_ok=True)
    copies = [os.path.join(SYN_TEXT, GROUP, f)
              for f in os.listdir(os.path.join(SYN_TEXT, GROUP))]
    with io.open(os.path.join(run_dir, "settings.json"), "w",
                 encoding="utf-8") as f:
        json.dump({"stamp": stamp, "argv": sys.argv[1:],
                   "online": a.online, "agent": a.agent,
                   "variants": a.variants or "all",
                   "agent answers by model": llm.answers_by_model(
                       llm.usage_rows()[log_start:]),
                   "manifest sha1": _fingerprint([MANIFEST]),
                   "copies sha1": _fingerprint(copies),
                   "bases reviewed by hand": len(reviewed)}, f, indent=1)
    cols = list(results[0])
    with io.open(os.path.join(run_dir, "results.csv"), "w", encoding="utf-8",
                 newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(results)
    meta = {"scope": "synthetic rigged tenders, 2510",
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "tenders": len(cases), "awards": graph is not None,
            "agent": a.agent, "weights": m4_case_builder.WEIGHTS,
            "alert_at": m4_case_builder.ALERT, "watch_at": m4_case_builder.WATCH}
    write_outputs(cases, meta, os.path.join(run_dir, "queue"))
    report(results, state, a.online, run_dir, len(reviewed))


def report(results, state, online, run_dir, n_reviewed=0):
    L = []
    say = L.append
    say(f"SYNTHETIC RIGGED TENDERS - 2510 - {time.strftime('%Y-%m-%d %H:%M')}"
        f" - brand check {'online' if online else 'OFFLINE (cache only)'}")
    say(f"{len({r['base'] for r in results})} base tenders x {len(VARIANTS)} copies\n")

    nb = len({r["base"] for r in results})
    nclause = len({r["base"] for r in results
                   if r.get("base_allows_equivalents") == "True"})
    say(f"Base tenders already accepting equivalents for every brand "
        f"(standard clause): {nclause}/{nb}. Brands added there are legal, "
        f"so those copies are expected to CLOSE. Decided by hand for "
        f"{n_reviewed} base(s) (--review), by the detector's clause "
        f"patterns for the others.")
    ag = collections.Counter(r["agent"].split(":")[0] for r in results)
    if set(ag) != {"not asked"}:
        say("M2 agent, per copy: " + ", ".join(f"{k} {v}" for k, v in
                                               sorted(ag.items()))
            + " - a copy it did not run on keeps the rules' result")
    say("")
    say("1. Routing by copy type")
    say(f"   {'copy':5} {'base':12} {'expected':9} {'alert':>6} {'watch':>6} "
        f"{'close':>6}  as expected")
    for v in VARIANTS:
        for allow in ("False", "True"):
            rs = [r for r in results if r["variant"] == v
                  and r.get("base_allows_equivalents") == allow]
            if not rs:
                continue
            c = collections.Counter(r["route"] for r in rs)
            ok = sum(1 for r in rs if r["correct"])
            say(f"   {v:5} {'clause' if allow == 'True' else 'no clause':12} "
                f"{rs[0]['expected']:9} {c['alert']:6} {c['watch']:6} "
                f"{c['close']:6}  {ok}/{len(rs)}")

    pos = [r for r in results if is_rigged(r)]
    caught = [r for r in pos if "found" in r["outcomes"].split("|")]
    say(f"\n2. Rigged copies (A+B+C+G, base without the clause): brand found in {len(caught)}/{len(pos)}"
        f" ({len(caught) / max(len(pos), 1):.0%}); alert fired on "
        f"{sum(1 for r in pos if r['route'] == 'alert')}")

    say("\n3. What happened to each injected brand (A+B+C+G)")
    oc = collections.Counter(o for r in pos for o in r["outcomes"].split("|"))
    for k, n in oc.most_common():
        say(f"   {k:32} {n}")

    say("\n4. Brand found, by wording style (A+B+C+G, per injected brand)")
    by = collections.defaultdict(lambda: [0, 0])
    for r in pos:
        for o in r["outcomes"].split("|"):
            by[r["style"]][1] += 1
            by[r["style"]][0] += o == "found"
    for s in STYLES:
        f, t = by[s]
        say(f"   {s:10} {f}/{t}")

    say("\n5. Brand found, by what the brand cache says about it (read at "
        "the start of this run)")
    by = collections.defaultdict(lambda: [0, 0])
    for r in pos:
        for o, c in zip(r["outcomes"].split("|"), r["brand_cache"].split("|")):
            by[c][1] += 1
            by[c][0] += o == "found"
    for c in ("confirmed", "unknown", "not a brand"):
        f, t = by[c]
        if t:
            say(f"   {c:12} {f}/{t}")

    # A finding on a brand the TEST added is a test result; a finding on a
    # brand already written in the real base tender is a REAL finding (or a
    # real false alarm) and is audited by hand, not scored as the test's.
    def injected(r, b):
        return any(names.begins_with(b, x)
                   for x in r["brands"].split("|") if x)
    neg = [r for r in results if not is_rigged(r)]
    added = [r for r in neg if r["variant"] != "F"]
    wrong = [r for r in added if any(injected(r, b) for b in
                                     r.get("l1_brands", "").split("|") if b)]
    plain = [r for r in neg if r["variant"] == "F"]
    plain_wrong = [r for r in plain if r["route"] != r["expected"]]
    say("\n6. Copies that should close - the false-alarm guards")
    for v, what in (("D", "brand added WITH 'or equivalent'"),
                    ("E", "brand only in a bidder's question")):
        rs = [r for r in added if r["variant"] == v]
        say(f"   {v}  {what:36} {sum(r in wrong for r in rs)}/{len(rs)} "
            f"flag the added brand")
    legal = [r for r in added if r["variant"] in POSITIVE]
    if legal:
        say(f"   A+B+C+G on a base whose clause accepts equivalents "
            f"{sum(r in wrong for r in legal)}/{len(legal)} flag it")
    say(f"   F  untouched copy (judged on its route)  "
        f"{len(plain_wrong)}/{len(plain)} not closed - the brands it finds "
        f"are real text, listed in 6b")
    for r in (wrong + plain_wrong)[:10]:
        say(f"   {r['sref']}  {r['route']}  outcomes={r['outcomes']}")
    real = collections.defaultdict(set)
    for r in results:
        for b in r.get("l1_brands", "").split("|"):
            if b and not injected(r, b):
                real[r["base"]].add(b)
    say(f"\n6b. Brands found in the REAL text of base tenders (not added by "
        f"the test) - audit by hand: {sum(len(v) for v in real.values())} "
        f"in {len(real)} tender(s)")
    for ref, bs in sorted(real.items()):
        say(f"   {ref}: {', '.join(sorted(bs))}")

    # Ranking. Real tenders count as clean (unknown, almost surely mostly
    # clean). Ties broken both ways, because most of the queue ties.
    order = {"alert": 0, "watch": 1, "close": 2}
    real = []
    for ev in state.values():
        route = ev["status"] if ev["status"] in order else (
            "watch" if ev["pile"] == "quarantine" else "close")
        real.append((order[route], -float(ev["score"] or 0), False))
    syn = [(order[r["route"]], -float(r["score"]), is_rigged(r))
           for r in results]
    pool = real + syn
    best = sorted(pool, key=lambda x: (x[0], x[1], not x[2]))[:50]
    worst = sorted(pool, key=lambda x: (x[0], x[1], x[2]))[:50]
    flagged = [x for x in pool if x[0] < 2]
    say(f"\n7. Synthetic ranking: {len(real)} real tenders (unreviewed, "
        f"counted as clean) + {len(syn)} copies ({len(pos)} rigged)")
    say(f"   rigged copies in the top 50: {sum(x[2] for x in worst)} (ties "
        f"broken against us) to {sum(x[2] for x in best)} (ties for us) - "
        f"not the real precision@50 of decision 8")
    say(f"   of everything flagged (alert+watch): "
        f"{sum(x[2] for x in flagged)}/{len(flagged)} are rigged copies")
    say(f"   Caveat: {len(pos)} rigged copies among ~{len(pool)} is far more "
        "rigging than real life; read the per-copy recall above first.")

    text = "\n".join(L)
    with io.open(os.path.join(run_dir, "summary.txt"), "w",
                 encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--agent", action="store_true",
                    help="let the M2 agent letter brand candidates a-f "
                         "(LLM, Windows)")
    ap.add_argument("--variants", default="",
                    help="run only these copy types, e.g. A,D,F - to limit "
                         "tokens with --agent")
    ap.add_argument("--review", action="store_true",
                    help="list the bases' 'equivalent' lines for a person to "
                         "mark (reports/synth/2510/equivalent_review.csv)")
    ap.add_argument("--bases", choices=["no-clause", "any"],
                    default="no-clause",
                    help="--build: pick only base tenders that do NOT already "
                         "accept equivalents for every brand (default); "
                         "'any' = the earlier selection")
    a = ap.parse_args()
    if a.review:
        review_equivalents()
        return
    if a.build or not a.run:
        build(a.n, a.seed, a.bases)
    if a.run or not a.build:
        run(a)


if __name__ == "__main__":
    main()
