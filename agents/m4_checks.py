"""
M4 CHECKS - before a case with a brand finding is ranked, three checks run
in a fixed order and the rules re-score the case after them:

    1. re-read   every brand finding the M2 agent has not lettered yet goes
                 back to the M2 agent; it stays only on letter "f"
    2. spellings the M3 agent checks the winner's other spellings in the
                 award notices (skipped when --m3-agent already did); the
                 brand may be the winner's under another name
    3. awards    titles of the winner's other awards that mention each
                 brand - shown to the auditor, never scored

A tender without a brand finding is never sent to a model. Scoring and
routing stay in m4_case_builder.build_case; the checks add no claim and set
no points. What was checked is written on the case page ("Checked again
before ranking").

Why a fixed order, not a model choosing (tests/m4_eval.py, 73 labelled
tenders, M2 on rules only, 2026-11-04):

    checks    flagged   real lock-ins flagged   false alarms   tokens
    none         9            2 of 12                7            0
    fixed        2            2 of 12                0          70k
    model        4            2 of 12                2          14k

The model saved tokens by skipping the re-read on two tenders, and their
false alarms stayed. Its version is kept, measured and not chosen:
experiments/m4_model_coordinator.py (--m4-checks model).

With the M2 agent on (--agent) every finding is already lettered, so check 1
has nothing to do: the re-read matters when M2 runs on rules alone (the
cheap setup).

    from agents.m4_checks import check_case, default_hooks
    case = check_case(ref, m1, m2, m3, default_hooks(ref, row, graph, m3))
"""


import copy

from agents import m4_case_builder as m4

AWARDS_SHOWN = 4
BRAND = "brand_without_equivalent"
# Same letters as m2_agent.LETTERS (tests check it), worded for the page.
LETTERS = {
    "a": "not a brand",
    "b": "a bidder is speaking",
    "c": "the buyer's own equipment",
    "d": "one of several brands listed",
    "e": "an equivalent is accepted",
    "f": "brand required, no equivalent",
}


class AgentUnavailable(RuntimeError):
    """--m4-checks was asked for, but no model could answer."""


class Hooks:
    """The follow-ups for one tender. Tests pass fakes.

    recheck(cands)      -> m2_agent.classify_tender's result
    spellings()         -> a new M3 result counting the winner's spellings
    winner_awards(text, keys) -> {"count": n, "lines": [...]}
    on_outage           no model answering: "stop" raises AgentUnavailable
                        (the run stops and resumes from this tender, as for
                        M2/M3); "keep" ranks the case as it stands
    """

    def __init__(self, recheck=None, spellings=None, winner_awards=None,
                 on_outage="stop"):
        self.recheck = recheck
        self.spellings = spellings
        self.winner_awards = winner_awards
        self.on_outage = on_outage


def default_hooks(reference, notice_row, graph, m3, online=False,
                  on_outage="stop"):
    """The real follow-ups: the M2 agent, the M3 agent, the award index."""
    from agents import m1_screener
    from agents import m2_agent
    from agents import m3_investigator as m3i
    key = ((m3 or {}).get("award") or {}).get("supplier_key")

    def recheck(cands):
        return m2_agent.classify_tender(
            reference, cands, online=online,
            title=(notice_row or {}).get(m1_screener.C_TITLE, ""))

    def spellings():
        trace = []
        same = m3i._agent_aliases(key, graph, trace, on_outage=on_outage)
        out = m3i.investigate(reference, notice_row, graph, spellings=same)
        out["trace"] = trace + out["trace"]
        return out

    own_sol = m3i.norm_sol((notice_row or {}).get(m3i.C_SOL_TENDER))

    def winner_awards(text, keys):
        needle = text.lower()
        recs = [r for n in [key] + list(keys or [])
                for r in graph["by_sup"].get(n, [])
                if needle in (r.get("title") or "").lower()
                and r.get("ref") != reference
                and not (own_sol and r.get("sol") == own_sol)]
        recs.sort(key=lambda r: r.get("date") or "", reverse=True)
        return {"count": len(recs),
                "lines": [f"{r.get('date') or '?'} | "
                          f"{(r.get('buyer_name') or '')[:40]} | "
                          f"{(r.get('title') or '')[:80]}"
                          for r in recs[:AWARDS_SHOWN]]}

    ok = bool(graph and key)
    return Hooks(recheck, spellings if ok else None,
                 winner_awards if ok else None, on_outage)


