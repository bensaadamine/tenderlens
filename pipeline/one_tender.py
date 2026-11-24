"""
ONE TENDER - the four components on one tender.

run_one(ref) is the chain for ONE tender; pipeline/stream.py calls it for
every new tender against the knowledge base (the normal way to run the
system); tools/report.py writes the ranked queue (HTML, CSV, JSON).

From the command line, one tender with every agent's reasoning:

    python tenderlens.py tender cb-258-72766150 [--online --agent ...]

---------------------------------------------------------------- the chain

    M1 screen(notice)  ->  route + signals         (notice only, free)
    M2 audit(spec)     ->  level 1 + level 2       (documents)
    M3 investigate     ->  award, buyer->supplier  (award notices)
    M4 build_case      ->  alert / watch / close   (fuses, verifies, scores)

THE ONE ORCHESTRATION DECISION: M1 never decides WHETHER a readable tender
is read. M1 sees only the notice, and a specification rigged inside an OPEN
competition - the case level 1 exists for - looks perfectly ordinary on the
notice. Letting M1 close it would make the system blind to its own target.
So every tender with documents gets M2.

Each component works on one tender; batching belongs to the tools, so
the shared data - notices, awards - is loaded once here and handed to each
component. Level 2's peers come from the knowledge base
(pipeline/stream.py: the run builds them; with_peers for one tender).
"""


import argparse
import sys
import time

from tools import brands
from agents import m1_screener
from agents import m2_spec_auditor
from agents import m3_investigator
from agents import m4_case_builder
from agents import m4_checks
from pipeline import settings
from pipeline.settings import Settings

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

def load_shared(verbose=True):
    """(notices, method windows, level-2 peers - none yet, awards)."""
    t = time.time()
    notices = m1_screener.load_notices()
    wstats = m1_screener.method_windows(notices)
    if verbose:
        print(f"notices: {len(notices):,} ({time.time()-t:.0f}s)")
    graph = m3_investigator.load_graph()
    if verbose:
        print(f"awards: "
              f"{'%s rows' % format(graph['rows'], ',') if graph else 'NOT LOADED (run m3_investigator.py --fetch)'}")
    return notices, wstats, [], graph


# Tenders that never became a contract (user decision 2026-10-22): cancelled,
# still open, or a request for information / supply-arrangement
# qualification. "After the award only": there is nothing to audit, so M3 is
# skipped and the case is closed; M1 and M2 still read it, so it can join the
# knowledge base.
NO_CONTRACT_TYPES = {"request for information", "request for supply arrangement"}


# User decision 2026-11-24: an advance contract award notice names its
# supplier in advance and gives other suppliers time to object; once awarded
# to that supplier, no objection succeeded, and the auditor has nothing to add.
ACAN_REASON = ("advance contract award notice: the supplier was announced in "
               "advance and other suppliers could object")


def no_contract_reason(row):
    """Why this tender cannot have a contract, or None."""
    row = row or {}
    status = (row.get(m1_screener.C_STATUS) or "").strip().lower()
    ntype = (row.get(m1_screener.C_NOTICE) or "").strip().lower()
    if status == "cancelled":
        return "the tender was cancelled"
    if status == "open":
        return "the tender is still open"
    if ntype in NO_CONTRACT_TYPES:
        return f"it is a {ntype}, which does not lead to a contract"
    return None


# Which tenders are audit cases (user decision 2026-11-21): only a tender
# that was AWARDED (its row in the award file) and whose DOCUMENTS we hold
# goes through M1-M4 and can reach the queue. Decided from the notice row,
# the award file and the list of extracted files - before any text is read
# or any model is asked. The others are only recorded:
GATE = {
    "process": "awarded, with documents: an audit case",
    "not_audit": "not an audit case",          # cancelled, RFI, supply arrangement
    "still_open": "still open: checked again on every run",
    "no_documents": "no documents to read",
    "award_not_found": "award not found: to check by hand",
}


def gate(ref, row, graph):
    """(one of GATE, why). Cheap: no text is read."""
    why = no_contract_reason(row)
    if why == "the tender is still open":
        return "still_open", why
    if why:
        return "not_audit", why
    if m1_screener.is_acan(row):
        return "not_audit", ACAN_REASON
    if not m1_screener.has_documents(ref):
        return "no_documents", "no documents held for this tender"
    recs, via = m3_investigator.find_award(ref, row, graph)
    if not recs:
        return ("award_not_found", "closed, documents held, but no award "
                "notice matches it in the award file")
    return "process", f"award found via {via}"


