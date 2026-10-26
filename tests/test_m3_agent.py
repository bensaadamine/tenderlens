"""
The M3 agent's rules, tested with a scripted fake model and a small made-up
award file - no network, no real data.

    python tests/test_m3_agent.py
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

import csv
import io
import os
import sys
import tempfile

import m3_agent
import m3_investigator

FAILS = 0
HEAD = ["title-titre-eng", "referenceNumber-numeroReference",
        "solicitationNumber-numeroSollicitation",
        "contractAwardDate-dateAttributionContrat",
        "totalContractValue-valeurTotaleContrat", "unspsc",
        "supplierLegalName-nomLegalFournisseur-eng",
        "supplierAddressCity-fournisseurAdresseVille-eng",
        "supplierAddressPostalCode-fournisseurAdresseCodePostal",
        "contractingEntityName-nomEntitContractante-eng"]
BUYER = "Natural Resources Canada"
# (solicitation, supplier, postal code, city)
AWARDS = [
    ("S1", "Agilent Technologies Canada Inc.", "L5N 5M4", "Mississauga"),
    ("S2", "Agilent Technologies", "L5N 5M4", "Mississauga"),
    ("S3", "Agilent Technologies Canada", "L5N 5M4", "Mississauga"),
    ("S4", "Tower Dental Clinic", "L5N 5M4", "Mississauga"),
    ("S5", "Agilent Technologies", "H9R 1A1", "Montreal"),
    ("S6", "Bruker Ltd", "L6H 6W6", "Milton"),
    ("S7", "Agilent Bruker Joint Venture", "L5N 5M4", "Mississauga"),
    ("S8", "Agilent Labs Solutions", "K1A 0A1", "Ottawa"),
]


def check(name, ok):
    global FAILS
    print(("ok   " if ok else "FAIL ") + name)
    FAILS += 0 if ok else 1


def scripted(*replies):
    it = iter(replies)
    calls = []

    def ask(system, user):
        calls.append((system, user))
        r = next(it, {"action": "finish", "args": {}})
        if isinstance(r, Exception):
            raise r
        return r
    ask.calls = calls
    return ask


def make_graph(tmp):
    path = os.path.join(tmp, "awards.csv")
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        for sol, sup, pc, city in AWARDS:
            w.writerow([f"Lab instrument {sol}", f"ref-{sol}", sol,
                        "2025-01-01", "1000", "41110000", sup, city, pc,
                        BUYER])
    return m3_investigator.load_graph(path)


def main():
    tmp = tempfile.mkdtemp()
    g = make_graph(tmp)
    aliases = os.path.join(tmp, "aliases.csv")
    W = "AGILENT TECHNOLOGIES CANADA"
    index = m3_agent.AwardIndex(g)

    v = index.variants(W)
    check("variants: same postal code and same first word, winner left out",
          "AGILENT TECHNOLOGIES" in v and "TOWER DENTAL CLINIC" in v
          and "AGILENT LABS SOLUTIONS" in v and W not in v)
    check("variants: shared postal code comes first",
          v.index("AGILENT TECHNOLOGIES") < v.index("AGILENT LABS SOLUTIONS"))

    ask = scripted({"action": "decide", "args": {
        "same": ["AGILENT TECHNOLOGIES", "AGILENT LABS SOLUTIONS",
                 "AGILENT BRUKER JOINT VENTURE", "MADE UP NAME"],
        "different": ["TOWER DENTAL CLINIC"],
        "reason": "test"}})
    out = m3_agent.aliases_for(W, g, ask=ask, path=aliases)
    check("same accepted with a shared postal code",
          "AGILENT TECHNOLOGIES" in out["same"])
    check("same refused without shared code and with a different name",
          "AGILENT LABS SOLUTIONS" not in out["same"]
          and any("AGILENT LABS" in t and "refused" in t for t in out["trace"]))
    check("a joint venture is never the same as a partner",
          "AGILENT BRUKER JOINT VENTURE" not in out["same"])
    check("a name the tools never showed is refused",
          any("MADE UP NAME" in t and "not a name the tools showed" in t
              for t in out["trace"]))
    check("different recorded", "TOWER DENTAL CLINIC" in out["different"])

    ask2 = scripted()
    out2 = m3_agent.aliases_for(W, g, ask=ask2, path=aliases)
    check("stored decisions reused: the model is asked only about the rest",
          "AGILENT TECHNOLOGIES" in out2["same"]
          and "AGILENT TECHNOLOGIES" not in out2["asked"]
          and "TOWER DENTAL CLINIC" not in out2["asked"])

    with io.open(aliases, "a", encoding="utf-8", newline="") as f:
        csv.writer(f).writerow([W, "AGILENT TECHNOLOGIES", "different",
                                "auditor says no", "auditor", "2026-10-26"])
    out3 = m3_agent.aliases_for(W, g, ask=scripted(), path=aliases)
    check("an auditor's row beats the agent's",
          "AGILENT TECHNOLOGIES" in out3["different"]
          and "AGILENT TECHNOLOGIES" not in out3["same"])

    out = m3_agent.aliases_for(
        "AGILENT TECHNOLOGIES", g, path=os.path.join(tmp, "a2.csv"),
        ask=scripted({"action": "decide", "args": {
            "same": ["AGILENT TECHNOLOGIES CANADA"], "reason": "x"}}))
    check("same allowed when names differ only by generic words",
          "AGILENT TECHNOLOGIES CANADA" in out["same"])

    seen = []

    def spy(system, user):
        seen.append(system)
        return {"action": "awards", "args": {"name": W}}
    m3_agent.aliases_for(W, g, ask=spy, path=os.path.join(tmp, "a3.csv"),
                         store=False)
    check("the last call has no tool menu",
          seen[-1] == m3_agent.FINAL_SYSTEM
          and "variants" not in m3_agent.FINAL_SYSTEM.split("Reply")[1])

    # --- the counts in m3_investigator use the decisions
    row = {m3_investigator.C_SOL_TENDER: "S1",
           "contractingEntityName-nomEntitContractante-eng": BUYER}
    plain = m3_investigator.investigate("t", row, g, agent=False)
    real = m3_agent.aliases_for
    m3_agent.aliases_for = lambda *a, **k: {
        "same": ["AGILENT TECHNOLOGIES"], "different": [], "asked": [],
        "steps": 1, "stop_reason": "every candidate decided",
        "engine": "loop", "trace": []}
    try:
        merged = m3_investigator.investigate("t", row, g, agent=True)
    finally:
        m3_agent.aliases_for = real
    # S1 and S3 are one spelling once "Inc." is dropped; S2 and S5 are the
    # spelling the agent merged
    check("without the agent: the winner's spelling counted alone",
          plain["award"]["to_this_supplier"] == 2)
    check("with the agent: its other spellings counted with it",
          merged["award"]["to_this_supplier"] == 4
          and merged["award"]["other_spellings"] == ["Agilent Technologies"])
    check("distinct suppliers drop by the merged spellings",
          merged["award"]["distinct_suppliers"]
          == plain["award"]["distinct_suppliers"] - 1)

    # --- no model
    real = m3_agent.aliases_for
    m3_agent.aliases_for = lambda *a, **k: real(
        *a, ask=scripted(RuntimeError("groq: daily quota used")),
        path=os.path.join(tmp, "a4.csv"), **k)
    try:
        try:
            m3_investigator.investigate("t", row, g, agent=True)
            check("no model, 'stop': the run is told to stop", False)
        except m3_investigator.AgentUnavailable:
            check("no model, 'stop': the run is told to stop", True)
        m3_investigator.AGENT_DOWN = "keep"
        kept = m3_investigator.investigate("t", row, g, agent=True)
        check("no model, 'keep': winner's spelling counted alone",
              kept["award"]["to_this_supplier"] == 2)
    finally:
        m3_agent.aliases_for = real
        m3_investigator.AGENT_DOWN = "stop"

    print(f"\n{FAILS} failure(s)")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
