"""
The M2 exam: run level 1 on every case judged by hand and compare the letter
it gives with the auditor's.

    python tests/m2_eval.py                    # rules only (the baseline)
    python tests/m2_eval.py --agent --online   # with the M2 agent (Windows)
    python tests/m2_eval.py --split holdout    # one set only
    python tests/m2_eval.py --only cb-425-92729935      # a partial run
    python tests/m2_eval.py --sample           # build the held-out set to label

Letters are the auditor's grid: a not a brand, b bidder speaking, c buyer's
equipment, d list of brands, e equivalent accepted, f required with no
equivalent. A case's "finding" is right when it is a finding exactly when
the auditor said "f"; precision and recall are on findings.

TWO SETS (audit finding 2026-11-01):
  tuning   tests/m2_labels.csv - the 36 cases the rules and the agent's
           prompt were fixed on. A score here is flattered by construction:
           every false finding in it got a rule fix.
  holdout  tests/m2_holdout.csv - cases never looked at while tuning, built
           by --sample and labelled by the auditor. Only rows with a letter
           count. This is the score to quote.

EVERY RUN KEEPS ITS EVIDENCE (audit finding 2026-11-01: the 36-case agent
result of 2026-10-23 was overwritten by a later one-tender run). Each run
writes reports/m2_eval/<stamp>_<mode>[_partial]/: cases.csv, summary.txt
(with the code commit and settings) and, with --agent, traces.txt. Nothing
is ever overwritten.

With --agent, a tender counts only if the agent really judged it: when no
model answers, M2 keeps the rules' result, and scoring that as the agent's
would measure the rules again (it did, on 2026-10-22). Such tenders are
listed apart; after STOP_AFTER of them in a row the run stops.
"""

import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import repo  # noqa: E402  (agents/ tools/ prepare/ tests/ importable; works from the repo root)

import argparse
import collections
import csv
import glob
import io
import json
import os
import random
import subprocess
import time

import m2_spec_auditor

m2_spec_auditor.AGENT_DOWN = "keep"   # tenders the agent missed are listed apart
import brands
import documents

LABELS = os.path.join("tests", "m2_labels.csv")
HOLDOUT = os.path.join("tests", "m2_holdout.csv")
OUT_DIR = os.path.join("reports", "m2_eval")
STOP_AFTER = 3      # tenders in a row where the agent could not run
LETTERS = set("abcdef")
HOLDOUT_COLS = ["reference", "name", "letter", "source", "split",
                "note", "document", "line_no", "evidence"]
SAMPLE_GROUPS = ("2510", "4110")
SAMPLE_UNRESOLVED = 12   # names the rules could not settle, per group
SAMPLE_SEED = 20261101

# What a rule clearance means in the auditor's grid.
STATUS_LETTER = {
    "OK": "e", "OK_EQUIV_PRODUCTS_CLAUSE": "e", "OK_BLANKET_CLAUSE": "e",
    "OK_PDF_ORDER": "e", "OK_BUYER_EQUIPMENT": "c", "OK_MAKE_LIST": "d",
    "OK_PLATFORM": "c",
    "OK_LLM_CLEARED": "cleared by the LLM judge",
}


def letter_for(l1, name):
    """The letter level 1 gave this name, or why it gave none."""
    key = name.lower()

    def same(row):
        b = (row.get("brand") or "").lower()
        return key in b or b in key

    for r in l1.get("findings", []):
        if same(r):
            return "f"
    for r in l1.get("ok", []):
        if same(r):
            if r.get("agent_letter"):
                return r["agent_letter"]
            return STATUS_LETTER.get(r.get("status"), r.get("status"))
    for r in l1.get("unresolved", []):
        if same(r):
            return "unresolved"
    return "not a candidate"


def agent_status(l1, trace):
    """Did the agent really judge this tender?"""
    if l1.get("agent"):
        stop = l1["agent"].get("stop_reason", "")
        return "incomplete: " + stop if stop.startswith("model") else "ran"
    for t in trace:
        if t.startswith("agent could not start"):
            return "did not run: " + t.split("(", 1)[-1].split(")")[0][:80]
        if t.startswith("agent: nothing"):
            return "ran (nothing to check)"
        if t.startswith("--agent asked but no LLM key"):
            return "did not run: no LLM key"
    return "ran (rules cleared everything)"


