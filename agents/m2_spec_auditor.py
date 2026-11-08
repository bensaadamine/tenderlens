"""
M2 SPEC AUDITOR - the agent.

Reads the specification of ONE tender with two tools and decides how far to
take each:

    tool 1  brands.brands_without_equivalent       detection level 1
    tool 2  level2.tight_requirements              detection level 2

    from agents.m2_spec_auditor import audit
    audit("cb-258-72766150", online=True, agent=True)

Every step it takes is written to `trace`, with the reason, so the auditor
can see what the agent did and why - not only what it concluded.

------------------------------------------------------------- the decisions

LEVEL 1 = rules, then the M2 agent. The rules find brand names and clear
the obvious cases; with `online` they check unknown names on Wikidata (free,
cached; reachable only from the Windows machine). What they leave open -
UNRESOLVED names, and their findings - goes to the M2 agent (`agent`),
which letters each one a-f with a verified quote. Without the agent an
unresolved name stays unresolved: neither a finding nor a clearance.

LEVEL 2 CONFIDENCE. Measured 2026-10-15 (see level2.py): the peer comparison
on UNSPSC-6 groups confuses vehicle CLASS with tightness - its top-ranked
"finding" was a semi-trailer required to carry 29,000 kg against pickup
trucks. So every level-2 peer finding leaves M2 marked confidence="low",
with its peer group attached, and the within-document test is reported
beside it as the level-2 signal that does not depend on peers.
"""


import argparse
import re
import json
import sys

from agents.evidence import Evidence, Finding
from tools import level2
from agents import m2_agent
from tools import brands
from tools import documents

# Rule results the agent looks at again: the rules' own findings, the names
# they could not settle, and the rule clearances listed here. Clearances
# backed by the text ("or equivalent", the general equivalence clause) stay
# with the rules, and so does the buyer's own equipment: re-checked by the
# agent, 3 correct clearances (Sellick, Fat Truck x2: "a trailer for
# transporting a Fat Truck") became false findings. Add "OK_BUYER_EQUIPMENT"
# to let the agent re-check them.
AGENT_RECHECK = {"OK_MAKE_LIST"}

# Two run choices arrive as parameters (pipeline/settings.py):
#   on_outage   the agent was asked for but no model answers (quota used,
#               providers down): "stop" raises AgentUnavailable, so a run
#               stops cleanly and the next run picks up the tenders not yet
#               done - a free daily quota then spreads one category over
#               several days. "keep" falls back to the rules' result (the
#               eval and the synthetic test use it).
#   agent_only  for the exam only (tests/m2_eval.py): (reference, name) ->
#               bool, which candidates the agent sees; the others keep the
#               rules' result. A tender can hold 50 open names of which one
#               is labelled. None = every candidate.


class AgentUnavailable(RuntimeError):
    """--agent was asked for, but no model could answer."""

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# Within-document test: a tender stating at least this share of its numeric
# requirements as two-sided bands, over at least MIN_REQS requirements.
# Measured on 2510: 2 of 133 tenders exceed 0.5, the median is 0.0 - so this
# is rare by construction and worth surfacing when it happens.
WDOC_SHARE = 0.5
WDOC_MIN_REQS = 5


def _llm_available():
    try:
        from tools import llm
        return bool(llm.available()) or llm.offline()   # offline: the cache answers
    except Exception:
        return False


def audit(reference, rows=None, online=False, agent=False, title="",
          level2_on=True, on_outage="stop"):
    """Audit ONE tender's specification.

    online: check names the brand cache does not know on Wikidata (free,
    cached). agent: let the M2 agent letter the names the rules found or
    could not settle (agents/m2_agent.py). title: the notice's title,
    shown to the agent as what the tender buys. level2_on: False skips
    level 2. on_outage: see above.
    Returns a dict with `readable`, `level1`, `level2`, `findings`
    (normalised, each with a source) and `trace`.
    """
    trace = []
    docs = documents.tender_documents(reference)
    if not docs:
        trace.append("no extracted text for this tender - nothing to read")
        return {"reference": reference, "readable": False, "findings": [],
                "level1": None, "level2": None, "trace": trace}
    trace.append(f"read {len(docs)} document(s)")
    l1 = run_level1(reference, online, agent, trace, title, on_outage)
    return _finish(reference, rows, l1, trace, docs, level2_on)


