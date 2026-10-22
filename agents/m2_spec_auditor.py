"""
M2 SPEC AUDITOR - the agent.

Reads the specification of ONE tender with two tools and decides how far to
take each:

    tool 1  spec_tools.brands_without_equivalent   detection level 1
    tool 2  level2.tight_requirements              detection level 2

    from m2_spec_auditor import audit
    audit("cb-258-72766150", depth="full")

Every step it takes is written to `trace`, with the reason, so the auditor
can see what the agent did and why - not only what it concluded.

------------------------------------------------------------- the decisions

DEPTH comes from M1 and gates only what costs TOKENS. "standard" runs both
tools on rules alone. "full" also lets level 1 ask the LLM to judge each
suspected lock-in in context (the step that clears "Leica products (or
equivalent)"). M1 decides which tenders are worth that spend; M2 never
decides it for itself.

LEVEL 1 RETRY, gated by `online` alone. If the rules leave brands
UNRESOLVED - candidates the offline cache cannot confirm or reject - M2
re-runs the tool against Wikidata. That is free and cached for every later
tender, so it is not tied to depth. It IS required in practice: the cache
was built on 4110 and resolves 0 of 129 candidates on a 2510 tender. An
unresolved brand is not a finding and not a clearance; leaving it there
silently would be both. Wikidata, like CanadaBuys and Groq, is reachable
only from the Windows machine.

LEVEL 2 CONFIDENCE. Measured 2026-10-15 (see level2.py): the peer comparison
on UNSPSC-6 groups confuses vehicle CLASS with tightness - its top-ranked
"finding" was a semi-trailer required to carry 29,000 kg against pickup
trucks. So every level-2 peer finding leaves M2 marked confidence="low",
with its peer group attached, and the within-document test is reported
beside it as the level-2 signal that does not depend on peers.
"""

# --- repo layout: find the agents/, tools/, prepare/ folders and run from
# the repository root, whatever folder the script sits in (2026-10-22).
import os as _os
import sys as _sys
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
for _d in ("agents", "tools", "prepare", "tests", ""):
    _p = _os.path.join(_ROOT, _d)
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
if __name__ == "__main__":
    _os.chdir(_ROOT)
# ---


import argparse
import json
import sys

import level2
import m2_agent
import spec_tools

# Rule results the agent looks at again: the rules' own findings, the names
# they could not settle, and the rule clearances listed here. Clearances
# backed by the text ("or equivalent", the general equivalence clause) stay
# with the rules. Buyer's equipment was in the list until 2026-10-22: the
# agent turned 3 correct clearances (Sellick, Fat Truck x2: "a trailer for
# transporting a Fat Truck") into false findings. Add "OK_BUYER_EQUIPMENT"
# back to restore.
AGENT_RECHECK = {"OK_MAKE_LIST"}

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
        import llm
        return bool(llm.available())
    except Exception:
        return False


def audit(reference, depth="standard", rows=None, online=False,
          agent=False):
    """Audit ONE tender's specification.

    depth: "standard" (rules, offline) or "full" (adds LLM judgement and,
    if `online`, live brand verification). agent: let the M2 agent letter
    the brand candidates (agents/m2_agent.py) instead of the LLM judge.
    Returns a dict with `readable`, `level1`, `level2`, `findings`
    (normalised, each with a source) and `trace`.
    """
    trace = []
    docs = spec_tools.tender_documents(reference)
    if not docs:
        trace.append("no extracted text for this tender - nothing to read")
        return {"reference": reference, "readable": False, "findings": [],
                "level1": None, "level2": None, "trace": trace}
    trace.append(f"read {len(docs)} document(s)")
    l1 = run_level1(reference, depth, online, agent, trace)
    return _finish(reference, depth, rows, l1, trace)