# Raised when --agent is on and no model answers; a run stops on them and
# the next run resumes from that tender.
AGENT_DOWN = (m2_spec_auditor.AgentUnavailable,
              m3_investigator.AgentUnavailable,
              m4_checks.AgentUnavailable)


def run_one(ref, shared, s=None):
    """The four components on one tender, with one run's settings
    (pipeline/settings.py; default: rules only). s.m4_checks: None (M4
    rules only), "fixed" (M4's checks before ranking a case with a brand
    finding, agents/m4_checks.py)."""
    s = s or Settings()
    notices, wstats, rows, graph = shared
    m1 = m1_screener.screen(ref, notices, wstats)
    why_none = no_contract_reason(notices.get(ref))
    if m1.get("documents"):
        m2 = m2_spec_auditor.audit(ref, rows=rows, online=s.online,
                                   agent=s.agent, title=m1.get("title", ""),
                                   level2_on=s.level2,
                                   on_outage=s.on_outage)
    else:
        m2 = {"reference": ref, "readable": False, "findings": [],
              "trace": ["no documents held for this tender"]}
    if why_none:
        m3 = {"reference": ref, "available": False, "award": None,
              "findings": [], "trace": [f"skipped - {why_none}"]}
    else:
        m3 = m3_investigator.investigate(ref, notices.get(ref), graph,
                                         agent=s.m3_agent,
                                         on_outage=s.on_outage)
    if s.m4_checks and not why_none:
        hooks = m4_checks.default_hooks(ref, notices.get(ref), graph, m3,
                                        s.online, s.on_outage)
        case = m4_checks.check_case(ref, m1, m2, m3, hooks)
    else:
        case = m4_case_builder.build_case(m1, m2, m3)
    case["m1_route"] = m1.get("route")
    if why_none:
        case["route"] = "close"
        case["no_contract"] = why_none
        case["narrative"] = (f"Not an audit case: {why_none}, so there is no "
                             f"contract to audit. Read only to enrich the "
                             f"knowledge base.\n\n" + case["narrative"])
    elif m1_screener.is_acan(notices.get(ref)):
        case["route"] = "close"
        case["not_audit"] = ACAN_REASON
        case["narrative"] = (f"Not an audit case: {ACAN_REASON}.\n\n"
                             + case["narrative"])
    return case


def select(notices, group=None, refs=None):
    if refs:
        return [r for r in refs if r in notices]
    out = []
    for ref, row in notices.items():
        code = m3_investigator.first_unspsc4(row.get(m1_screener.C_UNSPSC))
        if group and code != group:
            continue
        out.append(ref)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser(description="One tender, with every agent's "
                                 "reasoning. Whole categories: "
                                 "python tenderlens.py run.")
    ap.add_argument("tender", help="reference number, e.g. cb-258-72766150")
    settings.add_args(ap)
    a = ap.parse_args()
    s = Settings.from_args(a)

    from pipeline import stream      # here: stream imports this module
    shared = load_shared()
    if s.level2:                     # peers: the knowledge base as it is now
        shared = stream.with_peers(shared, a.tender)
    try:
        c = run_one(a.tender, shared, s)
    except AGENT_DOWN as e:
        print(f"\nstopped: {e}")
        return 1
    print(f"\n{c['reference']}  ->  {c['route'].upper()}  "
          f"(score {c['score']}, M1 {c['m1_route']})\n")
    print(c["narrative"])
    print()
    for x in c["claims"]:
        mark = "+" if x["verified"] else "?"
        print(f" {mark} [{x.get('agent')} {x['kind']} {x.get('points', 0)}pt] "
              f"{x['claim'][:110]}")
        if x.get("not_scored_because"):
            print(f"      not scored: {x['not_scored_because']}")
    print()
    for k in ("M1", "M2", "M3", "M4"):
        v = c["trace"].get(k)
        for line in ([v] if isinstance(v, str) else v or []):
            print(f"  {k}: {line}")


if __name__ == "__main__":
    main()