# ---------------------------------------------------------- what is open
def _brand_findings(m2):
    return [f for f in (m2 or {}).get("findings", [])
            if f.get("kind") == BRAND and m4._verified(f.get("source"))]


def _winner(m3):
    return ((m3 or {}).get("award") or {}).get("supplier")


def _recheckable(m2, hooks, done=()):
    """Brand findings the M2 agent has not lettered yet."""
    if not hooks.recheck:
        return []
    out = []
    for f in _brand_findings(m2):
        b = f.get("brand") or ""
        if not f.get("agent_letter") and b.lower() not in done \
                and b not in out:
            out.append(b)
    return out


def _can_spell(m2, m3, case, hooks):
    """Worth asking: a winner, a brand finding left, spellings not yet
    checked, and the brand not already matched to the winner."""
    return bool(hooks.spellings and _winner(m3)
                and not (m3 or {}).get("agent")
                and _brand_findings(m2)
                and not any(c["kind"] == "brand_matches_winner"
                            for c in case["claims"]))


def _searchable(m2, m3, hooks, done=()):
    """Brands worth looking for in the winner's other award titles."""
    if not (hooks.winner_awards and _winner(m3)):
        return []
    return [f["brand"] for f in _brand_findings(m2)
            if f["brand"].lower() not in done]


# ------------------------------------------------------------- the checks
def _recheck(reference, m2, hooks, brands):
    """Hand the findings back to the M2 agent; apply its letters."""
    m2 = copy.deepcopy(m2)
    rows = [f for f in _brand_findings(m2)
            if (f.get("brand") or "").lower() in brands
            and not f.get("agent_letter")]
    cands = [{"name": f["brand"], "document": f["source"].get("file"),
              "line_no": f.get("line_no"), "evidence": f["source"].get("line")}
             for f in rows]
    out = hooks.recheck(cands)
    if not out.get("steps") and str(out.get("stop_reason", "")) \
            .startswith("model"):
        if hooks.on_outage == "stop":
            raise AgentUnavailable(f"{reference}: "
                                   f"{out['stop_reason']}")
        return m2, [], "the spec agent has no model - findings kept"
    by_id = {v.get("id"): v for v in out.get("verdicts", [])}
    l1 = m2.setdefault("level1", {}) or {}
    m2["level1"] = l1
    gone, followups, notes = set(), [], []
    for i, f in enumerate(rows, 1):
        b, v = f["brand"], by_id.get(i)
        if v is None:
            _settled(m2, b, "open", "re-read by the M2 agent, no letter")
            gone.add(id(f))
            l1["unresolved"] = (l1.get("unresolved") or 0) + 1
            res, text = "unresolved", (f"'{b}' was re-read by the spec agent "
                                       f"but could not be settled; it no "
                                       f"longer counts.")
        else:
            where = f"{v.get('document')}, line {v.get('line')}"
            if v["letter"] == "f":
                f["decided_by"] = ("re-read by the M2 agent: brand "
                                   "required, no equivalent")
                _settled(m2, b, "finding", f["decided_by"])
                f.update(agent_letter="f", confidence="high",
                         line_no=v.get("line"),
                         detail="re-read by the M2 agent: required, no "
                                "equivalent accepted")
                f["source"] = dict(f["source"], file=v.get("document"),
                                   where=f"page {v.get('page')}, line "
                                         f"{v.get('line')}",
                                   line=v.get("quote"), checked=True,
                                   checked_how="quote found in the document "
                                               "by the M2 agent")
                res, text = "kept", (f"'{b}' was re-read by the spec agent: "
                                     f"required, no equivalent accepted "
                                     f"(\u201c{v.get('quote')}\u201d, "
                                     f"{where}).")
            else:
                _settled(m2, b, "cleared", "re-read by the M2 agent: "
                         + LETTERS.get(v["letter"], v["letter"]))
                gone.add(id(f))
                l1["ok"] = (l1.get("ok") or 0) + 1
                res, text = "cleared", (
                    f"'{b}' was re-read by the spec agent and removed - "
                    f"{LETTERS.get(v['letter'], v['letter'])}: "
                    f"\u201c{v.get('quote')}\u201d ({where}).")
        followups.append({"tool": "recheck", "brand": b, "result": res,
                          "letter": (v or {}).get("letter"),
                          "quote": (v or {}).get("quote"), "text": text})
        notes.append(f"{b}: {res}"
                     + (f" ({v['letter']})" if v else ""))
    m2["findings"] = [f for f in m2.get("findings", []) if id(f) not in gone]
    m2["trace"] = list(m2.get("trace", [])) + [
        "M4 recheck: " + t for t in out.get("trace", [])]
    return m2, followups, "; ".join(notes)


