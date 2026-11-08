"""
TENDERLENS - the one command. Every part of the system starts here.

    python tenderlens.py demo [--offline]          7 chosen tenders, 3 setups side by side
    python tenderlens.py tender <ref> [--online --agent --m3-agent --m4-checks]
                                                   one tender, every component's reasoning
    python tenderlens.py run --group 2510 [--online --agent ...]
                                                   a category, tender by tender, against its
                                                   knowledge base (--status, --feedback, --queue)
    python tenderlens.py test                      every offline test (no data, no network)
    python tenderlens.py eval m2|m4|synth [...]    the measurements
    python tenderlens.py awards --fetch            download the award notices (once)
    python tenderlens.py prepare profile|docs|text|census|labels [...]
                                                   data preparation, in that order
    python tenderlens.py llm [--usage]             the pinned models; tokens spent

Add -h after any command for its options. A single module also runs on its
own from the repository root: python -m tools.brands --tender <ref>

The command always works from the repository root (data/ and reports/ are
read and written there), whatever folder it is started from.
"""

import glob
import importlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

COMMANDS = {
    "demo": "pipeline.demo",
    "tender": "pipeline.one_tender",
    "run": "pipeline.stream",
    "awards": "agents.m3_investigator",
    "llm": "tools.llm",
}
EVALS = {"m2": "tests.m2_eval", "m4": "tests.m4_eval",
         "synth": "tests.synth_rig"}
PREPARE = {"profile": "prepare.profile_tenders", "docs": "prepare.fetch_docs",
           "text": "prepare.extract_text", "census": "prepare.census",
           "labels": "prepare.attribute_map"}


def run_tests():
    """Each test file in its own process (they exit with their result)."""
    failed = []
    for path in sorted(glob.glob(os.path.join("tests", "test_*.py"))):
        name = os.path.basename(path)[:-3]
        r = subprocess.run([sys.executable, "-m", f"tests.{name}"],
                           capture_output=True, text=True)
        last = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
        print(f"  {name:28s} {last}")
        if r.returncode:
            failed.append(name)
            print("\n".join("      " + l for l in (r.stdout + r.stderr)
                            .splitlines() if l.startswith(("FAIL", "Trace",
                                                           "  File", "E"))))
    print(f"\n{len(failed)} test file(s) failed" + (f": {', '.join(failed)}"
                                                    if failed else ""))
    return 1 if failed else 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    os.chdir(ROOT)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "test":
        return run_tests()
    table, label = COMMANDS, cmd
    if cmd in ("eval", "prepare"):
        table = EVALS if cmd == "eval" else PREPARE
        if not rest or rest[0] not in table:
            print(f"usage: python tenderlens.py {cmd} {'|'.join(table)} ...")
            return 2
        label, rest = f"{cmd} {rest[0]}", rest[1:]
        cmd = label.split()[1]
    if cmd not in table:
        print(f"unknown command '{argv[0]}'\n{__doc__}")
        return 2
    module = importlib.import_module(table[cmd])
    sys.argv = [f"tenderlens.py {label}"] + rest
    return module.main() or 0


if __name__ == "__main__":
    sys.exit(main())
