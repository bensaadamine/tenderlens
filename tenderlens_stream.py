"""TenderLens, incremental: one tender at a time against a growing knowledge base.

    python tenderlens_stream.py --group 2510                 # every new tender
    python tenderlens_stream.py --group 2510 --until 2024-12-31   # simulate an early batch
    python tenderlens_stream.py --group 2510 --status
    python tenderlens_stream.py --group 2510 --feedback      # apply the auditor's verdicts
    python tenderlens_stream.py --group 2510 --export-labels # raw labels for prepare/05_attribute_map.py
    python tenderlens_stream.py --group 2510 --queue         # queue of every tender's latest case
    python tenderlens_stream.py --group 2510 --queue 20261101-103000   # one batch's queue again

The batch run (tenderlens.py) is unchanged and still available.

HOW A RUN WORKS
  New tenders = notices of the group not yet in the register (plus tenders
  registered without documents whose text has since been extracted).
  They are processed in PUBLICATION-DATE order. For each one:
    1. extract its numeric requirements (prepare/04_census.py's own steps: section
       scoping, number parsing, band collapse, unit conversion, restatements)
    2. run the four agents, with the BASE as level 2's peers - so a tender
       is compared only with tenders processed before it
    3. alert or watch -> QUARANTINE (kept, never used as peers)
       close          -> BASE (becomes a peer for every later tender)
  The first run is the same loop starting from an empty base.

  Medians and spreads are computed on read, so adding a tender to the base
  IS the update. Level 2 stays silent for an attribute until it has 8 peers.

STORAGE  reports/kb/<group>/  - append-only, so a crash loses at most the
  tender being processed:
    observations.csv  every requirement extracted, RAW label, tagged run_id
    register.csv      one event per line; a tender's state = its last event.
                      pile = base | quarantine | none
    feedback.csv      filled in by the auditor: reference, verdict, note
                        verdict clean     -> back to the base
                        verdict confirmed -> stays out; a real test case
    cases.jsonl       every case as built, one JSON line, tagged run_id and
                      batch; written BEFORE the tender's register event, so
                      a crash never leaves a tender processed without its
                      case. The case that counts is the one whose run_id is
                      the tender's in the register.
    batches/<stamp>_queue.{html,csv,json}  ranked queue of each run,
                      rebuilt from cases.jsonl by --queue STAMP
    current_queue.{html,csv,json}  every tender's latest case, with its
                      current pile (--queue)

  Base and quarantine are two piles decided by the register, not two files:
  moving a tender is one appended line, never a rewrite.

  The attribute map is applied ON READ, so labels 05 learns later improve
  the base retroactively. Labels not yet in the map are kept as written.
"""

import repo  # noqa: F401  (agents/ tools/ prepare/ tests/ importable; works from the repo root)

import argparse
import collections
import csv
import io
import json
import os
import re
import sys
import time

import level2
import m1_screener
import m3_investigator
import m2_spec_auditor
import m4_case_builder
import brands
import documents
import tenderlens

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

census = repo.census()

MAP_PATH = "reports/attribute_map.json"
OBS_COLS = ["run_id", "reference", "unspsc8", "pub_date", "label",
            "raw_label", "label_src", "comparator", "comp_src", "value",
            "value_text", "unit", "unit_resolved", "dimension", "base_value",
            "base_unit", "value_lo", "value_hi", "base_lo", "base_hi",
            "band_width", "restated", "also_stated_as", "soft_unit",
            "part_number_line", "scope", "file", "line_no", "section",
            "source_line", "context"]
REG_COLS = ["at", "reference", "run_id", "pub_date", "event", "status",
            "pile", "score", "observations", "note"]
FB_COLS = ["reference", "verdict", "note", "reviewer", "date"]
QUARANTINE_ROUTES = {"alert", "watch"}
# Audit finding 2026-11-01: cases were written only at the end of a batch,
# while each tender was registered as processed as soon as it was done - a
# crash left those tenders skipped on the next run with no case anywhere.
# Each case now goes to cases.jsonl before its register event. False
# restores the old behaviour (no case log).
CASE_LOG = True


