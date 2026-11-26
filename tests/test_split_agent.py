"""
The split-purchase agent's rules, tested with a scripted fake model and a
small made-up award file - no network, no real data.

    python -m tests.test_split_agent
"""

import csv
import io
import os
import sys
import tempfile

from agents import split_agent as sa

FAILS = 0
C = sa.C
BUYER = "Public Works"
# (contract, date, value, method, title, supplier, category)
ROWS = [
    ("K1", "2024-01-05", "4,434.00", "Non-competitive",
     "EP916-240077 Workspaces Sole Source", "Teknion Limited", "*GD"),
    ("K2", "2024-01-05", "22289.10", "Non-competitive",
     "EP916-240077 Workspaces Sole Source", "TEKNION LIMITED", "*GD"),
    ("K3", "2024-03-01", "15000", "Non-competitive", "Office chairs",
     "Asokan", "*GD"),
    ("K4", "2024-03-20", "14000", "Non-competitive", "Lockers for Halifax",
     "Asokan", "*GD"),
    ("K5", "2024-03-25", "90000", "Competitive - Open bidding", "Desks",
     "Asokan", "*GD"),                                   # competitive: out
    ("K6", "2024-05-01", "20000", "Non-competitive", "Cleaning",
     "Clean Co", "*SRV"),                                # a service: out
    ("K7", "2024-05-02", "20000", "Non-competitive", "Cleaning",
     "Clean Co", "*SRV"),
    ("K8", "2024-06-01", "26000", "Non-competitive", "One big chair",
     "Big Co", "*GD"),                                   # over the limit
    ("K9", "2024-06-02", "1000", "Non-competitive", "One small chair",
     "Big Co", "*GD"),
    ("K12", "2023-01-10", "90000", "Competitive - Open bidding",
     "Sole Source chairs", "Teknion Limited", "*GD"),    # the pair's
    ("K13", "2023-05-10", "80000", "Competitive - Open bidding",
     "Sole Source desks", "Teknion Limited", "*GD"),     # other contracts
    ("K14", "2023-08-10", "70000", "Competitive - Open bidding",
     "Lockers", "Teknion Limited", "*GD"),
    ("K10", "2024-08-01", "13000", "Non-competitive", "Tables", "Late Co",
     "*GD"),
    ("K11", "2024-09-15", "13000", "Non-competitive", "Tables", "Late Co",
     "*GD"),                                             # 45 days apart
]


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def write_awards(path):
    cols = list(C.values())
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for k, d, v, m, t, s, cat in ROWS:
            w.writerow({C["contract"]: k, C["date"]: d, C["total"]: v,
                        C["method"]: m, C["title"]: t, C["supplier"]: s,
                        C["cat"]: cat, C["buyer"]: BUYER,
                        C["unspsc"]: "*56101500", C["amend"]: "0"})


def script(*replies):
    """A fake model: returns the replies in order."""
    it = iter(replies)
    return lambda system, user: next(it)


