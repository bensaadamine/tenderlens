"""
CANADA - STEP 5: the attribute map.

The census leaves every requirement label exactly as the tender wrote it, so
one attribute arrives as five strings:

    capacity | payload | gvwr | trailer payload capacity | vehicle payload

Five groups of 7-9 tenders each, all below the n >= 8 floor, when it is really
one attribute with ~30 peers. Merging them is the difference between a
detector that can speak and one that always answers "insufficient peers".

Rules cannot do this. "capacity" and "payload" share no token; "weight" and
"weight capacity" share one and must NOT merge. So this is the LLM's job, and
it is the project's first real LLM spend.

WHAT IT COSTS, and why that is affordable: only labels appearing in >= 2
tenders can ever form a peer group, so the singletons - 2,101 of 2,848 in
2510 - are not worth a token. The rest go out in batches of ~60, one batch per
(product family, physical dimension), which keeps each question small and
concrete. Measured cost is in reports/llm_usage.csv; `python tools/llm.py --usage`
prints it.

THE OUTPUT IS FROZEN ON PURPOSE. reports/attribute_map.json is written once
and then only ADDED to. If the grouping drifts between runs, precision@50 from
last week cannot be compared with this week's, and the headline metric becomes
meaningless. The file is plain JSON so a human can read it and fix a bad merge
by hand - and should, because one wrong merge silently pools two unrelated
attributes and every finding built on that group is wrong.

Run (on Windows - api.groq.com is blocked from the agent's shells):
    python prepare/05_attribute_map.py --group 2510
    python prepare/05_attribute_map.py --group 2510 --dry-run    # cost, no calls
    python prepare/04_census.py --group 2510 --map               # re-measure with it

Writes:
    reports/attribute_map.json      {raw_label: canonical}, frozen + additive
    reports/attribute_map_report.txt  what merged, what was rejected
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
import sys
from collections import Counter, defaultdict

csv.field_size_limit(10_000_000)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

MAP_PATH = "reports/attribute_map.json"

# A canonical name that is only a dimension word is not an attribute, it is a
# dumping ground. Measured on the first 2510 run: "length" swallowed 59 raw
# labels including "boom length", "beavertail length" and "any packages
# excess ten feet any side"; "height" took 56 including a barcode spec;
# "speed" merged WIND speed with road speed. Coverage read 75% where the
# defensible figure was 39% - pooling everything measured in millimetres
# recreates the category-wide comparison this design exists to avoid, while
# making the headline number look better. Refused in code as well as in the
# prompt, because a prompt is advice.
def squash(name):
    """Key for 'same name, different typing'. "ga wr" -> "gawr"."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


BARE_DIMENSIONS = {
    "length", "width", "height", "depth", "thickness", "diameter", "radius",
    "weight", "mass", "speed", "temperature", "capacity", "volume", "area",
    "pressure", "power", "clearance", "voltage", "current", "frequency",
    "size", "displacement", "load", "rating", "dimension", "distance",
    "longueur", "largeur", "hauteur", "poids", "vitesse",
}

SYSTEM = (
    "You normalise technical requirement labels taken from Canadian "
    "government procurement specifications. You reply with JSON only - no "
    "prose, no code fences."
)

