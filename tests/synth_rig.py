"""Synthetic rigged tenders: does the system catch a brand lock-in?

    python tests/synth_rig.py              # build the copies, then run them
    python tests/synth_rig.py --build      # only write the copies
    python tests/synth_rig.py --run        # only run + report (add --online on Windows)

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

NOTHING TOUCHES REAL DATA: copies live in reports/text_synth/2510/, results
in reports/synth/2510/. Each copy is compared with the real knowledge base
(reports/kb/2510) minus its own original, as a tender of a new batch would be.

HONEST LIMIT: the same team wrote the injections and the detector. Two
styles are included BECAUSE the rules are known to miss them - a brand with
no model number, and a "Make and model:" field - so they measure blind
spots. The score is an upper bound until someone writes injections blind.
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
import collections
import csv
import io
import json
import os
import random
import re
import sys
import time

import m1_screener
import m4_case_builder
import spec_tools
import tenderlens
import importlib.util

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


census = _load("census", _os.path.join(_ROOT, "prepare", "04_census.py"))
stream = _load("stream", _os.path.join(_ROOT, "tenderlens_stream.py"))

GROUP = "2510"
SYN_TEXT = "reports/text_synth"
OUT = os.path.join("reports", "synth", GROUP)
MANIFEST = os.path.join(OUT, "manifest.csv")

# name, model, part (en), part (fr), maker's company name, in offline cache
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
    return any(spec_tools.RE_EQUIV_CLAUSE.search(d["text"])
               or spec_tools.RE_DOC_CLAUSE.search(d["text"]) for d in docs)


def pick_bases(n, seed, bases="no-clause"):
    kb = stream.KB(GROUP)
    state = kb.state()
    pool = sorted(ref for ref, ev in state.items()
                  if ev["pile"] == "base" and ev["status"] == "close"
                  and int(ev["observations"] or 0) > 0)
    random.Random(seed).shuffle(pool)
    out = []
    for ref in pool:
        docs = spec_tools.tender_documents(ref)
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
        blanket = any(spec_tools.RE_DOC_CLAUSE.search(d["text"]) for d in docs)
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
            brands = ([b1] if v in ("A", "C", "D", "E", "G")
                      else [b1, b2] if v == "B" else [])
            rows.append({
                "sref": sref, "base": ref, "variant": v,
                "expected": VARIANTS[v], "style": style,
                "lang": "fr" if style == "french" else "en",
                "brands": "|".join(b[0] for b in brands),
                "models": "|".join(b[1] for b in brands),
                "in_cache": "|".join(str(b[5]) for b in brands),
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
def brand_outcome(l1, brand):
    """Where the brand checker filed this injected brand."""
    key = brand.lower()
    def hit(rows):
        return next((r for r in rows if r["brand"].lower().split()[0] == key
                     or r["brand"].lower().startswith(key)), None)
    if hit(l1.get("findings", [])):
        return "found"
    r = hit(l1.get("ok", []))
    if r:
        return "cleared_" + {"OK": "or_equivalent_nearby",
                             "OK_BLANKET_CLAUSE": "blanket_clause",
                             "OK_LLM_CLEARED": "llm",
                             "OK_MAKE_LIST": "make_list",
                             "OK_EQUIV_PRODUCTS_CLAUSE": "equiv_products_clause"}.get(r["status"], r["status"])
    if hit(l1.get("unresolved", [])):
        return "brand_not_confirmed"
    return "not_picked_up"


def run(a):
    if not os.path.exists(MANIFEST):
        sys.exit("no manifest - run with --build first")
    with io.open(MANIFEST, encoding="utf-8", newline="") as f:
        man = list(csv.DictReader(f))

    # Point every agent at the synthetic folder.
    spec_tools.TEXT_DIR = SYN_TEXT
    m1_screener.TEXT_DIR = SYN_TEXT

    notices, wstats, _, graph = tenderlens.load_shared()
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
        docs = spec_tools.tender_documents(ref, "reports/text")
        allows[ref] = allows_equivalents(docs)

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
        notices[sref] = row
        if m["variant"] == "C" and graph is not None:
            graph["by_ref"][sref] = [{
                "supplier": m["winner"].upper(), "supplier_name": m["winner"],
                "buyer": "SYNTHETIC", "buyer_name": "(synthetic test buyer)",
                "value": "", "date": "", "group": GROUP}]
        _, raw = stream.extract_tender(sref, row, GROUP, f"synth-{i:04d}")
        mine = stream.comparable(raw, amap)
        peers = [r for r in base if r["reference"] not in (ref, sref)] + mine
        case = tenderlens.run_one(sref, (notices, wstats, peers, graph),
                                  a.llm, a.online)
        # Same path as M2: rules offline, then online, then (with --llm) the
        # LLM wherever the rules leave something open. Cached, so repeating
        # what M2 just did costs nothing.
        l1 = spec_tools.brands_without_equivalent(sref, offline=True)
        if a.online:
            l1 = spec_tools.brands_without_equivalent(sref, offline=False)
        if a.llm and (l1.get("unresolved") or l1.get("findings")):
            l1 = spec_tools.brands_without_equivalent(
                sref, offline=not a.online, use_llm=True)
        outs = [brand_outcome(l1, b) for b in m["brands"].split("|") if b]
        res = dict(m)
        res.update({"route": case["route"], "score": case["score"],
                    "m1_route": case.get("m1_route"),
                    "outcomes": "|".join(outs),
                    "l1_findings": len(l1.get("findings", [])),
                    "l1_brands": "|".join(f["brand"] for f in
                                          l1.get("findings", [])),
                    "correct": case["route"] == m["expected"]})
        results.append(res)
        case["synthetic"] = m["variant"]
        cases.append(case)
        if i % 30 == 0:
            print(f"  {i}/{len(man)} ({time.time() - t0:.0f}s)")

    cols = list(results[0])
    with io.open(os.path.join(OUT, "results.csv"), "w", encoding="utf-8",
                 newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(results)
    meta = {"scope": "synthetic rigged tenders, 2510",
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "tenders": len(cases), "awards": graph is not None,
            "llm": a.llm, "weights": m4_case_builder.WEIGHTS,
            "alert_at": m4_case_builder.ALERT, "watch_at": m4_case_builder.WATCH}
    tenderlens.write_outputs(cases, meta, os.path.join(OUT, "queue"))
    report(results, state, a.online)


def report(results, state, online):
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
        f"so those copies are expected to CLOSE.\n")
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

    say("\n5. Brand found, by whether the offline cache knows the brand")
    by = collections.defaultdict(lambda: [0, 0])
    for r in pos:
        for o, c in zip(r["outcomes"].split("|"), r["in_cache"].split("|")):
            by[c][1] += 1
            by[c][0] += o == "found"
    for c in ("True", "False"):
        f, t = by[c]
        say(f"   cached={c:5} {f}/{t}")

    # A finding on a brand the TEST added is a test result; a finding on a
    # brand already written in the real base tender is a REAL finding (or a
    # real false alarm) and is audited by hand, not scored as the test's.
    def injected(r, b):
        return any(b.lower().split()[0] == x.lower() or
                   b.lower().startswith(x.lower())
                   for x in r["brands"].split("|") if x)
    neg = [r for r in results if not is_rigged(r)]
    wrong = [r for r in neg if any(injected(r, b) for b in
                                   r.get("l1_brands", "").split("|") if b)]
    say(f"\n6. Copies that should close (D+E+F, plus A+B+C+G built on a base "
        f"with the clause): {len(wrong)}/{len(neg)} flag the brand the test "
        f"added")
    for r in wrong[:10]:
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
    say(f"\n7. Ranking: {len(real)} real tenders + {len(syn)} copies "
        f"({len(pos)} rigged)")
    say(f"   precision@50: {sum(x[2] for x in worst) / 50:.2f} (ties broken "
        f"against us) to {sum(x[2] for x in best) / 50:.2f} (ties for us)")
    say(f"   of everything flagged (alert+watch): "
        f"{sum(x[2] for x in flagged)}/{len(flagged)} are rigged copies")
    say(f"   Caveat: {len(pos)} rigged copies among ~{len(pool)} is far more "
        "rigging than real life; read the per-copy recall above first.")

    text = "\n".join(L)
    with io.open(os.path.join(OUT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + text)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--llm", action="store_true",
                    help="use the LLM wherever the rules leave names open "
                         "(Windows; spends tokens)")
    ap.add_argument("--variants", default="",
                    help="run only these copy types, e.g. A,D,F - to limit "
                         "tokens with --llm")
    ap.add_argument("--bases", choices=["no-clause", "any"],
                    default="no-clause",
                    help="--build: pick only base tenders that do NOT already "
                         "accept equivalents for every brand (default); "
                         "'any' = the earlier selection")
    spec_tools.add_l1_args(ap)
    a = ap.parse_args()
    spec_tools.apply_l1_args(a)
    if a.build or not a.run:
        build(a.n, a.seed, a.bases)
    if a.run or not a.build:
        run(a)


if __name__ == "__main__":
    main()
