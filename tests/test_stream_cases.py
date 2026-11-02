"""
The incremental run's case log (tenderlens_stream.py), tested on a
temporary knowledge base - no data, no network. Audit finding 2026-11-01:
a crash after a tender was registered left it skipped on the next run with
no case anywhere. Checks: a case is written before its register event, a
line cut by a crash is skipped, and the queue picks the case the register
points to.

    python tests/test_stream_cases.py
"""

import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import repo  # noqa: E402  (agents/ tools/ prepare/ tests/ importable; works from the repo root)

import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace

import tenderlens_stream as ts

FAILS = 0


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def case(ref, route, score):
    return {"reference": ref, "route": route, "score": score, "claims": [],
            "m1_route": "standard", "readable": True, "title": ref,
            "entity": "", "narrative": "", "award": None, "method": "",
            "published": "", "trace": {}}


def main():
    root = tempfile.mkdtemp()
    kb = ts.KB("9999", root)
    # batch 1: t-1 and t-2 done; t-3's case written, then a crash before
    # its register event
    for i, (ref, route) in enumerate([("t-1", "watch"), ("t-2", "close"),
                                      ("t-3", "alert")], 1):
        rid = f"B1-{i:04d}"
        kb.append_case(rid, "B1", case(ref, route, i))
        if ref != "t-3":
            kb.append_event(reference=ref, run_id=rid, event="processed",
                            status=route, pile="quarantine"
                            if route == "watch" else "base")
    with io.open(kb.case_log, "a", encoding="utf-8") as f:
        f.write('{"run_id": "B1-0004", "batch": "B1", "case": {"refer')
    # batch 2: t-3 processed again after the restart
    kb.append_case("B2-0001", "B2", case("t-3", "watch", 7))
    kb.append_event(reference="t-3", run_id="B2-0001", event="processed",
                    status="watch", pile="quarantine")

    logged, bad = kb.cases()
    check("a line cut by a crash is skipped, the rest is read",
          bad == 1 and len(logged) == 4)

    args = SimpleNamespace(group="9999", kb_root=root, queue="current")
    ts.queue(args)
    with io.open(os.path.join(kb.dir, "current_queue.json"),
                 encoding="utf-8") as f:
        cur = {c["reference"]: c for c in json.load(f)["cases"]}
    check("current queue: one case per registered tender",
          sorted(cur) == ["t-1", "t-2", "t-3"])
    check("the case that counts is the one the register points to",
          cur["t-3"]["score"] == 7 and cur["t-3"]["route"] == "watch")
    check("each case carries its current pile",
          cur["t-1"]["pile"] == "quarantine" and cur["t-2"]["pile"] == "base")

    ts.queue(SimpleNamespace(group="9999", kb_root=root, queue="B1"))
    with io.open(os.path.join(kb.batches, "B1_queue.json"),
                 encoding="utf-8") as f:
        b1 = json.load(f)["cases"]
    check("a batch's queue is rebuilt from the log, crashed tender included",
          sorted(c["reference"] for c in b1) == ["t-1", "t-2", "t-3"])

    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