# The prompt carries the two failure modes that matter, with examples, because
# both were observed in real data: labels that are obviously the same thing
# but share no words, and labels that share a word but are opposites.
PROMPT = """These labels were extracted from tender specifications in UNSPSC \
category %(family)s. Each measures something in the dimension: %(dimension)s.

Give each label a canonical name, so that labels measuring THE SAME property \
of THE SAME part get the SAME canonical name.

Rules:
1. The canonical name MUST say WHAT is measured, not merely which dimension. \
A bare dimension word is NEVER a valid answer.
   REJECTED: "length", "height", "width", "speed", "capacity", "weight"
   GOOD: "overall length", "boom length", "cargo area height", \
"cruising speed", "fuel tank capacity"
   Different parts of the product are DIFFERENT attributes even in the same \
dimension: "boom length" and "beavertail length" must NOT both become \
"length".
2. Merge only true synonyms for the same quantity on the same part.
   "gross vehicle weight rating" / "gvwr" / "gvw" -> "gvwr"
   "body exterior height" / "overall vehicle height" -> "overall height"
3. NEVER merge different or opposite attributes, even when they share words.
   "weight" (what it weighs) vs "weight capacity" (what it carries) - \
DIFFERENT.
   "turning radius" vs "turning circle" - DIFFERENT, they differ by a factor \
of two.
   "wind speed" (weather it must survive) vs "road speed" (how fast it \
drives) - DIFFERENT.
4. Return "unknown" whenever the label does not identify a measurable \
property OF THE PRODUCT ITSELF. Be strict: "unknown" is correct far more \
often than a vague name.
   - packaging, pallets, crates, shipping, paperwork
   - bid formatting, paper sizes, barcode or label specifications
   - contract clause references, delivery schedules
   - human body measurements
   - ambient or weather conditions rather than a product rating
   - any fragment where you cannot tell what is being measured
5. Lowercase, singular, no units, no comparator words.

%(existing)sInput labels. Each line is:

  - label  (seen in N tenders)  [example wording]
      section: the document heading the requirement sits under
      context: the surrounding lines as they appear in the tender

Use the section and context to work out WHAT is being measured. The label \
alone is often a fragment of a table cell - "winch provided" with context \
"first layer pull of at least 9,072 kg" is a winch pull rating, not an \
unknown. But context that describes packaging, paperwork or a material \
standard still means "unknown": context is there to identify a real \
attribute, not to invent one.

%(labels)s

Reply with a JSON object mapping EVERY input label to its canonical name:
{"label one": "canonical", "label two": "unknown", ...}"""


def load_map(path=MAP_PATH):
    if os.path.exists(path):
        try:
            with io.open(path, encoding="utf-8") as f:
                d = json.load(f)
            return d.get("map", d), d.get("meta", {})
        except (OSError, ValueError) as e:
            sys.exit(f"{path} is unreadable ({e}). It is a frozen artifact - "
                     f"fix or move it by hand rather than letting this script "
                     f"overwrite it.")
    return {}, {}