def _settled(m2, brand, result, decided_by):
    """Record on M2's list of names who settled this one now."""
    for n in m2.get("names") or []:
        if n["name"] == brand and n["result"] == "finding":
            n.update(result=result, decided_by=decided_by)


def _spellings(hooks):
    new = hooks.spellings()
    sp = (new.get("award") or {}).get("other_spellings") or []
    text = ("The winner's other spellings in the award notices were checked: "
            + ("; ".join(sp) + " counted as the same company." if sp
               else "none is the same company."))
    return new, {"tool": "spellings", "result": sp, "text": text}, \
        ("same company: " + "; ".join(sp) if sp else "no other spelling "
         "is the same company")


def _winner_awards(m3, hooks, text):
    keys = ((m3 or {}).get("award") or {}).get("spelling_keys") or []
    res = hooks.winner_awards(text, keys)
    n, lines = res.get("count", 0), res.get("lines", [])
    page = (f"Other awards to the winner whose title mentions '{text}': "
            + (f"{n}, e.g. " + " / ".join(lines[:2]) if n else "none")
            + " (shown for the auditor, not scored).")
    obs = f"{n} award(s)" + ("\n" + "\n".join(lines) if lines else "")
    return {"tool": "winner_awards", "text_searched": text, "count": n,
            "lines": lines, "text": page}, obs


# --------------------------------------------------------------- the M4 path
def check_case(reference, m1, m2, m3, hooks):
    """Build the case, run the checks in order, re-score.
    Returns build_case's dict plus "m4_checks" and trace["M4"]."""
    m2, m3 = m2 or {}, m3 or {}
    if not _brand_findings(m2):
        return mark(m4.build_case(m1, m2, m3), "fixed", [], "no brand finding")
    followups, trace = [], []

    todo = _recheckable(m2, hooks)
    if todo:
        m2, new, note = _recheck(reference, m2, hooks,
                                 {b.lower() for b in todo})
        followups += new
        trace.append(f"re-read {', '.join(todo)} -> {note}")

    case = m4.build_case(m1, m2, m3, followups)
    if _can_spell(m2, m3, case, hooks):
        m3, new, note = _spellings(hooks)
        followups.append(new)
        trace.append(f"spellings -> {note}")

    for brand in _searchable(m2, m3, hooks):
        new, note = _winner_awards(m3, hooks, brand)
        followups.append(new)
        trace.append(f"winner's awards mentioning '{brand}' -> "
                     f"{note.splitlines()[0]}")

    return mark(m4.build_case(m1, m2, m3, followups), "fixed", trace,
                f"{len(trace)} check(s)" if trace else "nothing to check")


def mark(case, mode, trace, note):
    case["m4_checks"] = {"mode": mode, "checks": len(trace), "note": note}
    case["trace"]["M4"] = [f"checks ({mode}): {note}"] + trace
    return case
