"""
ONE RUN'S CHOICES - made once by the command, then passed down.

The pipeline holds them here; each component receives the ones it needs as
plain parameters (m2_spec_auditor.audit(..., level2_on=, on_outage=)), so a
function's signature shows what it depends on. Nothing sets a variable inside
another module.

    s = Settings.from_args(a)                 # the command's arguments
    case = one_tender.run_one(ref, shared, s)

Two choices belong to the LLM client and are read there (tools/llm.py):
TENDERLENS_OFFLINE=1 (answers from the cache only: python tenderlens.py
--offline ...) and TENDERLENS_SHARE=1 (calls shared with Gemini).
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Settings:
    online: bool = False      # check unknown brand names on Wikidata (else its cache)
    agent: bool = False       # the M2 agent letters the brand names left open
    m3_agent: bool = False    # the M3 agent decides which spellings are the winner's
    m4_checks: Optional[str] = None  # M4's checks: None, "fixed", "model" (experiment)
    level2: bool = True       # level 2: numbers against peer tenders (dormant)
    on_outage: str = "stop"   # no model answering: "stop" the run (the next run
    #                           resumes there) or "keep" the rules' result

    @classmethod
    def from_args(cls, a, **kw):
        """From a command's arguments (add_args); kw overrides."""
        return cls(online=a.online, agent=a.agent, m3_agent=a.m3_agent,
                   m4_checks=a.m4_checks, level2=not a.no_level2, **kw)


def add_args(ap):
    """The flags every command that runs the system shares."""
    ap.add_argument("--online", action="store_true",
                    help="check brand names Wikidata's cache does not know, "
                         "online (free, cached; Windows)")
    ap.add_argument("--agent", action="store_true",
                    help="the M2 agent letters the brand names the rules "
                         "left open, a-f with a checked quote (LLM, Windows)")
    ap.add_argument("--m3-agent", action="store_true",
                    help="the M3 agent decides which other spellings of the "
                         "winner are the same company (LLM, Windows)")
    ap.add_argument("--m4-checks", nargs="?", const="fixed",
                    choices=["fixed", "model"],
                    help="before ranking a case with a brand finding, M4 "
                         "sends the finding back to the M2 agent and the "
                         "winner's spellings to the M3 agent (LLM, Windows). "
                         "'model': a model chooses the checks (experiment, "
                         "measured worse)")
    ap.add_argument("--no-level2", action="store_true",
                    help="skip level 2 (numbers against peer tenders; "
                         "dormant): no numbers extracted, none compared")