# ------------------------------------------------------------------ storage
class KB:
    def __init__(self, group, root="reports/kb"):
        self.dir = os.path.join(root, group)
        self.obs = os.path.join(self.dir, "observations.csv")
        self.reg = os.path.join(self.dir, "register.csv")
        self.fb = os.path.join(self.dir, "feedback.csv")
        self.case_log = os.path.join(self.dir, "cases.jsonl")
        self.batches = os.path.join(self.dir, "batches")
        os.makedirs(self.batches, exist_ok=True)
        for path, cols in ((self.obs, OBS_COLS), (self.reg, REG_COLS),
                           (self.fb, FB_COLS)):
            if not os.path.exists(path):
                with io.open(path, "w", encoding="utf-8", newline="") as f:
                    csv.writer(f).writerow(cols)

    def state(self):
        """reference -> its last register event."""
        st = {}
        with io.open(self.reg, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                st[r["reference"]] = r
        return st

    def append_obs(self, rows):
        with io.open(self.obs, "a", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=OBS_COLS, extrasaction="ignore")
            for r in rows:
                w.writerow(r)

    def append_event(self, **ev):
        ev.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
        with io.open(self.reg, "a", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=REG_COLS,
                           extrasaction="ignore").writerow(ev)

    def append_case(self, run_id, batch, case):
        line = json.dumps({"run_id": run_id, "batch": batch, "case": case},
                          ensure_ascii=False, default=str)
        # A line cut by a crash has no newline; without this the next case
        # would be glued onto it and lost with it.
        cut = False
        if os.path.exists(self.case_log) and os.path.getsize(self.case_log):
            with io.open(self.case_log, "rb") as f:
                f.seek(-1, os.SEEK_END)
                cut = f.read(1) != b"\n"
        with io.open(self.case_log, "a", encoding="utf-8") as f:
            f.write(("\n" if cut else "") + line + "\n")

    def cases(self):
        """Every logged case, in order. A line cut by a crash is skipped."""
        out, bad = [], 0
        if not os.path.exists(self.case_log):
            return out, bad
        with io.open(self.case_log, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    bad += 1
        return out, bad

    def observations(self, state, pile):
        """Current-run rows of every tender now in `pile`, raw labels."""
        keep = {ref: ev["run_id"] for ref, ev in state.items()
                if ev["pile"] == pile}
        out = collections.defaultdict(list)
        with io.open(self.obs, encoding="utf-8", errors="replace",
                     newline="") as f:
            for r in csv.DictReader(f):
                if keep.get(r["reference"]) == r["run_id"]:
                    out[r["reference"]].append(r)
        return out


# ----------------------------------------------------------- per-tender work
def load_map(path=MAP_PATH):
    if not os.path.exists(path):
        print(f"  no {path} - labels used as written")
        return {}
    with io.open(path, encoding="utf-8") as f:
        m = json.load(f)
    return m.get("map", m)


def _text(v):
    """What csv would store - so a fresh row and a re-read row are identical."""
    return "" if v is None else str(v)


def notice_meta(row):
    raw = row.get(m1_screener.C_UNSPSC) or ""
    code = ""
    for t in re.split(r"[\r\n,;|\s]+", raw):
        d = re.sub(r"\D", "", t)
        if len(d) >= 4:
            code = d
            break
    return code[:8], (row.get(m1_screener.C_PUB) or "").strip()[:10]


def extract_tender(ref, row, group, run_id):
    """Raw observations for one tender, through prepare/04_census.py's own steps."""
    docs = documents.tender_documents(ref)
    rows = []
    for d in docs:
        got = census.extract(d["text"])
        for r in got:
            r["reference"] = ref
            r["file"] = f"{ref}__{d['document']}.txt"
        rows.extend(got)
    if rows:
        rows, _ = census.collapse_bands(rows)
        census.convert_units(rows)
        rows, _ = census.fold_restatements(rows)
    u8, date = notice_meta(row)
    out = []
    for r in rows:
        r["unspsc8"] = u8 or group
        r["pub_date"] = date
        r["run_id"] = run_id
        out.append({c: _text(r.get(c)) for c in OBS_COLS})
    return docs, out


def comparable(rows, amap):
    """Raw rows of ONE tender -> level-2 rows: map, drop 'unknown', dedup."""
    out = []
    for r in rows:
        r = dict(r)
        canon = amap.get(r["label"])
        if canon == "unknown":
            continue
        if canon:
            r["label"] = canon
        out.append(r)
    out = census.dedup(out, 8)
    return level2.prepare(out)


def base_rows(kb, state, amap):
    rows = []
    for ref, rs in kb.observations(state, "base").items():
        rows.extend(comparable(rs, amap))
    return rows


# ------------------------------------------------------------------ commands
def todo_list(notices, group, state, until=None):
    todo = []
    for ref in tenderlens.select(notices, group):
        ev = state.get(ref)
        if ev and not ((ev["status"] == "no_documents"
                        and documents.tender_documents(ref))
                       or ev["status"] == "still_open"):
            continue
        _, date = notice_meta(notices[ref])
        if until and (not date or date > until):
            continue
        todo.append((date or "9999", ref))
    return [ref for _, ref in sorted(todo)]


def reprocess_list(kb, notices, group, state, which):
    """Tenders to run again (audit finding 2026-11-01: a tender processed
    before its award notice existed, or under older rules or without the
    agents, was never looked at again). which: tender references, or
    "quarantine" / "all". A tender the auditor has judged keeps the
    auditor's verdict and is not re-run."""
    judged = set()
    with io.open(kb.reg, encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            if r["event"] == "feedback":
                judged.add(r["reference"])
    if "all" in which:
        refs = [r for r in state if r in notices]
    elif "quarantine" in which:
        refs = [r for r, ev in state.items() if ev["pile"] == "quarantine"]
    else:
        refs = list(which)
    out, skipped, unknown = [], [], []
    for ref in refs:
        if ref not in notices or tenderlens.select({ref: notices[ref]},
                                                   group) != [ref]:
            unknown.append(ref)
        elif ref in judged:
            skipped.append(ref)
        else:
            out.append((notice_meta(notices[ref])[1] or "9999", ref))
    if skipped:
        print(f"  not re-run - judged by the auditor: {', '.join(skipped)}")
    if unknown:
        print(f"  not in category {group}: {', '.join(unknown)}")
    return [ref for _, ref in sorted(out)]


def run(a):
    kb = KB(a.group, a.kb_root)
    level2.LEVEL[0] = a.l2_level
    amap = load_map()
    notices, wstats, _, graph = tenderlens.load_shared()
    state = kb.state()
    todo = todo_list(notices, a.group, state, a.until)
    if a.reprocess:
        again = reprocess_list(kb, notices, a.group, state, a.reprocess)
        todo += [r for r in again if r not in todo]
        print(f"  re-run: {len(again)} tender(s)")
    if a.limit:
        todo = todo[:a.limit]
    base = base_rows(kb, state, amap)
    n_base = len({r["reference"] for r in base})
    print(f"knowledge base: {n_base} tender(s) in base, "
          f"{sum(1 for e in state.values() if e['pile'] == 'quarantine')} "
          f"in quarantine | new tenders to process: {len(todo)}")
    if not todo:
        return

    stamp = time.strftime("%Y%m%d-%H%M%S")
    # The batch's settings, written before any tender: every case and event
    # carries the stamp, so what produced it can always be told (audit
    # finding 2026-11-01).
    with io.open(os.path.join(kb.batches, f"{stamp}_settings.json"), "w",
                 encoding="utf-8") as f:
        json.dump({"stamp": stamp, "argv": sys.argv[1:],
                   "settings": {k: v for k, v in vars(a).items()
                                if isinstance(v, (str, int, float, bool,
                                                  list, type(None)))},
                   "reprocessed": list(a.reprocess or []),
                   "native_tools": os.environ.get("TENDERLENS_NATIVE_TOOLS",
                                                  "on"),
                   "local_model": os.environ.get("TENDERLENS_LOCAL_MODEL")},
                  f, indent=1)
    cases, piles = [], collections.Counter()
    for i, ref in enumerate(todo, 1):
        run_id = f"{stamp}-{i:04d}"
        row = notices[ref]
        docs, raw = extract_tender(ref, row, a.group, run_id)
        mine = comparable(raw, amap)
        peers = [r for r in base if r["reference"] != ref] + mine
        try:
            case = tenderlens.run_one(ref, (notices, wstats, peers, graph),
                                      online=a.online, agent=a.agent,
                                      m3_agent=a.m3_agent,
                                      m4_mode=a.m4_checks)
        except tenderlens.AGENT_DOWN as e:
            # Nothing written for this tender: the next run starts here.
            # Answers already given are cached, so nothing is paid twice.
            print(f"\nstopped at {i}/{len(todo)}: the agents have no model "
                  f"({e}).\n  Run the same command again later (daily quotas "
                  f"reset); it continues from this tender.")
            break
        route = case["route"]
        if not docs:
            status = "no_documents"
        elif case.get("no_contract") == "the tender is still open":
            status = "still_open"        # read again in the next batch
        else:
            status = route
        pile = ("quarantine" if route in QUARANTINE_ROUTES
                else "base" if docs else "none")
        case["pile"] = pile
        case["peers_at_time"] = len({r["reference"] for r in base})
        cases.append(case)
        piles[pile] += 1

        kb.append_obs(raw)               # observations first,
        if CASE_LOG:                     # then the case,
            kb.append_case(run_id, stamp, case)
        kb.append_event(reference=ref, run_id=run_id,   # then the event
                        pub_date=notice_meta(row)[1], event="processed",
                        status=status, pile=pile, score=case["score"],
                        observations=len(raw))
        # a re-run tender may leave the base: drop its old rows either way
        base = [r for r in base if r["reference"] != ref] + (
            mine if pile == "base" else [])
        if i % 25 == 0:
            print(f"  {i:4d}/{len(todo)}  base now {len({r['reference'] for r in base})}")

    if not cases:
        return
    meta = {"scope": f"UNSPSC-4 {a.group}, incremental batch {stamp}",
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "tenders": len(cases), "awards": graph is not None,
            "agent": a.agent, "m4_checks": a.m4_checks,
            "weights": m4_case_builder.WEIGHTS,
            "alert_at": m4_case_builder.ALERT,
            "watch_at": m4_case_builder.WATCH}
    prefix = os.path.join(kb.batches, f"{stamp}_queue")
    tenderlens.write_outputs(cases, meta, prefix)
    routes = collections.Counter(c["route"] for c in cases)
    print(f"\nbatch {stamp}: {len(cases)} tender(s) - "
          f"{routes['alert']} alert, {routes['watch']} watch, "
          f"{routes['close']} close")
    print(f"  -> base {piles['base']}, quarantine {piles['quarantine']}, "
          f"no documents {piles['none']}")
    print(f"  queue: {prefix}.html")


def queue(a):
    """Write a queue from cases.jsonl: one batch (STAMP), or every
    tender's latest case with its current pile ("current")."""
    kb = KB(a.group, a.kb_root)
    logged, bad = kb.cases()
    if bad:
        print(f"  {bad} unreadable line(s) in {kb.case_log} skipped "
              f"(cut by a crash)")
    if a.queue == "current":
        state = kb.state()
        pick = {}
        for e in logged:                 # the case the register points to
            ev = state.get(e["case"]["reference"])
            if ev and ev["run_id"] == e["run_id"]:
                pick[e["case"]["reference"]] = dict(
                    e["case"], pile=ev["pile"], status=ev["status"])
        cases = list(pick.values())
        prefix = os.path.join(kb.dir, "current_queue")
        scope = f"UNSPSC-4 {a.group}, every tender's latest case"
    else:
        cases = [e["case"] for e in logged if e["batch"] == a.queue]
        prefix = os.path.join(kb.batches, f"{a.queue}_queue")
        scope = f"UNSPSC-4 {a.group}, incremental batch {a.queue}"
    if not cases:
        print(f"no logged case for '{a.queue}' in {kb.case_log}")
        return
    meta = {"scope": scope, "generated": time.strftime("%Y-%m-%d %H:%M"),
            "tenders": len(cases), "rebuilt_from": kb.case_log,
            "weights": m4_case_builder.WEIGHTS,
            "alert_at": m4_case_builder.ALERT,
            "watch_at": m4_case_builder.WATCH}
    tenderlens.write_outputs(cases, meta, prefix)
    routes = collections.Counter(c["route"] for c in cases)
    print(f"{len(cases)} case(s): {routes['alert']} alert, "
          f"{routes['watch']} watch, {routes['close']} close")
    print(f"  queue: {prefix}.html")


def feedback(a):
    kb = KB(a.group, a.kb_root)
    state = kb.state()
    applied = skipped = 0
    with io.open(kb.fb, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for fb in rows:
        ref = (fb.get("reference") or "").strip()
        verdict = (fb.get("verdict") or "").strip().lower()
        ev = state.get(ref)
        if verdict not in ("clean", "confirmed"):
            print(f"  skip {ref}: verdict must be 'clean' or 'confirmed'")
            skipped += 1
            continue
        if not ev:
            print(f"  skip {ref}: not in the register")
            skipped += 1
            continue
        status = "reviewed_clean" if verdict == "clean" else "confirmed"
        if ev["status"] == status:
            continue                       # already applied
        if ev["pile"] != "quarantine" and verdict == "clean":
            print(f"  skip {ref}: not in quarantine (pile={ev['pile']})")
            skipped += 1
            continue
        had_docs = ev["status"] != "no_documents"
        pile = ("quarantine" if verdict == "confirmed"
                else "base" if had_docs else "none")
        kb.append_event(reference=ref, run_id=ev["run_id"],
                        pub_date=ev["pub_date"], event="feedback",
                        status=status, pile=pile, score=ev["score"],
                        observations=ev["observations"],
                        note=(fb.get("note") or "")[:200])
        state[ref] = {**ev, "status": status, "pile": pile}
        applied += 1
        print(f"  {ref}: {verdict} -> {pile}")
    print(f"feedback: {applied} applied, {skipped} skipped")


def status(a):
    kb = KB(a.group, a.kb_root)
    level2.LEVEL[0] = a.l2_level
    state = kb.state()
    c = collections.Counter((e["pile"], e["status"]) for e in state.values())
    print(f"register: {len(state)} tender(s)")
    for (pile, st), n in sorted(c.items()):
        print(f"  {pile:11} {st:15} {n}")
    base = base_rows(kb, state, load_map())
    per_key = collections.defaultdict(set)
    per_fam = collections.defaultdict(set)
    for r in base:
        per_key[r["_key"]].add(r["reference"])
        per_fam[r["_key"][0]].add(r["reference"])
    ready = sorted(((len(v), k) for k, v in per_key.items()
                    if len(v) >= level2.MIN_PEERS), reverse=True)
    print(f"\nbase: {len({r['reference'] for r in base})} tender(s) with "
          f"comparable requirements, {len(per_fam)} peer group(s) "
          f"(UNSPSC-{level2.LEVEL[0]})")
    print(f"  groups with >= {level2.MIN_PEERS} tenders: "
          f"{sum(1 for v in per_fam.values() if len(v) >= level2.MIN_PEERS)}")
    print(f"  attributes that can be judged (>= {level2.MIN_PEERS} peers): "
          f"{len(ready)}")
    for n, (fam, dim, attr) in ready[:15]:
        print(f"    {fam} {attr:28} {n}")


def export_labels(a):
    """Raw rows of every registered tender, as prepare/05_attribute_map.py --census reads them."""
    kb = KB(a.group, a.kb_root)
    state = kb.state()
    out = os.path.join(kb.dir, "census_raw.csv")
    n = 0
    with io.open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OBS_COLS, extrasaction="ignore")
        w.writeheader()
        for pile in ("base", "quarantine"):
            for rs in kb.observations(state, pile).values():
                w.writerows(rs)
                n += len(rs)
    print(f"{n:,} rows -> {out}")
    print(f"next: python prepare/05_attribute_map.py --group {a.group} --census {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--group", required=True, help="UNSPSC-4 group, e.g. 2510")
    ap.add_argument("--until", help="only tenders published on or before "
                    "this date (YYYY-MM-DD) - to simulate an earlier batch")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--agent", action="store_true",
                    help="let the M2 agent letter brand candidates a-f "
                         "(LLM, Windows)")
    ap.add_argument("--m3-agent", action="store_true",
                    help="let the M3 agent decide which other spellings of "
                         "the winner are the same supplier (LLM, Windows)")
    ap.add_argument("--m4-checks", nargs="?", const="fixed",
                    choices=["fixed", "model"],
                    help="before ranking a case with a brand finding, M4 "
                         "sends the finding back to the M2 agent and the "
                         "winner's spellings to the M3 agent (LLM, Windows). "
                         "'model': a model chooses the checks (experiment, "
                         "measured worse)")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--l2-level", type=int, choices=[6, 8], default=8)
    ap.add_argument("--kb-root", default="reports/kb",
                    help="where knowledge bases live (one folder per group)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--feedback", action="store_true")
    ap.add_argument("--export-labels", action="store_true")
    ap.add_argument("--reprocess", nargs="+", metavar="REF",
                    help="run these tenders again with today's code and "
                         "settings: references, 'quarantine' or 'all'. "
                         "Tenders the auditor judged are kept as judged")
    ap.add_argument("--queue", nargs="?", const="current", metavar="STAMP",
                    help="write a queue from cases.jsonl: every tender's "
                         "latest case (default), or one batch by its stamp")
    brands.add_l1_args(ap)
    a = ap.parse_args()
    brands.apply_l1_args(a)
    if a.status:
        status(a)
    elif a.feedback:
        feedback(a)
    elif a.export_labels:
        export_labels(a)
    elif a.queue:
        queue(a)
    else:
        run(a)


if __name__ == "__main__":
    main()