def main():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "awards.csv")
    write_awards(path)
    recs = sa.load_records(path)
    groups = sa.find_groups(recs)
    by_sup = {g["supplier_key"]: g for g in groups}
    index = sa.Index(recs)

    check("money: thousands comma, decimals", sa.money("4,434.00") == 4434.0
          and sa.money("1 234,50") == 1234.5 and sa.money("13,600") == 13600)
    check("the rule finds the two groups and only them",
          sorted(by_sup) == ["asokan", "teknion limited"])
    check("spellings of one supplier are one supplier",
          len(by_sup["teknion limited"]["records"]) == 2)
    check("a competitive award, a service, a contract over the limit and a "
          "gap over 30 days are not grouped",
          "clean co" not in by_sup and "big co" not in by_sup
          and "late co" not in by_sup)
    t, a = by_sup["teknion limited"], by_sup["asokan"]
    check("facts computed by code: same day, shared code, total",
          t["same_day"] and t["shared_codes"] == ["EP916-240077"]
          and abs(t["total"] - 26723.1) < 0.01)
    check("the rules' own verdict", sa.rule_verdict(t) == "split"
          and sa.rule_verdict(a) == "separate")

    out = sa.judge(t, index, ask=script(
        {"action": "history", "args": {}},
        {"action": "decide", "args": {"verdict": "split", "reason": "same "
         "project code, same day", "quote": "EP916-240077 Workspaces"}}),
        use_graph=False)
    check("split accepted with a quote found in two titles",
          out["verdict"] == "split" and out["steps"] == 2
          and out["stop_reason"] == "verdict given")

    out = sa.judge(a, index, ask=script(
        {"action": "decide", "args": {"verdict": "split", "reason": "x",
                                      "quote": "Office chairs"}},
        {"action": "decide", "args": {"verdict": "separate",
                                      "reason": "chairs vs lockers"}}),
        use_graph=False)
    check("split refused when the quote is in one title only; the model "
          "then says separate",
          out["verdict"] == "separate" and "refused" in out["trace"][0])

    out = sa.judge(t, index, ask=script(
        {"action": "decide", "args": {"verdict": "split", "reason": "x",
                                      "quote": "Sole Source"}},
        {"action": "decide", "args": {"verdict": "split", "reason": "x",
                                      "quote": "EP916-240077"}}),
        use_graph=False)
    check("a quote common in the pair's other contracts is refused; a "
          "reference code passes", out["verdict"] == "split"
          and "usually trade" in out["trace"][0])
    replies = [{"action": "decide", "args": {"verdict": "split",
                                             "reason": "x",
                                             "quote": "Sole Source"}}] * (
        sa.MAX_STEPS + 2)
    out = sa.judge(t, index, ask=script(*replies), use_graph=False)
    check("a split never backed by a specific quote ends as unclear",
          out["verdict"] == "unclear" and "unclear" in out["trace"][-1])
    out = sa.judge(a, index, ask=script(
        {"action": "json", "args": {"thought": "t", "action": "decide",
                                    "args": {"verdict": "separate",
                                             "reason": "different items"}}}),
        use_graph=False)
    check("a reply wrapped in a made-up 'json' tool is unwrapped",
          out["verdict"] == "separate" and out["steps"] == 1)

    out = sa.judge(a, index, ask=script(
        {"action": "decide", "args": {"verdict": "fraud", "reason": "x"}},
        {"action": "finish", "args": {}}), use_graph=False)
    check("an unknown verdict is refused; finish leaves no verdict",
          out["verdict"] == "" and out["stop_reason"] ==
          "finished by the model")

    replies = [{"action": "habit", "args": {}}] * (sa.MAX_STEPS - 1) + [
        {"action": "habit", "args": {}},
        {"verdict": "unclear", "reason": "cannot tell"}]
    out = sa.judge(a, index, ask=script(*replies), use_graph=False)
    check("last step: the final call without tools gives the verdict",
          out["verdict"] == "unclear" and out["steps"] == sa.MAX_STEPS)

    def down(system, user):
        raise RuntimeError("HTTP 429")
    out = sa.judge(a, index, ask=down, use_graph=False)
    check("model unavailable: no verdict, the reason is kept",
          out["verdict"] == "" and "429" in out["stop_reason"])

    check("detail shows one award; an unknown id is said",
          "Lockers for Halifax" in index.detail("K4")
          and "no award" in index.detail("K99"))
    check("habit counts this buyer's near-limit awards",
          "8 non-competitive" in index.habit(a) and "1 between" in
          index.habit(a) and "0 of them to this supplier" in index.habit(a))
    check("a split routes to watch, never alert",
          sa.route("split") == "watch" and sa.route("separate") == "close")

    vpath = os.path.join(tmp, "verdicts.csv")
    sa.save_verdict({"group": "g", "verdict": "split", "reason": "",
                     "quote": "", "decided_by": "auditor", "date": ""}, vpath)
    sa.save_verdict({"group": "g", "verdict": "separate", "reason": "",
                     "quote": "", "decided_by": "agent", "date": ""}, vpath)
    w = sa.by_who(vpath)["g"]
    check("the auditor's and the agent's verdicts are kept apart",
          w["auditor"]["verdict"] == "split"
          and w["agent"]["verdict"] == "separate")

    print(f"\n{FAILS} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
