"""
M2 SPEC AUDITOR - tools.

Each function answers ONE question about ONE tender. That is the unit the
auditor works in: a tender is what gets inspected, so a tool that needs the
whole corpus to answer is the wrong shape.

Tool 1 - brands_without_equivalent(reference)
    Detection level 1. CFTA / CETA require functional requirements: a spec may
    name a product only if it also accepts an equivalent. Returns one finding
    per brand named without that allowance, each with the document, page, line
    and sentence, so a human can check it in seconds.

Verification is a Wikidata lookup, cached in reports/brand_cache.json. The
cache is shared and grows: a brand confirmed on one tender costs nothing on
every tender after it. Commit the cache - it makes the team's runs identical.

Two rules keep false positives near zero:
  * A blanket clause ("wherever a brand name appears, read 'or equivalent'")
    makes the document compliant. Those are reported as OK, not as findings.
  * A cited standard (CSA, ISO, ASTM) is evidence of good practice and is
    never a finding.

Use as a library (how the agent calls it):
    from spec_tools import brands_without_equivalent
    result = brands_without_equivalent("PW-23-01022340")

Or from the command line, to check one tender by hand:
    python spec_tools.py --tender PW-23-01022340
    python spec_tools.py --tender PW-23-01022340 --offline   # cache only
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TEXT_DIR = "reports/text"
CACHE_PATH = "reports/brand_cache.json"
WD_API = "https://www.wikidata.org/w/api.php"
UA = "TenderLens/0.1 (academic research)"

# --------------------------------------------------------------- the patterns
RE_CAND = re.compile(
    r"\b([A-Z][A-Za-z&.\-]{2,}(?:\s+[A-Z][A-Za-z&.\-]{2,})?)\b")

# A model number beside a word makes that word much more likely to be a brand.
RE_MODEL_NEAR = re.compile(r"(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9\-]{1,8}\b")

RE_TRADEMARK = re.compile(r"[®™]")

# Lines that explicitly introduce a product.
RE_MAKE_CONTEXT = re.compile(
    r"\b(?:manufacturer|manufactured by|make|makes|brand|brand name|"
    r"trade name|trademark|model|part number|p/?n|oem|supplier of|"
    r"produced by|marque|fabricant|modèle)\b", re.I)

RE_OR_EQUIV = re.compile(
    r"or\s+(?:an?\s+)?(?:approved\s+)?equivalent"
    r"|or\s+(?:an?\s+)?equal\b|or\s+better\b|or\s+comparable\b"
    r"|ou\s+(?:l['’])?équivalent|ou\s+(?:un\s+)?équivalent"
    r"|\bequivalent\s+(?:product|model|item)s?\b", re.I)

RE_EQUIV_TO = re.compile(r"equivalent\s+to\b|équivalent\s+à\b", re.I)

# A blanket clause that legalises every brand mention in the document.
RE_DOC_CLAUSE = re.compile(
    r"(?:any|all|each|wherever|where|whenever)\b[^.\n]{0,80}"
    r"\b(?:brand|trade)\s*names?\b[^.\n]{0,120}\bequivalent\b"
    r"|\bbrand\s*names?\b[^.\n]{0,60}\bfor\s+reference\s+only\b"
    r"|\bno\s+intention\s+to\s+restrict\b[^.\n]{0,80}\bequivalent\b"
    r"|\breference\s+to\s+a\s+(?:brand|trade)\s*name\b[^.\n]{0,120}"
    r"\bequivalent\b", re.I)

# A cited standard is the opposite of brand lock-in.
STANDARDS = {
    "csa", "ulc", "ansi", "astm", "nfpa", "iec", "din", "nema", "sae", "mil",
    "ieee", "nist", "cgsb", "iso", "en", "ul", "api", "awwa", "aashto", "cga",
    "tssa", "bifma", "ansi/bifma", "cmvss", "fmvss", "epa", "nsf", "fcc",
}

# Structural, legal and SACC-boilerplate vocabulary. Without this list the
# standard clauses in every solicitation become brand candidates - and some
# would verify on Wikidata ("Incoterms" is a registered ICC trademark).
STRUCTURAL = {
    "annex", "annexe", "appendix", "appendice", "article", "attachment",
    "section", "clause", "paragraph", "page", "part", "phase", "item",
    "table", "figure", "note", "notes", "schedule", "amendment", "revision",
    "number", "reference", "solicitation", "tender", "contract", "request",
    "proposal", "quotation", "bidder", "bidders", "offeror", "offerors",
    "contractor", "canada", "canadian", "ottawa", "quebec", "ontario",
    "alberta", "manitoba", "saskatchewan", "nunavut", "yukon", "halifax",
    "toronto", "montreal", "vancouver", "winnipeg", "edmonton", "calgary",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    "class", "type", "group", "level", "option", "line", "column", "row",
    "total", "version", "form", "chapter", "exhibit", "addendum",
    "department", "national", "defence", "defense", "public", "services",
    "procurement", "government", "minister", "ministry", "agency", "crown",
    "work", "works", "goods", "service", "delivery", "warranty", "payment",
    "price", "cost", "quantity", "description", "specification", "general",
    "mandatory", "technical", "evaluation", "criteria", "requirement",
    "requirements", "bid", "bids", "award", "closing", "date", "time",
    "the", "and", "all", "any", "for", "with", "this", "that", "shall",
    "must", "will", "may", "each", "such", "other", "new", "used",
    "english", "french", "yes", "no", "n/a", "tbd", "samples", "sample",
    # the context words themselves: "Manufacturer" is described on Wikidata as
    # "a company that produces goods", so it would verify as a brand
    "make", "makes", "manufacturer", "manufacturers", "manufactured",
    "model", "models", "brand", "brands", "trade", "trademark", "part",
    "parts", "supplier", "suppliers", "vendor", "vendors", "oem",
    "product", "products", "equivalent", "unit", "units", "equipment",
    "system", "systems", "assembly", "kit", "set", "series",
    # SACC standard-clause vocabulary
    "incoterms", "incoterm", "applicable", "trading", "partner", "partners",
    "pursuant", "withholding", "termination", "compliance", "applicant",
    "ownership", "accounts", "basis", "right", "rights", "parties", "code",
    "colonel", "sacc", "trafficking", "source", "act", "authority",
    "material", "customs", "tariff", "income", "insurance", "liability",
    "indemnity", "arbitration", "nscm", "cage", "ncage", "daat", "rdims",
}

RE_IS_BRAND = re.compile(
    r"\b(?:compan(?:y|ies)|corporation|corp\.?|incorporated|manufactur\w*|"
    r"brand|marque|enterprise|business|firm|automaker|automobile make|"
    r"conglomerate|multinational|subsidiary|producer|trademark|"
    r"product line|model of|series of)\b", re.I)

RE_NOT_BRAND = re.compile(
    r"\b(?:given name|surname|family name|human|person|city|town|village|"
    r"municipalit|province|state|country|river|lake|mountain|island|"
    r"hormone|protein|gene|chemical|disease|species|genus|film|movie|"
    r"album|song|novel|book|band\b|footballer|politician|writer|actor|"
    r"unit of|number|year|month|language|letter of|standard\b)\b", re.I)


# ------------------------------------------------------------------- helpers
def _norm(s):
    return re.sub(r"\s+", " ", s).strip()


def _plausible(term):
    low = term.lower()
    head = low.split()[0]
    if low in STRUCTURAL or low in STANDARDS:
        return False
    if head in STRUCTURAL or head in STANDARDS:
        return False
    if len(term) < 3 or len(term) > 40:
        return False
    if not re.match(r"^[A-Z]", term):
        return False
    if term.isupper() and len(term) <= 3:
        return False
    return True


def tender_documents(reference, text_dir=TEXT_DIR):
    """Every extracted document belonging to one tender."""
    out = []
    if not os.path.isdir(text_dir):
        return out
    for cat in sorted(os.listdir(text_dir)):
        d = os.path.join(text_dir, cat)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".txt") or not fn.startswith(reference + "__"):
                continue
            with open(os.path.join(d, fn), encoding="utf-8",
                      errors="replace") as f:
                out.append({"category": cat,
                            "document": fn[len(reference) + 2:-4],
                            "text": f.read()})
    return out


# ------------------------------------------------------------ Wikidata, cached
def _load_cache(path=CACHE_PATH):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def _save_cache(cache, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)


def verify_brand(name, cache=None, offline=False, timeout=20, sleep=0.15):
    """True / False / None (unclear). Cached, so each name costs one lookup
    across the whole project."""
    cache = _load_cache() if cache is None else cache
    key = name.lower()
    if key in cache:
        return cache[key].get("brand")
    if offline:
        return None
    url = (WD_API + "?action=wbsearchentities&format=json&language=en"
           "&uselang=en&limit=3&type=item&search="
           + urllib.parse.quote(name))
    verdict, label, desc = None, "", ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            hits = json.load(r).get("search", [])
        verdict, label, desc = False, "", "(no exact match on Wikidata)"
        for h in hits:
            if (h.get("label", "") or "").lower() != key:
                continue           # only an exact label counts
            label = h.get("label", "")
            desc = h.get("description", "") or ""
            if RE_NOT_BRAND.search(desc):
                verdict = False
            elif RE_IS_BRAND.search(desc):
                verdict = True
            else:
                verdict = None     # exists, but the description is unclear
            break
        time.sleep(sleep)
    except Exception as e:
        return None                # never cache a network failure as a verdict
    cache[key] = {"brand": verdict, "label": label, "description": desc}
    _save_cache(cache)
    return verdict


# ------------------------------------------------------------------- the tool
def _candidates(text):
    """Brand candidates in one document, with where they are."""
    found = {}
    lines = text.splitlines()
    run = 0
    for i, line in enumerate(lines):
        start, run = run, run + len(line) + 1
        if len(line) > 400:
            continue
        tm = bool(RE_TRADEMARK.search(line))
        ctx = bool(RE_MAKE_CONTEXT.search(line))
        model = bool(RE_MODEL_NEAR.search(line))
        if not (tm or ctx or model):
            continue
        for m in RE_CAND.finditer(line):
            term = m.group(1).strip(" .-&")
            if not _plausible(term):
                continue
            # Wikidata confirms ordinary English words that happen to be
            # brands: "Ensure" (an Abbott drink), "Square" (payments),
            # "Joint Venture". Measured on this corpus, those alone were 80%
            # of the findings. So a ONE-WORD candidate needs corroboration
            # right beside it - a trademark symbol, or a model number within
            # a few characters - not merely somewhere on the same line.
            # This applies to multi-word candidates too ("Joint Venture" was
            # 36 of 50 findings without it). A real brand lock-in in a
            # specification reads "Leica TS16" or "Michelin 275/80R22.5" - the
            # model or size is right there. A legal phrase never has one.
            tail = line[m.end():m.end() + 25]
            head = line[max(0, m.start() - 25):m.start()]
            if not (RE_TRADEMARK.search(tail[:3])
                    or RE_MODEL_NEAR.search(tail)
                    or RE_MODEL_NEAR.search(head)):
                continue
            # "Ensure all other products function properly" - capitalised
            # because it opens a sentence or a numbered item, not because it
            # is a name.
            if re.fullmatch(r"\s*(?:\d+[.)]\s*|[-*•]\s*)?",
                            line[:m.start()]):
                continue
            # "Square/Carre:", "Colour:" - a field label, not a product.
            if tail[:1] in (":", "/", "="):
                continue
            key = term.lower()
            found.setdefault(key, {"name": term, "occurrences": []})
            found[key]["occurrences"].append({
                "page": text.count("\f", 0, start) + 1,
                "line_no": i + 1,
                "pos": start + m.start(),
                "end": start + m.end(),
                "line": _norm(line)[:300],
                "trademark": tm, "make_context": ctx,
            })
    return list(found.values())


# A numeric requirement beside the brand, so the line can be ranked.
RE_REQ_LINE = re.compile(
    r"\b(?:must|shall|required?|minimum|maximum|min\.?|max\.?|"
    r"not\s+exceed|at\s+least)\b|≥|≤|>=|<=", re.I)

# Lines that mention a brand without specifying it: packing lists, titles,
# file names, delivery manifests. Weak evidence even when the brand is real.
RE_WEAK_LINE = re.compile(
    r"\b(?:quick\s+guide|user\s+manual|guide|manual|datasheet|data\s+sheet|"
    r"brochure|catalogue|catalog|quantity|qty|packing|annex|appendix|"
    r"table\s+of\s+contents|\.pdf|\.docx?|\.xlsx?)\b", re.I)


# ------------------------------------------------------------ LLM judgement
# The rules decide nothing on their own. They find candidates cheaply and hand
# every one, with its evidence line, to a model that can read context. That is
# what the Leica false positive needed: "Leica products (or equivalent) ... are
# mandatory" is obvious to a reader and invisible to a proximity window.
try:
    import llm
except ImportError:
    llm = None

TRIAGE_SYSTEM = (
    "You identify commercial product and company names in government "
    "procurement documents. You answer only with JSON, no prose.")

TRIAGE_PROMPT = """For each numbered item, decide whether the NAME is the name of a
real commercial company, brand or product model.