def run_level1(reference, depth="standard", online=False, agent=False,
               trace=None):
    """Level 1 for one tender: rules, then Wikidata (online), then either
    the LLM judge (depth full/auto) or the M2 agent. Returns spec_tools'
    full result (findings, ok, unresolved), plus "agent" when it ran."""
    trace = [] if trace is None else trace
    if agent:
        depth = "standard"        # the agent replaces the judge, not both
    use_llm = depth == "full" and _llm_available()
    if depth == "full" and not use_llm:
        trace.append("depth=full requested but no LLM key is set - level 1 "
                     "runs on rules only")

    # ------------------------------------------------------------ level 1
    l1 = spec_tools.brands_without_equivalent(
        reference, offline=True, use_llm=use_llm)
    trace.append(f"level 1 (offline{', LLM judge' if use_llm else ''}): "
                 f"{len(l1.get('findings', []))} finding(s), "
                 f"{len(l1.get('unresolved', []))} unresolved")

    # Online verification is free (Wikidata) and cached for every later
    # tender, so it is gated by --online alone, not by depth. Depth gates
    # only what costs tokens. Measured: the brand cache was built on 4110,
    # so offline it resolves 0 of 129 candidates on a 2510 tender - level 1
    # without --online finds nothing.
    # With --online, ALWAYS take the online pass: fresh cache entries cost
    # nothing, and entries written under an older verification rule are
    # re-checked. Measured 2026-10-17: gating this on "unresolved" let two
    # stale "is a brand" entries ("Dec", "Refer") raise watch cases.
    if online:
        trace.append("re-running level 1 with online verification")
        l1 = spec_tools.brands_without_equivalent(
            reference, offline=False, use_llm=use_llm)
        trace.append(f"level 1 (online): {len(l1.get('findings', []))} "
                     f"finding(s), {len(l1.get('unresolved', []))} "
                     f"still unresolved")
    elif l1.get("unresolved"):
        trace.append("unresolved brands left as unresolved - neither a "
                     "finding nor a clearance (run with --online to "
                     "resolve)")

    # depth "auto" (user decision 2026-10-17): the LLM is used on ANY tender
    # whose rules leave names Wikidata could not settle, or a brand finding
    # to judge - not only on tenders M1 routed deep. Before this, a rigged
    # OPEN tender naming a brand Wikidata does not know could never be
    # caught (synthetic: 2 of 46 such brands found). Tokens are spent only
    # where the rules leave something open.
    if depth == "auto" and (l1.get("unresolved") or l1.get("findings")):
        if _llm_available():
            trace.append("names left open by the rules - asking the LLM "
                         "(triage, then judge)")
            l1 = spec_tools.brands_without_equivalent(
                reference, offline=not online, use_llm=True)
            trace.append(f"level 1 (LLM): {len(l1.get('findings', []))} "
                         f"finding(s), {len(l1.get('unresolved', []))} "
                         f"still unresolved")
        else:
            trace.append("--llm asked but no LLM key is set - left as is")

    if agent:
        if _llm_available():
            l1 = _agent_pass(reference, l1, online, trace)
        else:
            trace.append("--agent asked but no LLM key is set - rules only")
    return l1


def _agent_pass(reference, l1, online, trace):
    """Let the M2 agent letter what the rules found or could not settle."""
    recheck = [o for o in l1.get("ok", []) if o.get("status") in AGENT_RECHECK]
    rows = l1.get("findings", []) + l1.get("unresolved", []) + recheck
    if not rows:
        trace.append("agent: nothing for it to check")
        return l1
    cands = [{"name": r["brand"], "document": r["document"],
              "line_no": r["line_no"], "evidence": r["evidence"]}
             for r in rows]
    out = m2_agent.classify_tender(reference, cands, online=online)
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
            unresolved.append(r)
            continue
        row = dict(r, document=v["document"], line_no=v["line"],
                   page=v["page"], evidence=v["quote"], agent_letter=v["letter"],
                   agent_reason=m2_agent.LETTERS[v["letter"]])
        if v["letter"] == "f":
            row["status"] = "BRAND_WITHOUT_EQUIVALENT"
            row["llm_reason"] = ("checked by the M2 agent: required, no "
                                 "equivalent accepted")
            findings.append(row)
        else:
            row["status"] = f"AGENT_{v['letter'].upper()}"
            ok.append(row)
    trace.append(f"agent ({out['engine']}, {out['steps']} step(s), "
                 f"{out['stop_reason']}): {len(rows)} candidate(s) -> "
                 f"{len(findings)} finding(s), "
                 f"{len(rows) - len(findings) - len(unresolved)} cleared, "
                 f"{len(unresolved)} unresolved")
    trace.extend("  agent " + t for t in out["trace"])
    return dict(l1, findings=findings, ok=ok, unresolved=unresolved,
                agent={k: out[k] for k in ("steps", "stop_reason", "engine")})


