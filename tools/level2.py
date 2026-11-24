"""M2 Spec Auditor, tool 2 - detection level 2 for one tender.

Compares a tender's numeric requirements with peer tenders (same UNSPSC-8
code, same attribute): a band much narrower than peers allow, or a threshold
far more restrictive than theirs. Also gives the within-document share of
two-sided bands, which needs no peers. Measured on 2510: peer comparison
finds nothing usable (its top-ranked "finding" was a semi-trailer compared
with pickup trucks), so results are low confidence.
"""


import collections
import statistics

MIN_PEERS = 8
BAND_RATIO = 0.25
POSITION_Z = 3.0
CONJUNCTION = 2

LEVEL = 8            # peer group: the first 8 UNSPSC digits (decision 13)
# Level 2 can be switched off for a run (--no-level2, pipeline/settings.py):
# then no numbers are extracted per tender and no peers compared.
SIGNALS = {"band", "position"}


def mad(values, med=None):
    """Median absolute deviation, scaled to compare with a std deviation."""
    if not values:
        return 0.0
    med = statistics.median(values) if med is None else med
    return 1.4826 * statistics.median([abs(v - med) for v in values])


def prepare(rows):
    """Keep rows with a converted value; add _v, _lo, _hi and the peer key
    (family, dimension, attribute)."""
    out = []
    for r in rows:
        if not r.get("base_value"):
            continue
        try:
            r["_v"] = float(r["base_value"])
        except ValueError:
            continue
        r["_lo"] = _f(r.get("base_lo"))
        r["_hi"] = _f(r.get("base_hi"))
        r["_key"] = (r["unspsc8"][:LEVEL], r["dimension"], r["label"])
        out.append(r)
    return out


def _f(x):
    """Float or None."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def tender_values(rows):
    """Group rows as {key: {reference: [rows]}}."""
    out = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        out[r["_key"]][r["reference"]].append(r)
    return out


def peer_stats(by_key, exclude=None):
    """Per attribute: peer median, MAD, range, spread (one value per tender, tender itself excluded)."""
    stats = {}
    for key, per_tender in by_key.items():
        vals, widths = [], []
        for ref, rs in per_tender.items():
            if ref == exclude:
                continue
            vals.append(statistics.median([r["_v"] for r in rs]))
            w = [r["_hi"] - r["_lo"] for r in rs
                 if r["_hi"] is not None and r["_lo"] is not None]
            if w:
                widths.append(statistics.median(w))
        if len(vals) < MIN_PEERS:
            continue
        med = statistics.median(vals)
        stats[key] = {
            "n": len(vals),
            "median": med,
            "mad": mad(vals, med),
            "lo": min(vals),
            "hi": max(vals),
            "spread": max(vals) - min(vals),
            "median_band": statistics.median(widths) if widths else None,
            "n_bands": len(widths),
        }
    return stats


def judge(rows_for_attr, st):
    """Is this tender's requirement unusually restrictive vs peers? Returns (tight, kind, detail)."""
    band = [r for r in rows_for_attr
            if r["_hi"] is not None and r["_lo"] is not None]
    if band and st["spread"] > 0 and "band" in SIGNALS:
        width = min(r["_hi"] - r["_lo"] for r in band)
        ratio = width / st["spread"]
        if ratio <= BAND_RATIO:
            return True, "narrow_band", {
                "band_width": round(width, 3),
                "peer_spread": round(st["spread"], 3),
                "ratio": round(ratio, 3),
            }
        return False, "band_not_narrow", {"ratio": round(ratio, 3)}

    if "position" not in SIGNALS:
        return False, "position_disabled", {}

    v = statistics.median([r["_v"] for r in rows_for_attr])
    if st["mad"] <= 0 or st["mad"] < 0.05 * abs(st["median"]):
        return False, "no_peer_spread", {"value": v, "peer_median": st["median"],
                                         "peer_mad": round(st["mad"], 4)}

    z = (v - st["median"]) / st["mad"]
    comps = {r["comparator"] for r in rows_for_attr}
    if "GE" in comps:
        restrictive = z
    elif "LE" in comps:
        restrictive = -z
    else:
        return False, "no_direction", {"value": v, "z": round(z, 2)}

    detail = {"value": v, "peer_median": st["median"], "peer_n": st["n"],
              "z": round(z, 2), "restrictive_z": round(restrictive, 2)}
    return (restrictive >= POSITION_Z), "extreme_threshold", detail


def within_document(rows_for_tender):
    """Share of this tender's requirements stated as two-sided bands."""
    total = len(rows_for_tender)
    if not total:
        return {"requirements": 0, "two_sided": 0, "share": 0.0}
    two = sum(1 for r in rows_for_tender
              if r["_hi"] is not None and r["_lo"] is not None)
    return {"requirements": total, "two_sided": two,
            "share": round(two / float(total), 3)}


def tight_requirements(reference, rows, min_peers=MIN_PEERS):
    """Level 2 for one tender: findings, attributes with too few peers,
    within-document share. `rows`: the tender's own rows and its peers'
    (pipeline/stream.py: the knowledge base)."""
    mine = [r for r in rows if r["reference"] == reference]
    if not mine:
        return {"reference": reference, "error": "no observations - is this "
                "tender in the knowledge base?", "score": 0, "findings": [],
                "insufficient_peers": [], "not_tight": [],
                "within_document": within_document([])}

    by_key = tender_values(rows)
    stats = peer_stats(by_key, exclude=reference)

    mine_by_attr = collections.defaultdict(list)
    for r in mine:
        mine_by_attr[r["_key"]].append(r)

    findings, thin, loose = [], [], []
    for key, rs in sorted(mine_by_attr.items()):
        fam, dim, attr = key
        st = stats.get(key)
        if st is None or st["n"] < min_peers:
            have = len(by_key[key]) - 1
            thin.append({"family": fam, "dimension": dim, "attribute": attr,
                         "peers": max(have, 0),
                         "why": "insufficient_peers"})
            continue
        tight, kind, detail = judge(rs, st)
        ev = max(rs, key=lambda r: len(r.get("source_line") or ""))
        row = {"family": fam, "dimension": dim, "attribute": attr,
               "unit": ev.get("base_unit") or ev.get("unit"),
               "kind": kind, "peer_n": st["n"],
               "peer_median": round(st["median"], 3),
               "peer_range": [round(st["lo"], 3), round(st["hi"], 3)],
               "stated": ev.get("value_text") or ev.get("value"),
               "file": ev.get("file"), "section": ev.get("section"),
               "source_line": ev.get("source_line"),
               "context": ev.get("context")}
        row.update(detail)
        (findings if tight else loose).append(row)

    findings.sort(key=lambda r: -(r.get("restrictive_z") or
                                  (1.0 / max(r.get("ratio", 1.0), 1e-9))))
    return {
        "reference": reference,
        "score": len(findings),
        "verdict": ("conjunction" if len(findings) >= CONJUNCTION
                    else "single" if findings else "none"),
        "findings": findings,
        "insufficient_peers": thin,
        "not_tight": loose,
        "within_document": within_document(mine),
    }