# ------------------------------------------------------------------ labels
def load_labels(split="all"):
    """Labelled cases of the chosen set(s); held-out rows need a letter."""
    out = []
    for path, default in ((LABELS, "tuning"), (HOLDOUT, "holdout")):
        if not os.path.exists(path):
            continue
        with io.open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                r["split"] = (r.get("split") or default).strip()
                r["letter"] = (r.get("letter") or "").strip().lower()
                if r["letter"] in LETTERS and split in ("all", r["split"]):
                    out.append(r)
    return out


def score(rows):
    """Counts and rates on findings for one set of scored rows."""
    n = len(rows)
    want = [r for r in rows if r["expected"] == "f"]
    got = [r for r in rows if r["got"] == "f"]
    tp = sum(r["expected"] == "f" for r in got)
    return {"cases": n,
            "finding_right": sum(r["finding_right"] for r in rows),
            "letter_right": sum(r["letter_right"] for r in rows),
            "lock_ins": len(want), "findings": len(got), "true_findings": tp,
            "precision": tp / len(got) if got else None,
            "recall": tp / len(want) if want else None,
            "given": collections.Counter(r["got"] for r in rows)}


def _pct(x):
    return "n/a" if x is None else f"{x:.0%}"


def summary_text(rows, mode, meta):
    out = [f"M2 exam - {mode}", f"run {meta['stamp']}, code {meta['commit']}",
           f"settings: {json.dumps(meta['settings'], sort_keys=True)}", ""]
    for split in ("tuning", "holdout"):
        rs = [r for r in rows if r["split"] == split]
        if not rs:
            out.append(f"{split}: no labelled case\n")
            continue
        s = score(rs)
        note = (" (flattered: the rules were fixed on these cases)"
                if split == "tuning" else "")
        out += [f"{split.upper()} - {s['cases']} cases{note}",
                f"  finding right:  {s['finding_right']}/{s['cases']}",
                f"  precision:      {_pct(s['precision'])} "
                f"({s['true_findings']} of {s['findings']} findings real)",
                f"  recall:         {_pct(s['recall'])} "
                f"({s['true_findings']} of {s['lock_ins']} lock-ins found)",
                f"  exact letter:   {s['letter_right']}/{s['cases']}",
                "  what each was given: " + ", ".join(
                    f"{k} {c}" for k, c in s["given"].most_common()), ""]
    return "\n".join(out)


def _commit():
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True,
                             timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"],
                               capture_output=True, text=True,
                               timeout=10).stdout.strip()
        return (sha or "unknown") + (" + uncommitted changes" if dirty else "")
    except Exception:
        return "unknown"


def run_dir(mode, partial):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(OUT_DIR, f"{stamp}_{mode}" + ("_partial" if partial
                                                   else ""))
    n = 1
    while os.path.exists(d):            # never overwrite, even same second
        n += 1
        d = d.rstrip("0123456789").rstrip("-") + f"-{n}"
    os.makedirs(d)
    return d, stamp


# ------------------------------------------------------------------ sample
def _tenders_with_text(group):
    d = os.path.join(documents.TEXT_DIR, group)
    if not os.path.isdir(d):
        return []
    return sorted({fn.split("__")[0] for fn in os.listdir(d) if "__" in fn})


