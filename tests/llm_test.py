"""First test of the LLM judge in the brand check, on 6 chosen tenders.

    python tests/llm_test.py            (Windows: needs a key in .env and Wikidata)

Writes reports/llm_test/<ref>.json and reports/llm_test/summary.txt.
Token use is logged as usual in reports/llm_usage.csv.

  synth-A  Knapheide KSS-96, no "or equivalent"   expect: finding (LLM confirms the brand)
  synth-D  same, WITH "or equivalent"              expect: cleared
  synth-E  same, only in a bidder's question       expect: nothing
  synth-F  untouched copy of the same tender       expect: nothing
  real     aircraft refueller (Carter, Velcon ...) expect: to audit by hand
  real     packaging-heavy tender, 53 junk names   expect: nothing
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

import io
import json
import os
import sys
import time

import llm
import spec_tools

CASES = [
    ("synth-A-cb-330-23891734", "reports/text_synth", "finding"),
    ("synth-D-cb-330-23891734", "reports/text_synth", "cleared"),
    ("synth-E-cb-330-23891734", "reports/text_synth", "nothing"),
    ("synth-F-cb-330-23891734", "reports/text_synth", "nothing"),
    ("cb-807-5793043", "reports/text", "audit by hand"),
    ("cb-19-46779306", "reports/text", "nothing"),
]
OUT = os.path.join("reports", "llm_test")


def main():
    if not llm.available():
        sys.exit("no LLM key found - set one in .env")
    os.makedirs(OUT, exist_ok=True)
    lines = [f"LLM TEST - {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for ref, tdir, expect in CASES:
        t = time.time()
        r = spec_tools.brands_without_equivalent(
            ref, tdir, offline=False, use_llm=True, verbose=True)
        with io.open(os.path.join(OUT, ref + ".json"), "w",
                     encoding="utf-8") as f:
            json.dump(r, f, indent=1, ensure_ascii=False)
        fnd = [f"{x['brand']} ({x.get('llm_confidence', '')})"
               for x in r.get("findings", [])]
        okk = [f"{x['brand']}:{x.get('status')}" for x in r.get("ok", [])]
        lines.append(f"{ref}   expect: {expect}   ({time.time() - t:.0f}s)")
        lines.append(f"  findings:   {fnd or '-'}")
        lines.append(f"  cleared:    {okk[:8] or '-'}")
        lines.append(f"  unresolved: {len(r.get('unresolved', []))}")
        for x in r.get("findings", []):
            lines.append(f"    {x['brand']}: {x['evidence'][:120]}")
            if x.get("llm_reason"):
                lines.append(f"      judge: {x['llm_reason'][:200]}")
        lines.append("")
    text = "\n".join(lines)
    with io.open(os.path.join(OUT, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print("\n" + text)


if __name__ == "__main__":
    main()