def run_level1(reference, online=False, agent=False, trace=None, title="",
               on_outage="stop", agent_only=None):
    """Level 1 for one tender: the rules (brand names checked against the
    Wikidata cache, or Wikidata itself when `online`), then, with `agent`,
    the M2 agent on what the rules found or could not settle. Returns
    brands.py's full result (findings, ok, unresolved), plus "agent" when
    it ran."""
    trace = [] if trace is None else trace
    # Online, every name is looked up again: fresh cache entries cost
    # nothing, and entries written under an older rule are re-checked
    # (measured 2026-10-17: two stale "is a brand" entries, "Dec" and
    # "Refer", raised watch cases).
    l1 = brands.brands_without_equivalent(reference, offline=not online)
    trace.append(f"level 1 ({'online' if online else 'offline'}): "
                 f"{len(l1.get('findings', []))} finding(s), "
                 f"{len(l1.get('unresolved', []))} unresolved")
    if l1.get("unresolved") and not agent:
        trace.append("unresolved names left as unresolved - neither a "
                     "finding nor a clearance (--online, --agent resolve)")
    if agent:
        if _llm_available():
            l1 = _agent_pass(reference, l1, online, trace, title,
                             on_outage, agent_only)
        elif on_outage == "stop":
            raise AgentUnavailable("no LLM key is set (GROQ_API_KEY, "
                                   "GEMINI_API_KEY in .env)")
        else:
            trace.append("--agent asked but no LLM key is set - rules only")
    return l1


def _agent_pass(reference, l1, online, trace, title="", on_outage="stop",
                agent_only=None):
    """Let the M2 agent letter what the rules found or could not settle."""
    recheck = [o for o in l1.get("ok", []) if o.get("status") in AGENT_RECHECK]
    rows = l1.get("findings", []) + l1.get("unresolved", []) + recheck
    held = []                       # left to the rules (agent_only)
    if agent_only is not None:
        held = [r for r in rows if not agent_only(reference, r["brand"])]
        rows = [r for r in rows if r not in held]
    if not rows:
        trace.append("agent: nothing for it to check")
        return l1
    cands = [{"name": r["brand"], "document": r["document"],
              "line_no": r["line_no"], "evidence": r["evidence"]}
             for r in rows]
    out = m2_agent.classify_tender(reference, cands, online=online,
                                   title=title)
    if on_outage == "stop" and out["stop_reason"].startswith("model"):
        raise AgentUnavailable(f"{reference}: {out['stop_reason']}")
    if out["steps"] == 0 and out["stop_reason"].startswith("model"):
        # The model never answered: keep the rules' result rather than
        # silently turning their findings into "unresolved".
        trace.append(f"agent could not start ({out['stop_reason']}) - "
                     f"rules' result kept")
        return l1
    findings, ok, unresolved = [], [o for o in l1.get("ok", [])
                                    if o not in recheck], []
    lettered = {v["id"]: v for v in out["verdicts"]}
    for i, r in enumerate(rows, 1):
        v = lettered.get(i)
        if v is None:
            unresolved.append(dict(r, decided_by="the M2 agent gave no "
                                                 "letter"))
            continue
        row = dict(r, document=v["document"], line_no=v["line"],
                   page=v["page"], evidence=v["quote"], agent_letter=v["letter"],
                   agent_reason=m2_agent.LETTERS[v["letter"]],
                   decided_by=f"the M2 agent: {m2_agent.LETTERS[v['letter']]}")
        if v["letter"] == "f":
            row["status"] = "BRAND_WITHOUT_EQUIVALENT"
            row["llm_reason"] = ("checked by the M2 agent: required, no "
                                 "equivalent accepted")
            findings.append(row)
        else:
            row["status"] = f"AGENT_{v['letter'].upper()}"
            ok.append(row)
    trace.append(f"agent ({out['engine']}, {out['steps']} step(s), "
                 f"{out['stop_reason']}; {_models(out)}): "
                 f"{len(rows)} candidate(s) -> "
                 f"{len(findings)} finding(s), "
                 f"{len(rows) - len(findings) - len(unresolved)} cleared, "
                 f"{len(unresolved)} unresolved")
    trace.extend("  agent " + t for t in out["trace"])
    for r in held:                  # as the rules left them
        (findings if r in l1.get("findings", []) else
         ok if r in recheck else unresolved).append(r)
    return dict(l1, findings=findings, ok=ok, unresolved=unresolved,
                agent={k: out.get(k) for k in ("steps", "stop_reason",
                                               "engine", "models")})


