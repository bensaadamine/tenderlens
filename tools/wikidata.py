"""
WIKIDATA - is this name a company?

    verify_brand(name) -> True   a company or brand
                          False  not one (a person, a place, a standard...)
                          None   unknown - the M2 agent decides

Each name is looked up once and cached in reports/brand_cache.json, so a brand
confirmed on one tender costs nothing on the next. Wikidata is reachable only
from the Windows machine; offline, only cached names are answered.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request

CACHE_PATH = "reports/brand_cache.json"
WD_API = "https://www.wikidata.org/w/api.php"
UA = "TenderLens/0.1 (academic research)"

# A name whose only exact matches are personal names is UNKNOWN (left to the
# M2 agent), not "not a brand": makers are often named after people, and
# Wikidata knows "Bendix" and "Allison" only as family names.
RE_PERSON_NAME = re.compile(
    r"\b(?:given name|surname|family name|male given name|female given name)\b",
    re.I)

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


def load_cache(path=CACHE_PATH):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def save_cache(cache, path=CACHE_PATH):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, sort_keys=True)


# How a Wikidata answer becomes a verdict.
#
# Measured 2026-10-17, synthetic rigged tenders, online: the first rule read
# only the FIRST entry whose label matched, so "Cummins" was a family name
# and "Allison" a given name - both "not a brand". And a name Wikidata does
# not hold ("Knapheide", "Jerr-Dan") was cached as NOT a brand, when it only
# means Wikidata does not know. Of 112 injected brands outside the cache,
# 0 were found. Now: every exact label or alias is read, one that describes
# a company wins (the candidate already has a model code beside it), and no
# match is "unknown" - left to the M2 agent, never cleared and never
# counted.
#
# Then measured online the same day: "Approval" (a Slovak company) and
# "Room" (a Czech company) verified, so "TA Approval-14 days" and "Building
# 236 Room 108" became findings in 5 real tenders. So a name that ALSO has an
# ordinary meaning on Wikidata - an exact entry that is neither a company
# nor a person or place - stays unknown. A person's name does not block:
# "Cummins" is a family name and a company, and verifies.
VERIFY_VERSION = 6
# Each cached verdict carries the version that wrote it. An entry from an
# older version is asked again only if it was undecided; brand / not-brand
# verdicts stay. (v5: with no company among the exact matches, the ordinary-
# word entry is cached - "pannier" the bag, not "Pannier" the family name.
# v6: an ordinary word only by the entry's own label, never an alias.)


# Measured 2026-10-19: "Axis" (Axis Communications, a real lock-in) was cached
# as the ordinary word "shaft" - an ALIAS match. Only an entry whose own
# label IS the name counts as the ordinary word.
def _decide(hits, key):
    """Verdict from Wikidata search hits: (True|False|None, label, desc)."""
    exact = [h for h in hits
             if (h.get("label", "") or "").lower() == key
             or ((h.get("match") or {}).get("text", "") or "").lower() == key]
    if not exact:
        return None, "", "(no exact match on Wikidata)"
    brand_hit = ordinary = None
    for h in exact:
        desc = h.get("description", "") or ""
        # A lowercase label is an ordinary word, never a company - even when
        # its description says "enterprise" ("project": "collaborative
        # enterprise ..." made "Program" a brand, 2026-10-19).
        lower_label = (h.get("label", "") or "")[:1].islower()
        if RE_IS_BRAND.search(desc) and not RE_NOT_BRAND.search(desc) \
                and not lower_label:
            brand_hit = brand_hit or h
        elif (h.get("label", "") or "")[:1].islower() and \
                not RE_NOT_BRAND.search(desc) and \
                (h.get("label", "") or "") == key:
            # An ORDINARY WORD: English Wikidata labels common nouns in
            # lowercase ("room", "consent", "square") and names of people,
            # places and companies capitalised. Measured 2026-10-17: without
            # the lowercase test, people blocked real brands - "Honda" hit a
            # botanist, "Cummins" a mycologist, "Ford" a badminton player.
            ordinary = ordinary or h
    if brand_hit and ordinary:
        return None, ordinary.get("label", ""), (
            "ordinary word as well as a company: "
            + (ordinary.get("description", "") or "")[:80])
    if ordinary and not brand_hit and VERIFY_VERSION >= 5:
        return None, ordinary.get("label", ""), (
            "ordinary word: " + (ordinary.get("description", "") or "")[:80])
    if brand_hit:
        return True, brand_hit.get("label", ""), \
            brand_hit.get("description", "") or ""
    if all(
            RE_PERSON_NAME.search(h.get("description", "") or "")
            for h in exact):
        h = exact[0]
        return None, h.get("label", ""), "person's name only: " + (
            h.get("description", "") or "")
    if all(RE_NOT_BRAND.search(h.get("description", "") or "") for h in exact):
        h = exact[0]
        return False, h.get("label", ""), h.get("description", "") or ""
    h = exact[0]
    return None, h.get("label", ""), h.get("description", "") or ""


def verify_brand(name, cache=None, offline=False, timeout=20, sleep=0.3):
    """True / False / None (unclear). Cached, so each name costs one lookup
    across the whole project."""
    cache = load_cache() if cache is None else cache
    key = name.lower()
    entry = cache.get(key)
    fresh = entry is not None and (
        entry.get("v") == VERIFY_VERSION
        or (entry.get("v") in (4, 5) and entry.get("brand") is not None))
    if entry is not None and entry.get("brand") is True \
            and (entry.get("label") or "")[:1].islower():
        return None                # cached under the old rule: an ordinary word
    if entry is not None and (fresh or offline):
        if not fresh and \
                entry.get("description") == "(no exact match on Wikidata)":
            return None            # old cache said "not a brand" for "unknown"
        if entry.get("brand") is False and \
                RE_PERSON_NAME.search(entry.get("description") or "") and \
                len((entry.get("description") or "").split()) <= 4:
            return None            # a maker may carry a person's name
        return entry.get("brand")
    if offline:
        return None
    url = (WD_API + "?action=wbsearchentities&format=json&language=en"
           "&uselang=en&limit=10&type=item&search="
           + urllib.parse.quote(name))
    hits = None
    for wait in (0, 2, 5, 10):     # Wikidata refuses bursts; never cache that
        if wait:
            time.sleep(wait)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                hits = json.load(r).get("search", [])
            break
        except Exception:
            continue
    if hits is None:
        return None                # never cache a network failure as a verdict
    time.sleep(sleep)
    verdict, label, desc = _decide(hits, key)
    cache[key] = {"brand": verdict, "label": label, "description": desc,
                  "v": VERIFY_VERSION}
    save_cache(cache)
    return verdict
