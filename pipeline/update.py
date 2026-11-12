"""UPDATE - download the latest data and run the new tenders, in one command.

    python tenderlens.py update                   # every category with a knowledge base
    python tenderlens.py update --groups 2510
    python tenderlens.py update --groups 4014 --no-download   # start a new category
    python tenderlens.py update --agents          # step 4 with every agent (quota)

Steps, in order; the update stops at the first one that fails:
  1. download the notice and award files again (CanadaBuys republishes each in
     full every day). Each goes to a new file first and replaces the old one
     only if it looks complete, so a broken download changes nothing. The
     replaced file is kept in data/previous/.
  2. download the documents of the groups' tenders (files already on disk are
     skipped, so only new tenders' documents are fetched)
  3. extract their text (text already extracted is reused)
  4. run each group: only tenders not yet in its knowledge base are processed,
     oldest first (pipeline/stream.py). Rules only, with Wikidata online
     (free); with --agents also the M2 and M3 agents and M4's checks (the
     model's daily quota: a run that spends it stops, the next resumes).

Needs the network (Windows). To run it every week: README, "Weekly update".
"""

import argparse
import os
import subprocess
import sys

from agents.m1_screener import C_REF, TENDERS
from agents.m3_investigator import AWARDS, AWARDS_URL, fetch
from pipeline.stream import known_groups
from prepare.profile_tenders import FEED

PREVIOUS = "data/previous"


def refresh(path, url):
    """Download url as path's replacement. Kept only if its header has the
    reference column and it is at least 90% of the old file's size: the feed
    is republished in full, so it only grows."""
    new = path + ".new"
    if os.path.exists(new):
        os.remove(new)            # fetch() skips a file that is already there
    fetch(new, url)
    with open(new, encoding="utf-8-sig", errors="replace") as f:
        header = f.readline()
    old_size = os.path.getsize(path) if os.path.exists(path) else 0
    if C_REF not in header or os.path.getsize(new) < 0.9 * old_size:
        sys.exit(f"{url}: the download looks incomplete - {path} kept as it "
                 f"was ({new} left to inspect)")
    if old_size:
        os.makedirs(PREVIOUS, exist_ok=True)
        os.replace(path, os.path.join(PREVIOUS, os.path.basename(path)))
    os.replace(new, path)
    print(f"  {path} updated")


def step(*args):
    """One existing command, in its own process, as typed by hand."""
    print("\n>>> python tenderlens.py " + " ".join(args), flush=True)
    if subprocess.run([sys.executable, "tenderlens.py", *args]).returncode:
        sys.exit(f"update stopped: '{' '.join(args)}' failed")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--groups", default="",
                    help="UNSPSC-4 groups, comma separated (default: every "
                         "category that has a knowledge base)")
    ap.add_argument("--no-download", action="store_true",
                    help="skip step 1: use the notice and award files on disk")
    ap.add_argument("--agents", action="store_true",
                    help="run with every agent: --agent --m3-agent "
                         "--m4-checks (uses the model's daily quota)")
    a = ap.parse_args()
    groups = ([g.strip() for g in a.groups.split(",") if g.strip()]
              or known_groups())
    if not groups:
        sys.exit("no category yet: name one with --groups")
    if not a.no_download:
        refresh(TENDERS, FEED)
        refresh(AWARDS, AWARDS_URL)
    step("prepare", "docs", "--only", ",".join(groups), "--per-cat", "0")
    step("prepare", "text")
    for g in groups:
        step("run", "--group", g, "--online",
             *(["--agent", "--m3-agent", "--m4-checks"] if a.agents else []))


if __name__ == "__main__":
    main()