def _models(out):
    """Which model answered the agent, for the trace: "groq/openai/gpt-oss-
    120b x5" - two models in one result means a mixed (or replayed) run."""
    m = out.get("models") or {}
    return ", ".join(f"{k} x{v}" for k, v in sorted(m.items())) or \
        "no model call"


# One brand written several ways in a tender is one finding: a spelling
# joins a SHORTER finding whose words it begins with - "FormLabs Form" and
# "FormLabs From" join "Formlabs". Counted apart, they made two brands and an
# alert (Formlabs, demo 2026-11-08). "General Electric" and "General Motors"
# stay two findings: neither begins with the other. A merged spelling keeps
# its own place and words in the finding's evidence.


def _words(name):
    return re.findall(r"[a-z0-9]+", (name or "").lower())


def merge_spellings(findings):
    """Level-1 findings -> one per brand (see above). The kept row is the
    shortest spelling; the others go in also_written, and their locations
    and words first in its list of other locations."""
    hosts = []                         # (words, row), shortest names first
    for f in sorted(findings, key=lambda f: len(_words(f["brand"]))):
        w = _words(f["brand"])
        host = next((h for hw, h in hosts if w[:len(hw)] == hw), None)
        if host is None:
            hosts.append((w, dict(f)))
            continue
        host["also_written"] = host.get("also_written", []) + [f["brand"]]
        host["occurrences"] = (host.get("occurrences", 1)
                               + f.get("occurrences", 1))
        here = (f"{f.get('document')} p.{f.get('page')} l.{f.get('line_no')}"
                f" ('{f['brand']}': {str(f.get('evidence') or '')[:100]})")
        locs = host.get("locations") or [""]
        host["locations"] = locs[:1] + [here] + locs[1:]
    rows = [h for _w, h in hosts]
    order = {f["brand"]: i for i, f in enumerate(findings)}
    return sorted(rows, key=lambda r: order[r["brand"]])


def names_checked(l1):
    """Every brand name the tender raised and who settled it, for the case
    page: [{"name", "result", "decided_by"}]."""
    out = []
    for key, result in (("findings", "finding"), ("unresolved", "open"),
                        ("ok", "cleared"), ("dropped", "dropped")):
        for r in l1.get(key) or []:
            out.append({"name": r["brand"], "result": result,
                        "decided_by": r.get("decided_by", "")})
            out += [{"name": n, "result": "finding",
                     "decided_by": f"counted once with '{r['brand']}'"}
                    for n in r.get("also_written") or []]
    return out


def _check_sources(findings, docs, trace):
    """Mark each finding's source checked or not (M4 scores only checked
    ones)."""
    bad = 0
    for f in findings:
        s = f["source"]
        s["checked"] = bool(s.get("line")) and documents.quote_in_documents(
            docs, s.get("file"), s.get("line"))
        s["checked_how"] = ("words found in the tender's text by M2"
                            if s["checked"] else "words NOT found in the "
                            "tender's text")
        bad += bool(s.get("line")) and not s["checked"]
    if bad:
        trace.append(f"{bad} finding(s) quote words not found in the "
                     f"documents - they will not score")