Answer false for: ordinary English or French words, legal and contractual
phrases, place names, street addresses, people's names, job titles, document
or section titles, technical standards (CSA, ISO, ASTM, BIFMA), units, and
government bodies.

Answer true only for a manufacturer, a brand, or a specific product model.

Items:
%s

Reply with a JSON array, one object per item, nothing else:
[{"n": 1, "company": true, "why": "Swiss surveying equipment manufacturer"}]
"""

JUDGE_SYSTEM = (
    "You audit public procurement specifications against trade-agreement "
    "rules. You answer only with JSON, no prose. You are cautious: you report "
    "a problem only when the text shows it.")

JUDGE_PROMPT = """Canadian trade agreements (CFTA, CETA) require procurement
requirements to be functional. A specification may name a brand or product only
if it also permits an equivalent.

Brand named: %s

Every place this tender mentions it, with surrounding text:

%s

EVERY sentence in this tender that allows an equivalent, anywhere in any of its
documents:

%s

Decide: does this tender REQUIRE this specific product without permitting an
equivalent?

Answer "locked" false if any allowance above covers this product. An allowance
written over a LIST or a TABLE covers every item in it - "The following Leica
products (or equivalent) in the quantities listed are mandatory" permits an
equivalent for every row that follows, including accessories, software and
licences, even though each row names only its own part number. The allowance
does not have to sit next to the product, or name it.

