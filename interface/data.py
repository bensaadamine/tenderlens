"""What the auditor's page reads and writes.

It reads what the runs wrote - the knowledge bases (reports/kb/<group>/), the
extracted documents (reports/text/) and the synthetic test runs - and computes
no case itself. It writes one thing: the auditor's verdict, recorded in
feedback.csv and applied to the register at once, as `run --feedback` does.
"""

import collections
import csv
import getpass
import glob
import io
import json
import os
import re
import time

from pipeline.stream import FB_COLS, KB, apply_verdict, known_groups
from tools import documents
from tools.report import ROUTE_ORDER

KB_ROOT = "reports/kb"
SYNTH_RUNS = "reports/synth/2510/runs"
DEMO_DIR = "reports/demo"
USAGE, ERRORS = "reports/llm_usage.csv", "reports/llm_errors.csv"
GROQ_DAILY = 200_000           # Groq's free tier, tokens a day (tools/llm.py)
AGENTS = {"m2_agent": "M2 agent - brand names",
          "m3_agent": "M3 agent - supplier spellings",
          "m4_agent": "M4 model coordinator (experiment)",
          "attribute_map": "label map (data preparation)",
          "facts": "facts tool (removed 2026-11-04)",
          "selftest": "connection test"}
NAMES = {"2510": "Motor vehicles", "4110": "Laboratory and scientific equipment"}
SCOPE = "reports/selected_unspsc4.json"          # the families in scope
COUNTS = "reports/category_counts.json"          # tenders with documents
# Families in scope by count, but whose specification is a part number or a
# size code, not requirements to read (decision 9).
NOT_READABLE = {"2520": "aircraft tires: part numbers and size codes, "
                        "not a specification to read"}
JUDGED = {"reviewed_clean": "clean", "confirmed": "confirmed"}


def groups():
    """Every category that has a knowledge base."""
    return known_groups(KB_ROOT)


def families():
    """The goods families in scope: code, description, tenders with
    documents, whether work has started on it, why not if it cannot be."""
    with open(SCOPE, encoding="utf-8") as f:
        codes = json.load(f)
    counts = {}
    if os.path.exists(COUNTS):
        with open(COUNTS, encoding="utf-8") as f:
            counts = json.load(f)
    started = set(groups())
    return [{"group": g, "usable": counts.get("usable", {}).get(g, 0),
             "name": NAMES.get(g) or counts.get("labels", {}).get(g, ""),
             "started": g in started, "not_readable": NOT_READABLE.get(g)}
            for g in codes]


_logged = {}


def _cases(kb):
    """reference -> run_id -> case, from cases.jsonl; read again only when
    the file changes."""
    if not os.path.exists(kb.case_log):
        return {}
    stamp = os.path.getmtime(kb.case_log)
    if _logged.get(kb.case_log, (None,))[0] != stamp:
        by_ref = collections.defaultdict(dict)
        for e in kb.cases()[0]:
            by_ref[e["case"]["reference"]][e["run_id"]] = e["case"]
        _logged[kb.case_log] = (stamp, by_ref)
    return _logged[kb.case_log][1]


def tenders(group):
    """Every registered tender: where it is now, the auditor's verdict, and
    the case the register points to (None for a tender processed before
    cases were logged - re-running the category gives it one)."""
    kb = KB(group, KB_ROOT)
    cases = _cases(kb)
    out = []
    for ref, ev in kb.state().items():
        case = cases.get(ref, {}).get(ev["run_id"])
        out.append({"reference": ref, "pub_date": ev["pub_date"],
                    "pile": ev["pile"], "status": ev["status"],
                    "verdict": JUDGED.get(ev["status"]), "case": case,
                    "score": case["score"] if case else int(ev["score"] or 0)})
    return out


def ranked(rows):
    """Alert first, then watch, then close; higher score first."""
    return sorted(rows, key=lambda t: (
        ROUTE_ORDER[t["case"]["route"]] if t["case"] else 3, -t["score"],
        t["reference"]))


def to_review(t):
    return t["pile"] == "quarantine" and not t["verdict"]


def summary(group):
    rows = tenders(group)
    piles = collections.Counter(t["pile"] for t in rows)
    batches = sorted(glob.glob(os.path.join(KB_ROOT, group, "batches",
                                            "*_settings.json")))
    last = os.path.basename(batches[-1])[:15] if batches else ""
    return {"group": group, "name": NAMES.get(group, ""),
            "tenders": len(rows), "base": piles["base"],
            "quarantine": piles["quarantine"], "none": piles["none"],
            "to_review": sum(map(to_review, rows)),
            "confirmed": sum(t["verdict"] == "confirmed" for t in rows),
            "no_case": sum(to_review(t) and not t["case"] for t in rows),
            "last_run": (f"{last[:4]}-{last[4:6]}-{last[6:8]} "
                         f"{last[9:11]}:{last[11:13]}") if last else "never"}


def find(group, ref):
    return next((t for t in tenders(group) if t["reference"] == ref), None)


