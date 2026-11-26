"""
M4 CHECKS - before a case with a brand finding is ranked, two checks run
in a fixed order and the rules re-score the case after them:

    1. spellings the M3 agent checks the winner's other spellings in the
                 award notices (skipped when --m3-agent already did); the
                 brand may be the winner's under another name
    2. awards    titles of the winner's other awards that mention each
                 brand - shown to the auditor, never scored

A tender without a brand finding is never sent to a model. Scoring and
routing stay in m4_case_builder.build_case; the checks add no claim and set
no points. What was checked is written on the case page ("Checked again
before ranking").

A third check, handing each brand finding back to the M2 agent for a second
read, was removed (user decision 2026-11-26): the system always runs with
the M2 agent on, so every finding is already lettered and the re-read had
nothing to do. It mattered only when M2 ran on rules alone (measured
2026-11-04: it removed 7 of 7 false alarms in that setup).

    from agents.m4_checks import check_case, default_hooks
    case = check_case(ref, m1, m2, m3, default_hooks(ref, row, graph, m3))
"""


from agents import m4_case_builder as m4

AWARDS_SHOWN = 4
BRAND = "brand_without_equivalent"


class Hooks:
    """The follow-ups for one tender. Tests pass fakes.

    spellings()         -> a new M3 result counting the winner's spellings
                           (the M3 agent; no model -> its own outage rule)
    winner_awards(text, keys) -> {"count": n, "lines": [...]}
    """

    def __init__(self, spellings=None, winner_awards=None):
        self.spellings = spellings
        self.winner_awards = winner_awards


def default_hooks(reference, notice_row, graph, m3, on_outage="stop"):
    """The real follow-ups: the M3 agent and the award index."""
    from agents import m3_investigator as m3i
    key = ((m3 or {}).get("award") or {}).get("supplier_key")

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
    return Hooks(spellings if ok else None, winner_awards if ok else None)


# ---------------------------------------------------------- what is open
def _brand_findings(m2):
    return [f for f in (m2 or {}).get("findings", [])
            if f.get("kind") == BRAND and m4._verified(f.get("source"))]


def _winner(m3):
    return ((m3 or {}).get("award") or {}).get("supplier")


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
