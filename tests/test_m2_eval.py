"""
The M2 exam's bookkeeping, tested offline: held-out rows without a letter
are not scored, sets are reported apart, precision and recall are counted
on findings, and a run never overwrites another's evidence.

    python tests/test_m2_eval.py
"""

import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import repo  # noqa: E402  (agents/ tools/ prepare/ tests/ importable; works from the repo root)

import io
import os
import sys
import tempfile

import m2_eval

FAILS = 0


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def row(split, want, got):
    return {"split": split, "expected": want, "got": got,
            "letter_right": want == got,
            "finding_right": (got == "f") == (want == "f")}


def main():
    tmp = tempfile.mkdtemp()
    m2_eval.LABELS = os.path.join(tmp, "labels.csv")
    m2_eval.HOLDOUT = os.path.join(tmp, "holdout.csv")
    with io.open(m2_eval.LABELS, "w", encoding="utf-8") as f:
        f.write("reference,name,letter,source,split\n"
                "t-1,Bendix,f,hand,tuning\nt-2,Net,a,hand,tuning\n")
    with io.open(m2_eval.HOLDOUT, "w", encoding="utf-8") as f:
        f.write("reference,name,letter,source,split\n"
                "t-3,Conviron,F,sample,holdout\nt-4,Suite,,sample,holdout\n")
    labs = m2_eval.load_labels("all")
    check("held-out rows without a letter are not scored; letters lowercased",
          [(l["reference"], l["letter"]) for l in labs]
          == [("t-1", "f"), ("t-2", "a"), ("t-3", "f")])
    check("one set can be chosen",
          [l["reference"] for l in m2_eval.load_labels("holdout")] == ["t-3"])

    s = m2_eval.score([row("h", "f", "f"), row("h", "f", "unresolved"),
                       row("h", "a", "f"), row("h", "a", "a")])
    check("precision and recall are counted on findings",
          s["precision"] == 0.5 and s["recall"] == 0.5
          and s["finding_right"] == 2 and s["letter_right"] == 2)
    check("no finding: precision is n/a, not 0 or 100%",
          m2_eval.score([row("h", "a", "a")])["precision"] is None)

    text = m2_eval.summary_text(
        [row("tuning", "f", "f"), row("holdout", "a", "f")], "rules",
        {"stamp": "s", "commit": "c", "settings": {}})
    check("the two sets are reported apart, tuning marked as flattered",
          "TUNING - 1 cases (flattered" in text
          and "HOLDOUT - 1 cases" in text)

    m2_eval.OUT_DIR = os.path.join(tmp, "out")
    d1, _ = m2_eval.run_dir("rules", False)
    d2, _ = m2_eval.run_dir("rules", False)
    d3, _ = m2_eval.run_dir("rules", True)
    check("a run never overwrites another, even in the same second",
          len({d1, d2, d3}) == 3 and "_partial" in d3)

    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