def record_verdict(group, ref, verdict, note=""):
    """Apply the auditor's verdict to the register and keep it in
    feedback.csv; says what happened ("skip: ..." when refused)."""
    kb = KB(group, KB_ROOT)
    said = apply_verdict(kb, kb.state(), ref, verdict, note)
    if not said.startswith(("skip", "already")):
        with io.open(kb.fb, "a", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=FB_COLS).writerow({
                "reference": ref, "verdict": verdict, "note": note,
                "reviewer": getpass.getuser(),
                "date": time.strftime("%Y-%m-%d")})
    return said


def document_names(ref):
    return [d["document"] for d in documents.tender_documents(ref)]


def document(ref, name):
    """One extracted document of a tender, as lines; None if it has none
    of that name."""
    for d in documents.tender_documents(ref):
        if d["document"] == name:
            return d["text"].splitlines()
    return None


def synth_runs():
    """The synthetic test's runs, newest first: [(stamp, summary path)]."""
    paths = sorted(glob.glob(os.path.join(SYNTH_RUNS, "*", "summary.txt")),
                   reverse=True)
    return [(os.path.basename(os.path.dirname(p)), p) for p in paths]


# ------------------------------------------------------------- the demo
def demo_runs():
    """The demo's runs (python tenderlens.py demo), newest first."""
    if not os.path.isdir(DEMO_DIR):
        return []
    return sorted((d for d in os.listdir(DEMO_DIR)
                   if os.path.exists(os.path.join(DEMO_DIR, d, "summary.txt"))),
                  reverse=True)


def demo(stamp):
    """One demo run: {setup: {reference: case}} and its summary text."""
    runs = {}
    for path in glob.glob(os.path.join(DEMO_DIR, stamp, "*_queue.json")):
        setup = os.path.basename(path)[:-len("_queue.json")]
        with open(path, encoding="utf-8") as f:
            runs[setup] = {c["reference"]: c for c in json.load(f)["cases"]}
    with open(os.path.join(DEMO_DIR, stamp, "summary.txt"),
              encoding="utf-8") as f:
        return runs, f.read()


# ------------------------------------------------- watching the agents
def _log(path):
    """Rows of a log file; NUL bytes left by a crash are dropped."""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return list(csv.DictReader(line.replace("\0", "") for line in f))


def agent_name(tag):
    return AGENTS.get(tag) or tag or "other (before calls were tagged)"


def _tokens(r):
    """Tokens a call paid: 0 when the answer came from the cache."""
    if r.get("cached") == "1":
        return 0
    try:
        return int(float(r.get("total_tokens") or 0))
    except ValueError:
        return 0


def usage():
    """Every model call (reports/llm_usage.csv), per agent and per day."""
    rows = _log(USAGE)
    per_agent = collections.defaultdict(collections.Counter)
    last = {}
    per_day = collections.defaultdict(collections.Counter)
    for r in rows:
        name = agent_name(r.get("tag"))
        a = per_agent[name]
        a["calls"] += 1
        a["cached"] += r.get("cached") == "1"
        a["tokens"] += _tokens(r)
        last[name] = max(last.get(name, ""), r.get("ts") or "")
        per_day[(r.get("ts") or "")[:10]][name] += _tokens(r)
    today = time.strftime("%Y-%m-%d")
    t = [r for r in rows if (r.get("ts") or "").startswith(today)]
    return {"agents": per_agent, "last": last, "days": per_day,
            "today": {"calls": len(t),
                      "cached": sum(r.get("cached") == "1" for r in t),
                      "groq_tokens": sum(_tokens(r) for r in t
                                         if r.get("provider") == "groq")}}


def errors(n=12):
    """Failed model calls (reports/llm_errors.csv): counts, the last n."""
    rows = _log(ERRORS)
    counts = collections.Counter((agent_name(r.get("tag")), r.get("error"))
                                 for r in rows)
    return counts, rows[-n:][::-1]


RE_M3 = re.compile(r"M3 agent \((\w+), (\d+) step\(s\), ([^;)]+)")


def agent_runs():
    """How the agents' investigations ended: every case logged in the
    knowledge bases, plus the latest demo run. Per agent: runs, steps,
    stop reasons."""
    cases = [e["case"] for g in groups() for e in KB(g, KB_ROOT).cases()[0]]
    if demo_runs():
        runs, _ = demo(demo_runs()[0])
        cases += [c for setup in runs.values() for c in setup.values()]
    stops = {"M2 agent": collections.Counter(),
             "M3 agent": collections.Counter()}
    steps = collections.Counter()
    for c in cases:
        a = (c.get("m2") or {}).get("agent") or {}
        if a.get("steps"):
            stops["M2 agent"][a.get("stop_reason") or "?"] += 1
            steps["M2 agent"] += a["steps"]
        m3 = (c.get("trace") or {}).get("M3") or []
        for line in [m3] if isinstance(m3, str) else m3:
            m = RE_M3.search(line)
            if m and int(m.group(2)):
                stops["M3 agent"][m.group(3).strip()] += 1
                steps["M3 agent"] += int(m.group(2))
    return {k: {"runs": sum(v.values()), "steps": steps[k], "stops": v}
            for k, v in stops.items()}