def _finish(reference, rows, l1, trace, docs=None, level2_on=True):
    """Level 2, then every finding normalised with its source."""
    # ------------------------------------------------------------ level 2
    if not level2_on:
        l2 = {"off": True}
        trace.append("level 2: off (--no-level2)")
    else:
        if rows is None:
            rows = level2.load_observations()
        l2 = level2.tight_requirements(reference, _rows=rows)
    if l2.get("off"):
        pass
    elif l2.get("error"):
        trace.append(f"level 2: {l2['error']}")
    else:
        trace.append(f"level 2: {l2['score']} tight attribute(s) against "
                     f"peers, {len(l2['insufficient_peers'])} attribute(s) "
                     f"without enough peers")

    findings = []
    l1 = dict(l1, findings=merge_spellings(l1.get("findings", [])))

    for f in l1["findings"]:
        src = _l1_source(f)
        also = f.get("also_written") or []
        findings.append(Finding(
            kind="brand_without_equivalent", level=1,
            confidence="high" if f.get("llm_reason") else "medium",
            claim=f"Names '{f['brand']}'"
                  + (f" (also written {', '.join(repr(n) for n in also)})"
                     if also else "")
                  + f" without accepting an equivalent product "
                    f"({f.get('occurrences', 1)} mention(s))",
            brand=f["brand"], also_written=also,
            decided_by=f.get("decided_by", ""),
            detail=f.get("llm_reason") or "",
            source=src,
            # for M4's re-read: where it is, whether the agent lettered it
            line_no=f.get("line_no"), agent_letter=f.get("agent_letter"),
        ).to_dict())

    for f in (l2.get("findings") or []):
        findings.append(Finding(
            kind=f["kind"], level=2, confidence="low",
            claim=f"'{f['attribute']}' set at {f.get('stated')} against "
                  f"a peer median of {f['peer_median']} {f.get('unit','')} "
                  f"(n={f['peer_n']})",
            detail=f"Peer group is UNSPSC-{level2.LEVEL} - check the peers "
                   "are the same kind of product.",
            source=Evidence(file=f.get("file"), where=f.get("section"),
                            line=f.get("source_line"),
                            context=f.get("context")),
        ).to_dict())

    wd = (l2 or {}).get("within_document") or {}
    if wd.get("requirements", 0) >= WDOC_MIN_REQS and \
            wd.get("share", 0) >= WDOC_SHARE:
        findings.append(Finding(
            kind="within_document_bands", level=2, confidence="low",
            claim=f"{wd['two_sided']} of {wd['requirements']} numeric "
                  f"requirements are two-sided bands ({wd['share']:.0%}); "
                  f"most tenders state none",
            detail="Needs no peers. Rare by construction - 2 of 133 "
                   "tenders in 2510 exceed 50%.",
            # A count over the whole tender has no single line to cite.
            # M4 treats a finding without a document line as unsourced and
            # will not let it raise a case on its own - which is right for
            # a statistic about the document rather than a quote from it.
            source=Evidence(file=None, where="whole tender", line=None),
        ).to_dict())

    if docs is None:
        docs = documents.tender_documents(reference)
    _check_sources(findings, docs, trace)
    names = names_checked(l1)
    drop = [n for n in names if n["result"] == "dropped"]
    if drop:
        trace.append("dropped by the rules: " + "; ".join(
            f"{n['name']} ({n['decided_by']})" for n in drop[:20])
            + (f"; and {len(drop) - 20} more" if len(drop) > 20 else ""))
    return {"reference": reference, "readable": True,
            "level1": _slim_l1(l1), "level2": _slim_l2(l2),
            "findings": findings, "names": names, "trace": trace}


def _l1_source(f):
    """The document location of a level-1 finding.

    Field names are brands.py's own (brands_without_equivalent builds the
    row): `document`, `page`, `line_no`, `evidence` is the line itself, and
    `locations` lists every other place the brand appears.
    """
    return Evidence(file=f.get("document"),
                    where=f"page {f.get('page')}, line {f.get('line_no')}",
                    line=f.get("evidence"),
                    context=f.get("llm_quote") or None,
                    also_at=(f.get("locations") or [])[1:6])


def _slim_l1(l1):
    return {"findings": len(l1.get("findings", [])),
            "ok": len(l1.get("ok", [])),
            "unresolved": len(l1.get("unresolved", [])),
            "blanket_clause": l1.get("blanket_clause")}


def _slim_l2(l2):
    if (l2 or {}).get("off"):
        return {"off": True}
    if not l2 or l2.get("error"):
        return {"error": (l2 or {}).get("error")}
    return {"score": l2["score"], "verdict": l2["verdict"],
            "insufficient_peers": len(l2["insufficient_peers"]),
            "within_document": l2["within_document"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tender", required=True)
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--agent", action="store_true",
                    help="let the M2 agent letter the brand candidates")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = audit(a.tender, online=a.online, agent=a.agent)
    if a.json:
        print(json.dumps(res, indent=2, default=str))
        return
    print(f"\n{res['reference']}  readable={res['readable']}")
    for t in res["trace"]:
        print(f"  . {t}")
    for f in res["findings"]:
        print(f"\n  [L{f['level']} {f['confidence']}] {f['claim']}")
        s = f["source"] or {}
        if s.get("file"):
            print(f"      {s['file']}")
        if s.get("line"):
            print(f"      \"{str(s['line'])[:110]}\"")


if __name__ == "__main__":
    main()
