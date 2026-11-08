"""
WHAT M2 AND M3 HAND TO M4 - the one place that says what a finding is.

M2 (the documents) and M3 (the award history) each return a list of
findings; M4 turns them into claims, checks each source and scores them. A
finding is BUILT here, as a Finding, so its fields are named in one place
and a misspelt one fails at once. It TRAVELS as a plain dict
(Finding.to_dict()) - the same JSON M4, the queue files and cases.jsonl
have always read. Empty optional fields are left out.

    from agents.evidence import Evidence, Finding
    f = Finding(kind="brand_without_equivalent", level=1,
                claim="Names 'Honda' without accepting an equivalent product",
                source=Evidence(file="01_rfp.pdf", where="page 3, line 12",
                                line="The engine must be a Honda GX390."),
                brand="Honda")
    findings.append(f.to_dict())
"""

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Evidence:
    """Where a finding comes from: what a person opens to check it."""
    file: Optional[str]            # the document, or "award notices (CanadaBuys)"
    where: Optional[str]           # page and line, or what was counted
    line: Optional[str]            # the words themselves
    context: Optional[str] = None  # the lines around them
    checked: Optional[bool] = None  # True: code found the words in the source
    checked_how: Optional[str] = None
    also_at: Optional[list] = None  # other places the same words appear


@dataclass
class Finding:
    """One thing a component found. Only a checked source can score (M4)."""
    kind: str        # brand_without_equivalent | narrow_band | extreme_threshold
    #                  | within_document_bands | recurring_supplier | dependent_supplier
    level: int       # 1 a brand, 2 numbers against peers, 3 award history
    claim: str       # one sentence for the trace and the CSV
    source: Evidence
    confidence: str = "medium"     # high | medium | low
    detail: Optional[str] = None
    weight: Optional[str] = None   # "context" (M3): adds to a case, never makes one
    # brand findings (level 1) only
    brand: Optional[str] = None
    also_written: Optional[list] = None   # other spellings of the same brand
    decided_by: Optional[str] = None      # a rule, Wikidata or the M2 agent
    line_no: Optional[int] = None
    agent_letter: Optional[str] = None    # a-f, when the M2 agent lettered it

    def to_dict(self):
        """The plain dict M4 and the files read; empty fields left out."""
        d = {k: v for k, v in asdict(self).items() if v is not None}
        d["source"] = {k: v for k, v in d["source"].items() if v is not None}
        return d