Answer "locked" false too if the name is only mentioned incidentally: in a
document title, a file name, a delivery address, a list of manuals, or as
software the buyer already owns and must stay compatible with.

Answer "locked" true only when the product is mandatory and NO allowance above
can reasonably be read as covering it.

Reply with JSON only:
{"locked": true, "confidence": "high", "reason": "one sentence",
 "quote": "the exact sentence that decides it"}
"""


def llm_triage(names, evidence, verbose=False):
    """Which of these candidate names are real companies or products?

    One call for the whole batch - Groq's free tier is capped per minute, so
    asking once about forty names is the difference between working and not.
    Returns {name_lower: True/False}. On any failure returns {} and the caller
    keeps its own verdict, rather than losing findings to a dead key.
    """
    if llm is None or not names:
        return {}
    items = "\n".join(
        f'{i}. NAME: "{n}"  CONTEXT: {evidence.get(n.lower(), "")[:160]}'
        for i, n in enumerate(names, 1))
    try:
        out = llm.ask_json(TRIAGE_SYSTEM, TRIAGE_PROMPT % items,
                           max_tokens=120 * len(names) + 200, verbose=verbose)
    except llm.NoProvider:
        return {}
    except Exception as e:
        if verbose:
            print(f"    triage failed: {type(e).__name__}: {e}")
        return {}
    result = {}
    for row in out if isinstance(out, list) else out.get("items", []):
        try:
            n = names[int(row["n"]) - 1]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        result[n.lower()] = bool(row.get("company"))
    return result


def llm_judge_lock(brand, excerpts, allowances=(), verbose=False):
    """Is the tender locked to this brand? Returns the model's dict or None.

    `allowances` is every equivalence clause in the tender, regardless of where
    it sits. Without it the model judged "Cyclone Software" a violation while
    eighteen lines above its table the tender said "The following Leica
    products (or equivalent) ... are mandatory". A clause written over a list
    covers the whole list, and no window around one product can see that.
    """
    if llm is None or not excerpts:
        return None
    blob = "\n\n".join(f"[{i}] {e}" for i, e in enumerate(excerpts[:12], 1))
    allow = ("\n\n".join(f"({i}) {a}" for i, a in enumerate(allowances[:12], 1))
             or "(none found anywhere in this tender)")
    try:
        # Generous budget: the strong models are reasoning models and the
        # visible answer is whatever is left after they think.
        return llm.ask_json(JUDGE_SYSTEM, JUDGE_PROMPT % (brand, blob, allow),
                            strong=True, max_tokens=3000, verbose=verbose)
    except llm.NoProvider:
        return None
    except Exception as e:
        if verbose:
            print(f"    judge failed: {type(e).__name__}: {e}")
        return None


def _excerpts(docs, brand, pad=320, limit=12):
    """Every mention of the brand in the tender, with text around it, for the
    model to read. Taken from the RAW text - the candidate filter is not
    allowed to hide the sentence that proves compliance."""
    pat = re.compile(r"(?<![\w-])" + re.escape(brand) + r"(?![\w-])", re.I)
    out = []
    for d in docs:
        text = d["text"]
        for m in pat.finditer(text):
            a = max(0, m.start() - pad)
            b = min(len(text), m.end() + pad)
            out.append(f"{d['document']}: ...{_norm(text[a:b])}...")
            if len(out) >= limit:
                return out
    return out


def _allowances(docs, pad=260, limit=12):
    """Every equivalence clause in the tender, wherever it sits. A clause over
    a table covers the table, so these must be judged against every brand, not
    only the ones they happen to sit beside."""
    out, seen = [], set()
    for d in docs:
        text = d["text"]
        for pat in (RE_OR_EQUIV, RE_DOC_CLAUSE):
            for m in pat.finditer(text):
                a = max(0, m.start() - pad)
                b = min(len(text), m.end() + pad)
                frag = _norm(text[a:b])
                k = frag[:80].lower()
                if k in seen:
                    continue
                seen.add(k)
                out.append(f"{d['document']}: ...{frag}...")
                if len(out) >= limit:
                    return out
    return out


def _qualified_anywhere(docs, brand, window, window_before):
    """Does the tender allow an equivalent for this brand ANYWHERE?

    This must be asked of the raw text, not of the filtered candidates. The
    candidate filter demands a model number beside the brand, which is exactly
    what the exonerating sentence lacks: "The following Leica products (or
    equivalent) ... are mandatory" names no model. Checking only filtered
    occurrences therefore reported a compliant tender as a violation.
    """
    pat = re.compile(r"(?<![\w-])" + re.escape(brand) + r"(?![\w-])", re.I)
    for d in docs:
        text = d["text"]
        for m in pat.finditer(text):
            after = text[m.end():m.end() + window]
            before = text[max(0, m.start() - window_before):m.start()]
            if RE_OR_EQUIV.search(after) or RE_EQUIV_TO.search(before):
                return f"{d['document']} offset {m.start()}"
    return None


def _evidence_rank(occ):
    """Higher is better evidence that this line is where the brand is
    specified, rather than merely mentioned."""
    line = occ["line"]
    score = 0
    if occ["trademark"]:
        score += 4
    if occ["make_context"]:
        score += 3
    if RE_REQ_LINE.search(line):
        score += 3
    if RE_WEAK_LINE.search(line):
        score -= 4
    if len(line) < 25:
        score -= 1
    return score


def brands_without_equivalent(reference, text_dir=TEXT_DIR, window=200,
                              window_before=80, offline=False, cache=None,
                              use_llm=False, verbose=False):
    """Detection level 1 for ONE tender.

    Returns {reference, documents, findings, ok, unresolved, blanket_clause}.
    A finding means: this requirement names a product and does not accept an
    equivalent. It is a hypothesis with evidence attached, never a verdict.
    """
    docs = tender_documents(reference, text_dir)
    if not docs:
        return {"reference": reference, "error": "no extracted text found",
                "documents": 0, "findings": [], "ok": [], "unresolved": []}

    cache = _load_cache() if cache is None else cache
    blanket = False

    # Collect every occurrence of every candidate across the WHOLE tender, so
    # a requirement repeated in the RFP, the statement of requirement and two
    # amendments is one finding with four locations, not four findings.
    per_brand = {}
    for d in docs:
        text = d["text"]
        clause = bool(RE_DOC_CLAUSE.search(text))
        blanket = blanket or clause
        for c in _candidates(text):
            key = c["name"].lower()
            b = per_brand.setdefault(key, {"name": c["name"], "hits": []})
            for occ in c["occurrences"]:
                after = text[occ["end"]:occ["end"] + window]
                before = text[max(0, occ["pos"] - window_before):occ["pos"]]
                occ = dict(occ)
                occ["document"] = d["document"]
                occ["category"] = d["category"]
                occ["blanket_clause"] = clause
                occ["qualified"] = bool(RE_OR_EQUIV.search(after)
                                        or RE_EQUIV_TO.search(before))
                b["hits"].append(occ)

    # Wikidata first - free and already cached. Whatever it cannot settle goes
    # to the LLM in ONE batched call, which is what turns 127 unresolved
    # candidates from a dead end into an answer.
    wd = {k: verify_brand(b["name"], cache, offline)
          for k, b in per_brand.items()}
    triaged = {}
    if use_llm:
        undecided = [b["name"] for k, b in per_brand.items() if wd[k] is None]
        if undecided:
            ev = {b["name"].lower(): max(b["hits"], key=_evidence_rank)["line"]
                  for b in per_brand.values()}
            if verbose:
                print(f"  LLM triage on {len(undecided)} unconfirmed names")
            triaged = llm_triage(undecided, ev, verbose=verbose)

    findings, ok, unresolved = [], [], []
    for key, b in per_brand.items():
        verdict = wd[key]
        if verdict is None and key in triaged:
            verdict = triaged[key]
        hits = b["hits"]
        best = max(hits, key=_evidence_rank)
        row = {
            "brand": b["name"],
            "document": best["document"], "category": best["category"],
            "page": best["page"], "line_no": best["line_no"],
            "evidence": best["line"],
            "occurrences": len(hits),
            "documents": sorted({h["document"] for h in hits}),
            "locations": [f"{h['document']} p.{h['page']} l.{h['line_no']}"
                          for h in sorted(hits, key=lambda h: (h["document"],
                                                               h["page"]))],
            "blanket_clause": best["blanket_clause"],
        }
        if verdict is None:
            row["reason"] = "could not confirm this is a brand"
            unresolved.append(row)
        elif verdict is False:
            continue                          # not a brand: not a finding
        elif _qualified_anywhere(docs, b["name"], window, window_before):
            # The tender allows an equivalent for this product somewhere.
            # Conservative on purpose: one allowance clears the brand.
            row["status"] = "OK"
            row["qualified_at"] = _qualified_anywhere(docs, b["name"], window,
                                                      window_before)
            ok.append(row)
        elif blanket:
            row["status"] = "OK_BLANKET_CLAUSE"
            ok.append(row)
        else:
            # The rules say this looks like a lock-in. Before reporting it, let
            # the model read every mention in context - this is the step that
            # catches "Leica products (or equivalent)" and incidental mentions
            # in titles, addresses and manual lists.
            if use_llm:
                j = llm_judge_lock(b["name"], _excerpts(docs, b["name"]),
                                   allowances=_allowances(docs),
                                   verbose=verbose)
                if j is not None:
                    row["llm_reason"] = str(j.get("reason", ""))[:300]
                    row["llm_quote"] = str(j.get("quote", ""))[:300]
                    row["llm_confidence"] = str(j.get("confidence", ""))[:20]
                    if not j.get("locked"):
                        row["status"] = "OK_LLM_CLEARED"
                        ok.append(row)
                        continue
            row["status"] = "BRAND_WITHOUT_EQUIVALENT"
            findings.append(row)

    findings.sort(key=lambda r: -r["occurrences"])
    return {"reference": reference, "documents": len(docs),
            "blanket_clause": blanket, "findings": findings, "ok": ok,
            "unresolved": unresolved}


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tender", required=True, help="reference number")
    ap.add_argument("--text-dir", default=TEXT_DIR)
    ap.add_argument("--offline", action="store_true",
                    help="use the cache only, no Wikidata calls")
    ap.add_argument("--llm", action="store_true",
                    help="use the LLM to triage unconfirmed names and to "
                         "judge each apparent lock-in in context")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    args = ap.parse_args()

    r = brands_without_equivalent(args.tender, args.text_dir,
                                  offline=args.offline, use_llm=args.llm,
                                  verbose=args.verbose)
    if args.json:
        print(json.dumps(r, indent=2))
        return
    if r.get("error"):
        sys.exit(f"{args.tender}: {r['error']}")

    print(f"\nTender {r['reference']}  ({r['documents']} documents)")
    if r["blanket_clause"]:
        print("  a blanket 'brand names are for reference only' clause is "
              "present")
    print(f"\n  FINDINGS ... {len(r['findings'])}")
    for f in r["findings"]:
        print(f"\n    {f['brand']}   {f['document']} p.{f['page']} "
              f"line {f['line_no']}")
        print(f"      {f['evidence'][:160]}")
        if f["occurrences"] > 1:
            print(f"      also at: {', '.join(f['locations'][:4])}"
                  + (" ..." if len(f["locations"]) > 4 else ""))
        if f.get("llm_reason"):
            print(f"      judged: {f['llm_reason']} "
                  f"[{f.get('llm_confidence', '')}]")
            if f.get("llm_quote"):
                print(f"      quote:  \"{f['llm_quote'][:140]}\"")
    print(f"\n  compliant brand mentions ... {len(r['ok'])}")
    if r["unresolved"]:
        print(f"  unconfirmed candidates ..... {len(r['unresolved'])}"
              "   (not reported as findings)")
    if not r["findings"]:
        print("\n  Nothing to report on level 1 for this tender.")


if __name__ == "__main__":
    main()