def save_map(mapping, meta, path=MAP_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "map": dict(sorted(mapping.items()))},
                  f, indent=1, ensure_ascii=False, sort_keys=False)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="2510")
    ap.add_argument("--census", default="")
    ap.add_argument("--out", default=MAP_PATH)
    ap.add_argument("--min-tenders", type=int, default=2,
                    help="only canonicalize labels seen in at least this many "
                         "distinct tenders. A label in one tender cannot form "
                         "a peer group, so spending a token on it buys "
                         "nothing.")
    ap.add_argument("--batch", type=int, default=60,
                    help="labels per LLM call. Groq's cap is ~6,000 tokens "
                         "per minute, and a long list also makes the model "
                         "sloppier about rule 2.")
    ap.add_argument("--family", choices=["unspsc8", "unspsc6", "unspsc4"],
                    default="unspsc6", help="must match prepare/04_census.py")
    ap.add_argument("--strong", action="store_true",
                    help="use the larger model; worth it here, the merge "
                         "decisions are the whole value of this step")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the batches and the token estimate, call "
                         "nothing")
    ap.add_argument("--rebuild", action="store_true",
                    help="archive the current map to attribute_map_v<N>.json "
                         "and build a fresh one. The map is frozen and "
                         "additive, so this is the only way to correct a bad "
                         "grouping - and the old one is kept, because any "
                         "metric computed against it must stay explainable.")
    ap.add_argument("--recheck-unknown", action="store_true",
                    help="re-ask only the labels currently mapped to "
                         "'unknown', now with the section and context that "
                         "04 carries (decision 24). 360 of 699 labels in "
                         "2510 are unknown because a bare table cell cannot "
                         "be named alone. Settled names are untouched, so "
                         "this can only add attributes.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    census = args.census or f"reports/census_{args.group}.csv"
    if not os.path.exists(census):
        sys.exit(f"missing {census} - run prepare/04_census.py --group {args.group}")

    cut = {"unspsc8": 8, "unspsc6": 6, "unspsc4": 4}[args.family]

    # ------------------------------------------------------------ gather
    tenders = defaultdict(set)      # (fam, dim, label) -> {reference}
    examples = defaultdict(Counter)  # (fam, dim, label) -> raw wording
    # Section heading and surrounding lines, carried by 04 since decision 24.
    # A label is frequently one cell of a table - "fpm", "winch provided",
    # "hr passenger" - and no amount of prompting makes those nameable on
    # their own. 360 of 699 labels came back `unknown` without this.
    sections = defaultdict(Counter)
    contexts = defaultdict(Counter)
    rows_cache = []
    with io.open(census, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            fam = (r.get("unspsc8") or args.group)[:cut] or args.group
            k = (fam, r["dimension"], r["label"])
            tenders[k].add(r["reference"])
            if r.get("raw_label"):
                examples[k][r["raw_label"]] += 1
            if r.get("section"):
                sections[k][r["section"]] += 1
            if r.get("context"):
                contexts[k][r["context"]] += 1
            rows_cache.append(r)

    # CO-OCCURRENCE VETO, value-aware.
    #
    # First idea: if one tender states two labels, they cannot be synonyms.
    # That is wrong on this data, because the extractor itself produces
    # several overlapping labels from one sentence - pdfminer splits a table
    # cell and the +/-2 window reads the fragments separately. "temperatures
    # ranging" and "found canada temperatures ranging" co-occur because they
    # ARE the same line.
    #
    # The test that works: do they carry DIFFERENT VALUES in the same tender?
    #   different values -> two real requirements -> never merge
    #   same value       -> one requirement extracted twice -> merging is fine
    # Majority vote across the tenders that state both, so a single noisy
    # tender cannot split a good group.
    #
    # Measured: this correctly vetoes "gawr" vs "gvwr" (differ in 5 tenders)
    # and "outside width" vs "width" (3), while correctly permitting the
    # temperature fragments and "front axle load" variants.
    #
    # The converse never holds: labels that do not co-occur may still be
    # different, they just never met. So this is only ever a VETO on merging,
    # never a reason to merge.
    tvals = defaultdict(set)
    tsrcs = defaultdict(set)
    for r in rows_cache:
        tvals[(r["reference"], r["label"])].add(r["value"])
        tsrcs[(r["reference"], r["label"])].add(r["source_line"][:60])

    labs_in = defaultdict(set)
    for (ref, lab) in tvals:
        labs_in[ref].add(lab)

    differ = defaultdict(Counter)   # (a,b) -> {"diff": n, "same": n}
    for ref, labs in labs_in.items():
        ls = sorted(labs)
        for i, a in enumerate(ls):
            for b in ls[i + 1:]:
                if tsrcs[(ref, a)] & tsrcs[(ref, b)]:
                    continue        # same source line: an extraction artifact
                key = (a, b)
                if tvals[(ref, a)] & tvals[(ref, b)]:
                    differ[key]["same"] += 1
                else:
                    differ[key]["diff"] += 1

    incompat = defaultdict(set)
    for (a, b), c in differ.items():
        if c["diff"] > c["same"]:
            incompat[a].add(b)
            incompat[b].add(a)
    print(f"co-occurrence veto: {sum(len(v) for v in incompat.values())//2:,} "
          f"label pairs proven to be different attributes")

    keep = {k: v for k, v in tenders.items() if len(v) >= args.min_tenders}
    print(f"{len(tenders):,} distinct labels in {census}")
    print(f"{len(keep):,} seen in >= {args.min_tenders} tenders "
          f"-> these are the only ones worth canonicalizing")
    print(f"{len(tenders)-len(keep):,} singletons skipped (cannot form a peer "
          f"group)")

    # one batch per (family, dimension): a small, concrete question
    groups = defaultdict(list)
    for (fam, dim, lab), refs in keep.items():
        groups[(fam, dim)].append((lab, len(refs)))

    batches = []
    for (fam, dim), labs in sorted(groups.items(), key=lambda x: -len(x[1])):
        labs.sort(key=lambda x: -x[1])
        for i in range(0, len(labs), args.batch):
            batches.append((fam, dim, labs[i:i + args.batch]))

    # Estimate what will ACTUALLY be sent, not what exists. Settled labels
    # are skipped in the loop below, so counting them here would quote a
    # price three times the real one - and a wrong estimate is how a free
    # tier gets spent by surprise. The map has to be read before the
    # estimate for the same reason.
    _seen, _ = load_map(args.out)

    def _will_ask(lab):
        return (lab not in _seen
                or (args.recheck_unknown and _seen.get(lab) == "unknown"))

    ask = [(f, d, [x for x in b if _will_ask(x[0])]) for f, d, b in batches]
    ask = [(f, d, b) for f, d, b in ask if b]
    n_ask = sum(len(b) for _f, _d, b in ask)
    # Context roughly doubles a short label's line; see decision 24.
    est = sum(sum(len(l) // 4 + (80 if len(l.split()) <= 3
                                 or args.recheck_unknown else 14)
                  for l, _ in b) * 2 + 320
              for _f, _d, b in ask)
    print(f"\n{len(ask)} LLM calls over {n_ask:,} labels "
          f"({len(keep) - n_ask:,} already settled and skipped), "
          f"~{est:,} tokens estimated")
    print(f"  (Groq free tier is ~6,000 tok/min, so expect roughly "
          f"{est/5500.0:.0f} minute(s) of throttled wall clock)\n")
    for fam, dim, b in ask[:12]:
        print(f"    {fam:8s} {dim:12s} {len(b):3d} labels")
    if len(ask) > 12:
        print(f"    ... and {len(ask)-12} more batches")

    if args.dry_run:
        print("\n--dry-run: nothing called, nothing written")
        return

    if args.rebuild and os.path.exists(args.out):
        n = 1
        stem = args.out.rsplit(".", 1)[0]
        while os.path.exists(f"{stem}_v{n}.json"):
            n += 1
        os.replace(args.out, f"{stem}_v{n}.json")
        print(f"\n--rebuild: previous map archived to {stem}_v{n}.json")

    mapping, meta = load_map(args.out)
    print(f"\nexisting map: {len(mapping):,} labels already canonicalized")

    # Canonical names indexed BY DIMENSION. The first run passed one global
    # list of up to 120 names into every call, which is both noise - a length
    # name cannot help a mass batch - and most of a 34k-token overrun (the run
    # cost 82,776 tokens against a 48,374 estimate). Scoped per dimension the
    # context is shorter and actually relevant.
    dim_canon = defaultdict(set)
    # squashed form -> the spelling we settled on first, per dimension. The
    # model is not reliably consistent even inside one answer, so the FIRST
    # spelling wins and later variants are folded into it.
    canon_alias = defaultdict(dict)

    # Repair pass over whatever the map already holds, so a resume heals the
    # file instead of needing a full --rebuild and a second token spend. The
    # winner is the spelling used by the MOST labels, not the first seen.
    spellings = defaultdict(Counter)
    lab_dim = {}
    for (_f, dim, lab) in keep:
        lab_dim[lab] = dim
        v = mapping.get(lab)
        if v and v != "unknown":
            spellings[(dim, squash(v))][v] += 1
    _fixed = 0
    for (dim, sq), c in spellings.items():
        win = c.most_common(1)[0][0]
        canon_alias[dim][sq] = win
        if len(c) > 1:
            for lab, v in list(mapping.items()):
                if lab_dim.get(lab) == dim and v != win and squash(v) == sq:
                    mapping[lab] = win
                    _fixed += 1
    if _fixed:
        print(f"repaired {_fixed} label(s) whose canonical name was a "
              f"respelling of another")

    # Apply the veto to merges already in the map. This costs nothing - no
    # LLM call - so a bad merge from an earlier run is undone on the next run
    # rather than needing a full rebuild. The member seen in FEWER tenders
    # loses its merge, keeping the better-evidenced group intact.
    def _nt(lab):
        return sum(len(v) for (_f, _d, l), v in tenders.items() if l == lab)

    _groups = defaultdict(list)
    for lab, v in mapping.items():
        if v not in ("unknown", lab):
            _groups[v].append(lab)
    _undone = []
    for v, labs in _groups.items():
        for lab in sorted(labs, key=_nt):          # weakest first
            if any(o in incompat.get(lab, ()) and mapping.get(o) == v
                   for o in labs if o != lab):
                mapping[lab] = lab
                _undone.append((lab, v))
    if _undone:
        print(f"veto un-merged {len(_undone)} label(s) that carry different "
              f"values from a group-mate in the same tender:")
        for lab, v in _undone[:10]:
            print(f"    {lab[:46]!r} pulled out of {v!r}")
        save_map(mapping, meta, args.out)
    for (_f, dim, lab) in keep:
        v = mapping.get(lab)
        if v and v != "unknown":
            dim_canon[dim].add(v)

    import llm

    # ------------------------------------------------------------- the calls
    added, failed = 0, 0
    all_rejected = []
    all_folded = []
    all_vetoed = []

    def tenders_by_label(lab):
        out = set()
        for (_f, _d, l), refs in tenders.items():
            if l == lab:
                out |= refs
        return out
    queue = list(batches)
    n = 0
    while queue:
        fam, dim, labs = queue.pop(0)
        n += 1
        # Resumable: a label already decided is never re-asked, so a re-run
        # after a rule change costs nothing. --recheck-unknown reopens ONLY
        # the labels that failed, now that 04 carries the context they
        # failed for want of (decision 24). Settled names are never
        # disturbed, so this can only add attributes, never move one.
        todo = [(l, c) for l, c in labs
                if l not in mapping
                or (args.recheck_unknown and mapping.get(l) == "unknown")]
        if not todo:
            continue

        # Reusing names across batches is what stops batch 2 inventing
        # "chamber capacity" beside batch 1's "chamber volume" and silently
        # halving both peer counts.
        known = sorted(dim_canon.get(dim, ()))
        existing = ""
        if known:
            existing = ("Canonical names already in use for this dimension - "
                        "REUSE one when it fits, invent a new one only when "
                        "none does:\n" + ", ".join(known[:80]) + "\n\n")

        # Only the pairs inside THIS batch are worth the tokens; the code
        # check below catches anything crossing batches.
        inbatch = {l for l, _ in todo}
        pairs = []
        for l, _c in todo:
            for o in sorted(incompat.get(l, ())):
                if o in inbatch and (o, l) not in pairs:
                    pairs.append((l, o))
        warn = ""
        if pairs:
            warn = ("These pairs appear TOGETHER in the same tender, so they "
                    "are measuring DIFFERENT things. Give each a different "
                    "canonical name:\n"
                    + "\n".join(f"- {a!r} vs {b!r}" for a, b in pairs[:25])
                    + "\n\n")

        lines = []
        for lab, cnt in todo:
            k = (fam, dim, lab)
            ex = examples[k].most_common(1)
            ex = re.sub(r"\s+", " ", ex[0][0])[:70] if ex else ""
            lines.append(f"- {lab}  (seen in {cnt} tenders)  [{ex}]")
            # Context costs tokens, so spend it where it decides something:
            # short labels, which are the table cells that cannot be named
            # alone, and anything being re-asked BECAUSE it failed without
            # context. A long descriptive label already carries its meaning.
            if len(lab.split()) <= 3 or args.recheck_unknown:
                sec = sections[k].most_common(1)
                ctx = contexts[k].most_common(1)
                if sec:
                    lines.append(f"      section: {sec[0][0][:70]}")
                if ctx:
                    lines.append(f"      context: {ctx[0][0][:200]}")

        prompt = PROMPT % {"family": fam, "dimension": dim,
                           "existing": existing + warn,
                           "labels": "\n".join(lines)}
        print(f"  [{n}/~{len(batches)}] {fam} {dim}: {len(todo)} labels ...",
              end="", flush=True)
        try:
            ans = llm.ask_json(SYSTEM, prompt, strong=args.strong,
                               max_tokens=4000, tag="attribute_map",
                               verbose=args.verbose)
        except llm.NoProvider:
            sys.exit("\nno LLM key set - see llm.py. Nothing was written.")
        except Exception as e:
            ans = None
            print(f" FAILED ({type(e).__name__})", end="")

        if ans is not None and not isinstance(ans, dict):
            print(" FAILED (not a JSON object)", end="")
            ans = None

        if ans is None:
            # A reply that will not parse is usually one the model wrote too
            # much of. Half the labels is a smaller ask and normally parses;
            # without this, one bad reply silently cost 59 labels.
            if len(todo) > 6:
                h = len(todo) // 2
                queue.insert(0, (fam, dim, todo[h:]))
                queue.insert(0, (fam, dim, todo[:h]))
                print(" -> retrying in two halves")
            else:
                failed += 1
                print(" -> giving up on these")
            continue

        # Validate. A model that invents labels, or drops them, must not be
        # allowed to quietly reshape the grouping: anything it did not answer
        # for maps to itself, anything it invented is discarded.
        want = {l for l, _ in todo}
        got = 0
        rejected = []
        folded = []
        for lab in want:
            # A label being re-asked was already judged `unknown`. If this
            # pass does not improve on that, it must STAY unknown - falling
            # back to "maps to itself" would quietly promote boilerplate
            # back into the census, undoing the judgement rather than
            # declining to revise it.
            prev = mapping.get(lab)
            fallback = "unknown" if prev == "unknown" else lab
            v = ans.get(lab)
            if not isinstance(v, str) or not v.strip():
                mapping[lab] = fallback     # unanswered -> unchanged
                continue
            v = re.sub(r"\s+", " ", v.strip().lower())[:60]
            if v in BARE_DIMENSIONS:
                rejected.append((lab, v))
                mapping[lab] = fallback  # refuse the merge, keep the row
                continue
            # fold a respelling into the name already chosen for it
            sq = squash(v)
            if sq in canon_alias[dim] and canon_alias[dim][sq] != v:
                folded.append((v, canon_alias[dim][sq]))
                v = canon_alias[dim][sq]
            canon_alias[dim].setdefault(sq, v)
            mapping[lab] = v
            dim_canon[dim].add(v)
            got += 1
        extra = [k for k in ans if k not in want]
        added += len(want)
        print(f" ok ({got}/{len(want)} answered"
              + (f", {len(rejected)} bare-dimension refused" if rejected
                 else "")
              + (f", {len(extra)} invented and discarded" if extra else "")
              + ")")
        # Enforce the veto. If the model gave two co-occurring labels the
        # same name, the one seen in FEWER tenders loses its merge - keeping
        # the better-evidenced group intact.
        vetoed = []
        canon_of = {}
        for lab in sorted(want, key=lambda l: -len(tenders_by_label(l))):
            v = mapping.get(lab)
            if not v or v == "unknown" or v == lab:
                continue
            for other, ov in list(canon_of.items()):
                if ov == v and other in incompat.get(lab, ()):
                    mapping[lab] = lab
                    vetoed.append((lab, other, v))
                    break
            else:
                canon_of[lab] = v
        all_vetoed.extend(vetoed)
        all_rejected.extend(rejected)
        all_folded.extend(folded)

        meta = {"group": args.group, "family_level": args.family,
                "min_tenders": args.min_tenders,
                "labels": len(mapping), "batches_run": n,
                "note": "FROZEN artifact - additive only. A changed grouping "
                        "makes precision@50 incomparable between runs. Edit "
                        "by hand to fix a bad merge."}
        save_map(mapping, meta, args.out)

    print(f"\n{added:,} labels canonicalized, {failed} batch(es) failed")
    if all_vetoed:
        print(f"{len(all_vetoed)} merge(s) vetoed - the two labels appear in "
              f"the same tender, so they cannot be the same attribute:")
        for lab, other, v in all_vetoed[:10]:
            print(f"    {lab[:38]!r} kept apart from {other[:30]!r} "
                  f"(both -> {v!r})")
    if all_folded:
        print(f"{len(all_folded)} respellings folded into an existing name:")
        for bad, good in all_folded[:10]:
            print(f"    {bad!r} -> {good!r}")
    if all_rejected:
        print(f"{len(all_rejected)} bare-dimension answers refused and left "
              f"unmerged:")
        for lab, v in all_rejected[:12]:
            print(f"    {v:14s} <- {lab[:58]}")
    print(f"map -> {args.out}  ({len(mapping):,} entries)")

    # ------------------------------------------------------------- what merged
    merged = defaultdict(list)
    for lab, canon in mapping.items():
        merged[canon].append(lab)

    newt = defaultdict(set)
    for (fam, dim, lab), refs in tenders.items():
        canon = mapping.get(lab)
        if canon == "unknown":
            continue
        newt[(fam, dim, canon or lab)] |= refs

    before = sum(1 for v in tenders.values() if len(v) >= 8)
    after = sum(1 for v in newt.values() if len(v) >= 8)
    cov_b, cov_a = set(), set()
    for v in tenders.values():
        if len(v) >= 8:
            cov_b |= v
    for v in newt.values():
        if len(v) >= 8:
            cov_a |= v
    allrefs = set()
    for v in tenders.values():
        allrefs |= v

    lines = []

    def say(s=""):
        print(s)
        lines.append(s)

    say("\n" + "=" * 70)
    say("EFFECT ON PEER DEPTH - the only reason this step exists")
    say("=" * 70)
    say(f"\n  labels clearing n >= 8 : {before:4d}  ->  {after:4d}")
    say(f"  tenders covered        : {len(cov_b):4d}  ->  {len(cov_a):4d}"
        f"   of {len(allrefs)}")
    if allrefs:
        say(f"  coverage               : "
            f"{100.0*len(cov_b)/len(allrefs):4.0f}%  ->  "
            f"{100.0*len(cov_a)/len(allrefs):4.0f}%")
    say(f"\n  labels marked 'unknown' (boilerplate, not attributes): "
        f"{sum(1 for v in mapping.values() if v == 'unknown'):,}")

    say("\n  biggest merges:\n")
    for canon, labs in sorted(merged.items(), key=lambda x: -len(x[1]))[:25]:
        if canon == "unknown" or len(labs) < 2:
            continue
        n_t = max((len(newt[k]) for k in newt if k[2] == canon), default=0)
        say(f"  {canon:34s} n={n_t:3d}  <- {len(labs)} labels")
        for l in sorted(labs)[:6]:
            say(f"       {l[:62]}")

    out = "reports/attribute_map_report.txt"
    with io.open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\n  report -> {out}")
    print("  then:  python prepare/04_census.py --group %s --map" % args.group)


if __name__ == "__main__":
    main()