def _finish(reference, depth, rows, l1, trace):
    """Level 2, then every finding normalised with its source."""
    # ------------------------------------------------------------ level 2
    if rows is None:
        rows = level2.load_observations()
    l2 = level2.tight_requirements(reference, _rows=rows)
    if l2.get("error"):
        trace.append(f"level 2: {l2['error']}")
    else:
        trace.append(f"level 2: {l2['score']} tight attribute(s) against "
                     f"peers, {len(l2['insufficient_peers'])} attribute(s) "
                     f"without enough peers")

    findings = []

    for f in l1.get("findings", []):
        src = _l1_source(f)
        findings.append({
            "level": 1, "kind": "brand_without_equivalent",
            "confidence": "high" if f.get("llm_reason") else "medium",
            "claim": f"Names '{f['brand']}' without accepting an equivalent "
                     f"product ({f.get('occurrences', 1)} mention(s))",
            "brand": f["brand"],
            "detail": f.get("llm_reason") or "",
            "source": src,
        })

    for f in (l2.get("findings") or []):
        findings.append({
            "level": 2, "kind": f["kind"], "confidence": "low",
            "claim": f"'{f['attribute']}' set at {f.get('stated')} against "
                     f"a peer median of {f['peer_median']} {f.get('unit','')} "
                     f"(n={f['peer_n']})",
            "detail": f"Peer group is UNSPSC-{level2.LEVEL[0]}"
                      + ("; it mixes vehicle classes" if level2.LEVEL[0] < 8
                         else "") + " - check the peers are the same kind "
                      "of product.",
            "source": {"file": f.get("file"), "where": f.get("section"),
                       "line": f.get("source_line"),
                       "context": f.get("context")},
        })

    wd = (l2 or {}).get("within_document") or {}
    if wd.get("requirements", 0) >= WDOC_MIN_REQS and \
            wd.get("share", 0) >= WDOC_SHARE:
        findings.append({
            "level": 2, "kind": "within_document_bands", "confidence": "low",
            "claim": f"{wd['two_sided']} of {wd['requirements']} numeric "
                     f"requirements are two-sided bands ({wd['share']:.0%}); "
                     f"most tenders state none",
            "detail": "Needs no peers. Rare by construction - 2 of 133 "
                      "tenders in 2510 exceed 50%.",
            # A count over the whole tender has no single line to cite.
            # M4 treats a finding without a document line as unsourced and
            # will not let it raise a case on its own - which is right for
            # a statistic about the document rather than a quote from it.
            "source": {"file": None, "where": "whole tender",
                       "line": None, "context": None},
        })

    return {"reference": reference, "readable": True, "depth": depth,
            "level1": _slim_l1(l1), "level2": _slim_l2(l2),
            "findings": findings, "trace": trace}


def _l1_source(f):
    """The document location of a level-1 finding.

    Field names are spec_tools' own (brands_without_equivalent builds the
    row): `document`, `page`, `line_no`, `evidence` is the line itself, and
    `locations` lists every other place the brand appears.
    """
    return {"file": f.get("document"),
            "where": f"page {f.get('page')}, line {f.get('line_no')}",
            "line": f.get("evidence"),
            "context": f.get("llm_quote") or None,
            "also_at": (f.get("locations") or [])[1:6]}


def _slim_l1(l1):
    return {"findings": len(l1.get("findings", [])),
            "ok": len(l1.get("ok", [])),
            "unresolved": len(l1.get("unresolved", [])),
            "blanket_clause": l1.get("blanket_clause")}


def _slim_l2(l2):
    if not l2 or l2.get("error"):
        return {"error": (l2 or {}).get("error")}
    return {"score": l2["score"], "verdict": l2["verdict"],
            "insufficient_peers": len(l2["insufficient_peers"]),
            "within_document": l2["within_document"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tender", required=True)
    ap.add_argument("--depth", choices=["standard", "full", "auto"],
                    default="standard")
    ap.add_argument("--online", action="store_true")
    ap.add_argument("--agent", action="store_true",
                    help="let the M2 agent letter the brand candidates")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = audit(a.tender, depth=a.depth, online=a.online, agent=a.agent)
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