def _agent_findings():
    """Brand findings the M2 agent gave in the incremental runs' batches -
    never labelled, so they test the agent where it said 'f'."""
    out = []
    for fp in sorted(glob.glob(os.path.join("reports", "kb", "*", "batches",
                                            "*_queue.json"))):
        try:
            with io.open(fp, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        if not (d.get("meta") or {}).get("agent"):
            continue
        for c in d.get("cases", []):
            for x in c.get("claims", []):
                if x.get("kind") == "brand_without_equivalent":
                    s = x.get("source") or {}
                    out.append({"reference": c["reference"],
                                "name": x.get("brand") or "",
                                "document": s.get("file") or "",
                                "line_no": "", "evidence": s.get("line") or "",
                                "source": "M2 agent finding, batch "
                                          + os.path.basename(fp)[:15]})
    return out


def sample(a):
    """Build tests/m2_holdout.csv: names never used while tuning, for the
    auditor to letter. Rows already lettered are kept; the rest is rebuilt."""
    seen = {(r["reference"], r["name"].lower())
            for r in load_labels("all")}
    tuned_refs = set()
    with io.open(LABELS, encoding="utf-8", newline="") as f:
        tuned_refs = {r["reference"] for r in csv.DictReader(f)}
    kept = []
    if os.path.exists(HOLDOUT):
        with io.open(HOLDOUT, encoding="utf-8", newline="") as f:
            kept = [r for r in csv.DictReader(f)
                    if (r.get("letter") or "").strip()]
    have = {(r["reference"], r["name"].lower()) for r in kept}

    cand = _agent_findings()
    rng = random.Random(SAMPLE_SEED)
    for group in SAMPLE_GROUPS:
        refs = _tenders_with_text(group)
        print(f"  reading {len(refs)} tender(s) of {group} with the rules "
              f"(offline)...")
        unresolved = []
        for ref in refs:
            if ref in tuned_refs:
                continue
            l1 = m2_spec_auditor.run_level1(ref, online=False, agent=False)
            for r in l1.get("findings", []):
                cand.append(_row(ref, r, f"rule finding ({group})"))
            for r in l1.get("unresolved", []):
                unresolved.append(_row(ref, r, f"unresolved name ({group})"))
        rng.shuffle(unresolved)
        picked, per_tender = [], set()
        for r in unresolved:            # one per tender, to spread them
            if r["reference"] not in per_tender:
                picked.append(r)
                per_tender.add(r["reference"])
            if len(picked) >= SAMPLE_UNRESOLVED:
                break
        cand += picked

    new, words = [], collections.defaultdict(set)
    for r in kept:
        words[r["reference"]] |= _words(r["name"])
    for r in cand:
        key = (r["reference"], r["name"].lower())
        if (not r["name"] or key in seen or key in have
                or r["reference"] in tuned_refs
                or brands.is_platform(r["name"])):  # tuned on: Windows...
            continue
        # one brand written several ways in one tender ("FormLabs Form",
        # "Formlabs") is one case, not three
        if _words(r["name"]) & words[r["reference"]]:
            continue
        have.add(key)
        words[r["reference"]] |= _words(r["name"])
        new.append(dict(r, letter="", split="holdout"))
    rows = kept + sorted(new, key=lambda r: (r["reference"], r["name"]))
    with io.open(HOLDOUT, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HOLDOUT_COLS, extrasaction="ignore",
                           lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    by_src = collections.Counter(r["source"].split(",")[0].split(" (")[0]
                                 for r in new)
    print(f"\n{len(new)} new case(s) to label, {len(kept)} kept -> {HOLDOUT}")
    for k, c in by_src.most_common():
        print(f"  {c:3d}  {k}")
    print("Fill 'letter' (a-f) with the auditor's grid; empty rows are "
          "not scored.")


def _words(name):
    return {w for w in "".join(c if c.isalnum() else " "
                               for c in name.lower()).split() if len(w) >= 4
            and w not in ("these", "model", "series", "from", "form")}


def _row(ref, r, source):
    return {"reference": ref, "name": r.get("brand") or "",
            "document": r.get("document") or "",
            "line_no": r.get("line_no") or "",
            "evidence": " ".join(str(r.get("evidence") or "").split())[:200],
            "source": source}


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--split", choices=["all", "tuning", "holdout"],
                    default="all")
    ap.add_argument("--only", action="append", default=[], metavar="REF",
                    help="only this tender (repeatable); marks the run "
                         "partial")
    ap.add_argument("--limit", type=int, default=0,
                    help="only the first N tenders; marks the run partial")
    ap.add_argument("--all-names", action="store_true",
                    help="with --agent: let it judge every open name of the "
                         "tender, not only the labelled ones (hours on the "
                         "free quota; what a normal run does)")
    ap.add_argument("--sample", action="store_true",
                    help="build tests/m2_holdout.csv for the auditor to "
                         "label, and stop")
    brands.add_l1_args(ap)
    a = ap.parse_args()
    brands.apply_l1_args(a)
    if a.sample:
        sample(a)
        return

    labels = load_labels(a.split)
    by_ref = collections.OrderedDict()
    for lab in labels:
        by_ref.setdefault(lab["reference"], []).append(lab)
    if a.only:
        by_ref = collections.OrderedDict(
            (r, by_ref[r]) for r in a.only if r in by_ref)
    if a.limit:
        by_ref = collections.OrderedDict(list(by_ref.items())[:a.limit])
    if not by_ref:
        print("No labelled case to run.")
        return

    if a.agent and not a.all_names:
        # Only the labelled names go to the agent: same verdicts per name,
        # a fraction of the calls. Matching as letter_for() does.
        wanted = {ref: [l["name"].lower() for l in labs]
                  for ref, labs in by_ref.items()}

        def only(ref, name):
            n = (name or "").lower()
            return any(k in n or n in k for k in wanted.get(ref, ()))
        m2_spec_auditor.AGENT_ONLY = only
    mode = ("agent" if a.agent else "rules") + ("-online" if a.online else "")
    out_dir, stamp = run_dir(mode, bool(a.only or a.limit
                                        or a.split != "all"))
    traces = io.open(os.path.join(out_dir, "traces.txt"), "w",
                     encoding="utf-8") if a.agent else None
    rows, down_in_a_row = [], 0
    for ref, labs in by_ref.items():
        trace = []
        l1 = m2_spec_auditor.run_level1(ref, online=a.online, agent=a.agent,
                                        trace=trace)
        ran = agent_status(l1, trace) if a.agent else "-"
        for lab in labs:
            got = letter_for(l1, lab["name"])
            want = lab["letter"]
            rows.append({
                "split": lab["split"], "reference": ref, "name": lab["name"],
                "expected": want, "got": got, "letter_right": got == want,
                "finding_right": (got == "f") == (want == "f"),
                "agent": ran,
                "agent_steps": (l1.get("agent") or {}).get("steps", "")})
        print(f"  {ref:28} [{ran}] " + ", ".join(
            f"{r['name']}: {r['got']} (want {r['expected']})"
            for r in rows[-len(labs):]))
        if traces:
            traces.write(f"=== {ref}\n" + "\n".join(trace) + "\n\n")
            traces.flush()
        down_in_a_row = down_in_a_row + 1 if ran.startswith("did not run") \
            else 0
        if down_in_a_row >= STOP_AFTER:
            print(f"\nStopped: the agent could not run on {STOP_AFTER} "
                  f"tenders in a row. See reports/llm_errors.csv.")
            break
    if traces:
        traces.close()

    with io.open(os.path.join(out_dir, "cases.csv"), "w", encoding="utf-8",
                 newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    skipped = [r for r in rows if r["agent"].startswith("did not run")]
    scored = [r for r in rows if not r["agent"].startswith("did not run")]
    meta = {"stamp": stamp, "commit": _commit(),
            "settings": {"agent": a.agent, "online": a.online,
                         "agent_names": "all" if a.all_names else "labelled",
                         "split": a.split, "only": a.only, "limit": a.limit,
                         "tenders_run": len({r["reference"] for r in rows}),
                         "left_out_agent_did_not_run": len(skipped)}}
    text = summary_text(scored, mode, meta)
    if skipped:
        text += (f"\n{len(skipped)} case(s) left out - the agent did not run "
                 f"on their tender: " + "; ".join(sorted({r["agent"]
                                                          for r in skipped})))
    with io.open(os.path.join(out_dir, "summary.txt"), "w",
                 encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + text)
    print(f"\n-> {out_dir}")


if __name__ == "__main__":
    main()
