"""
The trash: a tender moved there leaves the base (its requirements no longer
count), the queues and the verdicts; Restore puts it back where it was;
Delete forever erases its lines and keeps one "deleted" line so runs do not
read it again. On a made-up knowledge base in a temporary folder.

    python -m tests.test_trash
"""

import copy
import io
import json
import os
import sys
import tempfile

from pipeline import stream
from tools import report

FAILS = 0
GROUP = "9999"


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def a_case(ref, route):
    """A case with every field the queue page needs."""
    return {"reference": ref, "title": f"Tender {ref}", "entity": "Defence",
            "method": "Competitive - Open bidding", "published": "2024-01-01",
            "route": route, "score": 0 if route == "close" else 5,
            "breakdown": [], "claims": [], "narrative": "", "readable": True,
            "award": {}, "findings": []}


def make_kb(root):
    kb = stream.KB(GROUP, root)
    for i, (ref, pile, route) in enumerate((("A", "base", "close"),
                                            ("B", "quarantine", "alert")), 1):
        run_id = f"20240101-000000-{i:04d}"
        kb.append_obs([{"run_id": run_id, "reference": ref,
                        "label": "length", "value": "10", "unit": "m"},
                       {"run_id": run_id, "reference": ref,
                        "label": "width", "value": "2", "unit": "m"}])
        kb.append_case(run_id, "20240101-000000", a_case(ref, route))
        kb.append_event(reference=ref, run_id=run_id, pub_date="2024-01-01",
                        event="processed", status=route, pile=pile,
                        score=0, observations=2)
    kb.append_unawarded({"reference": "A", "title": "A"})
    with io.open(kb.fb, "a", encoding="utf-8", newline="") as f:
        f.write("A,clean,,me,2024-01-02\n")
    cases = [e["case"] for e in kb.cases()[0]]
    report.write_outputs(copy.deepcopy(cases), {"scope": "test"},
                         os.path.join(kb.batches, "20240101-000000_queue"))
    return kb


def main():
    root = tempfile.mkdtemp()
    kb = make_kb(root)
    state = kb.state()
    check("A's requirements are in the base",
          "A" in kb.observations(state, "base"))

    check("trash: done", stream.to_trash(kb, state, "A", "test")
          == "moved to the trash")
    state = kb.state()
    check("trash: A's requirements leave the base",
          "A" not in kb.observations(state, "base"))
    check("trash: twice is refused",
          stream.to_trash(kb, state, "A").startswith("skip"))
    check("trash: a verdict on it is refused",
          stream.apply_verdict(kb, state, "A", "confirmed").startswith("skip"))
    check("trash: B is untouched", state["B"]["pile"] == "quarantine")

    check("restore: back to the base",
          stream.restore(kb, state, "A") == "restored -> base")
    state = kb.state()
    check("restore: A's requirements count again",
          len(kb.observations(state, "base")["A"]) == 2)
    check("restore: not in the trash any more",
          stream.restore(kb, state, "A").startswith("skip"))

    check("delete: refused outside the trash",
          stream.delete_forever(kb, state, "A").startswith("skip"))
    stream.to_trash(kb, state, "A")
    stream.restore(kb, kb.state(), "A")
    stream.to_trash(kb, kb.state(), "A")
    state = kb.state()
    check("trash twice, restore once: still restores to the base",
          [e["pile"] for e in kb.events("A")][-2] == "base")
    check("delete: done", stream.delete_forever(kb, state, "A") == "deleted")
    state = kb.state()
    check("delete: one 'deleted' line left in the register",
          [e["event"] for e in kb.events("A")] == ["deleted"])
    check("delete: a run will not read it again",
          state["A"]["status"] in stream.GONE)
    for path in (kb.obs, kb.case_log, kb.unawarded, kb.fb):
        with io.open(path, encoding="utf-8") as f:
            text = f.read()
        check(f"delete: A erased from {os.path.basename(path)}",
              '"A"' not in text and ",A," not in text
              and not text.startswith("A,") and "\nA," not in text)
    with io.open(os.path.join(kb.batches, "20240101-000000_queue.json"),
                 encoding="utf-8") as f:
        q = json.load(f)
    check("delete: A out of the batch queue, B kept",
          [c["reference"] for c in q["cases"]] == ["B"])
    check("delete: B still whole",
          len(kb.observations(state, "quarantine")["B"]) == 2
          and state["B"]["event"] == "processed")

    # the page's side
    from interface import data
    data.KB_ROOT = root
    data._logged.clear()
    check("page: B listed, A not", [t["reference"] for t in
                                     data.tenders(GROUP)] == ["B"])
    check("page: trash a tender", data.trash_action(GROUP, "B", "trash")
          == "moved to the trash")
    check("page: it shows in the trash", [t["reference"] for t in
                                          data.trashed()] == ["B"])
    check("page: and not in the family", data.tenders(GROUP) == [])
    check("page: its page still opens", data.find(GROUP, "B") is not None)
    check("page: restore", data.trash_action(GROUP, "B", "restore")
          == "restored -> quarantine")
    check("page: unknown action refused",
          data.trash_action(GROUP, "B", "burn").startswith("skip"))

    print(f"\n{FAILS} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
