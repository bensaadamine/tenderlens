"""
M2 SPEC AUDITOR - the agent.

Reads the specification of ONE tender with two tools and decides how far to
take each:

    tool 1  brands.brands_without_equivalent       detection level 1
    tool 2  level2.tight_requirements              detection level 2

    from m2_spec_auditor import audit
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

import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import repo  # noqa: E402  (agents/ tools/ prepare/ tests/ importable; works from the repo root)


import argparse
import re
import json
import sys

import level2
import m2_agent
import brands
import documents

# Rule results the agent looks at again: the rules' own findings, the names
# they could not settle, and the rule clearances listed here. Clearances
# backed by the text ("or equivalent", the general equivalence clause) stay
# with the rules. Buyer's equipment was in the list until 2026-10-22: the
# agent turned 3 correct clearances (Sellick, Fat Truck x2: "a trailer for
# transporting a Fat Truck") into false findings. Add "OK_BUYER_EQUIPMENT"
# back to restore.
AGENT_RECHECK = {"OK_MAKE_LIST"}
# For the exam only (tests/m2_eval.py): a test (reference, name) -> bool
# choosing which candidates the agent sees; the others keep the rules'
# result. A tender can hold 50 open names of which one is labelled, and the
# free quota (about 2-3 calls a minute) makes judging all of them take
# hours. None = every candidate (normal runs).
AGENT_ONLY = None

# When the agent was asked for but no model answers (quota used, providers
# down): "stop" raises AgentUnavailable, so a run stops cleanly and the
# tenders not yet done are picked up by the next run - a free daily quota
# then spreads one category over several days. "keep" falls back to the
# rules' result, as before 2026-10-23 (the eval and the synthetic test).
AGENT_DOWN = "stop"


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
        import llm
        return bool(llm.available())
    except Exception:
        return False


def audit(reference, rows=None, online=False, agent=False, title=""):
    """Audit ONE tender's specification.

    online: check names the brand cache does not know on Wikidata (free,
    cached). agent: let the M2 agent letter the names the rules found or
    could not settle (agents/m2_agent.py). title: the notice's title,
    shown to the agent as what the tender buys.
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
    l1 = run_level1(reference, online, agent, trace, title)
    return _finish(reference, rows, l1, trace, docs)


def run_level1(reference, online=False, agent=False, trace=None, title=""):
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
            l1 = _agent_pass(reference, l1, online, trace, title)
        elif AGENT_DOWN == "stop":
            raise AgentUnavailable("no LLM key is set (GROQ_API_KEY, "
                                   "GEMINI_API_KEY in .env)")
        else:
            trace.append("--agent asked but no LLM key is set - rules only")
    return l1


def _agent_pass(reference, l1, online, trace, title=""):
    """Let the M2 agent letter what the rules found or could not settle."""
    recheck = [o for o in l1.get("ok", []) if o.get("status") in AGENT_RECHECK]
    rows = l1.get("findings", []) + l1.get("unresolved", []) + recheck
    held = []                       # left to the rules (AGENT_ONLY)
    if AGENT_ONLY is not None:
        held = [r for r in rows if not AGENT_ONLY(reference, r["brand"])]
        rows = [r for r in rows if r not in held]
    if not rows:
        trace.append("agent: nothing for it to check")
        return l1
    cands = [{"name": r["brand"], "document": r["document"],
              "line_no": r["line_no"], "evidence": r["evidence"]}
             for r in rows]
    out = m2_agent.classify_tender(reference, cands, online=online,
                                   title=title)
    if AGENT_DOWN == "stop" and out["stop_reason"].startswith("model"):
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
                 f"{out['stop_reason']}): {len(rows)} candidate(s) -> "
                 f"{len(findings)} finding(s), "
                 f"{len(rows) - len(findings) - len(unresolved)} cleared, "
                 f"{len(unresolved)} unresolved")
    trace.extend("  agent " + t for t in out["trace"])
    for r in held:                  # as the rules left them
        (findings if r in l1.get("findings", []) else
         ok if r in recheck else unresolved).append(r)
    return dict(l1, findings=findings, ok=ok, unresolved=unresolved,
                agent={k: out[k] for k in ("steps", "stop_reason", "engine")})


# One brand written several ways in a tender ("Formlabs", "FormLabs Form")
# was counted as several brands - two brands make an alert (demo, 2026-11-08:
# Formlabs reached alert alone). Findings whose names share their first word
# are now one finding. The error this rule can make (two companies sharing a
# first word, both required) costs an alert, never adds one. None restores
# one finding per spelling.
SAME_BRAND = "first word"


def _first_word(name):
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return words[0] if words else (name or "").lower()


def merge_spellings(findings):
    """Level-1 findings -> one per brand. The kept row: lettered by the
    agent first, then the shortest name (the plainest spelling: "Formlabs",
    not "FormLabs Form"); the others go in also_written."""
    if SAME_BRAND is None:
        return findings
    groups = {}
    for f in findings:
        groups.setdefault(_first_word(f["brand"]), []).append(f)
    out = []
    for fs in groups.values():
        fs = sorted(fs, key=lambda f: (f.get("agent_letter") != "f",
                                       len(f["brand"])))
        main = dict(fs[0])
        if fs[1:]:
            main["also_written"] = [f["brand"] for f in fs[1:]]
            main["occurrences"] = sum(f.get("occurrences", 1) for f in fs)
        out.append(main)
    return out


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
    ones - audit finding 2026-11-01)."""
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


def _finish(reference, rows, l1, trace, docs=None):
    """Level 2, then every finding normalised with its source."""
    # ------------------------------------------------------------ level 2
    if not level2.ON[0]:
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
        findings.append({
            "level": 1, "kind": "brand_without_equivalent",
            "confidence": "high" if f.get("llm_reason") else "medium",
            "claim": f"Names '{f['brand']}'"
                     + (f" (also written {', '.join(repr(n) for n in also)})"
                        if also else "")
                     + f" without accepting an equivalent product "
                       f"({f.get('occurrences', 1)} mention(s))",
            "brand": f["brand"],
            "also_written": also,
            "decided_by": f.get("decided_by", ""),
            "detail": f.get("llm_reason") or "",
            "source": src,
            # for the M4 agent, which can hand the finding back to the M2
            # agent: where it is, and whether that agent already lettered it
            "line_no": f.get("line_no"),
            "agent_letter": f.get("agent_letter"),
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
