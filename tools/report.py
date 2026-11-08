"""
THE AUDIT QUEUE, written to disk: <prefix>.json (every case, in full),
<prefix>.csv (one line per case) and <prefix>.html (the page the auditor
opens: alert first, each case unfolding into its case page and sources).

    from tools.report import write_outputs
    write_outputs(cases, {"scope": ..., "generated": ..., "awards": True},
                  "reports/kb/2510/batches/<stamp>_queue")
"""


import collections
import csv
import html
import io
import json
import os

ROUTE_ORDER = {"alert": 0, "watch": 1, "close": 2}


def write_outputs(cases, meta, prefix):
    """Write the ranked queue to <prefix>.json / .csv / .html."""
    out_json, out_csv, out_html = (prefix + ".json", prefix + ".csv",
                                   prefix + ".html")
    os.makedirs(os.path.dirname(out_json) or ".", exist_ok=True)
    cases.sort(key=lambda c: (ROUTE_ORDER[c["route"]], -c["score"],
                              c["reference"]))
    with io.open(out_json, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "cases": cases}, f, indent=1, default=str)
    with io.open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "reference", "route", "score", "m1_route",
                    "readable", "verified_claims", "title", "entity",
                    "supplier"])
        for i, c in enumerate(cases, 1):
            w.writerow([i, c["reference"], c["route"], c["score"],
                        c["m1_route"], int(c["readable"]),
                        sum(1 for x in c["claims"] if x["verified"]),
                        c["title"], c["entity"],
                        (c.get("award") or {}).get("supplier", "")])
    with io.open(out_html, "w", encoding="utf-8") as f:
        f.write(render_html(cases, meta))


# ------------------------------------------------------------- the page
CSS = """
:root{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e2dd;
--alert:#b42318;--watch:#b54708;--close:#5f6b5f;--chip:#efeee9;--mono:ui-monospace,Menlo,Consolas,monospace}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;
--card:#1f1f1d;--line:#33332f;--alert:#f97066;--watch:#fdb022;--close:#9aa59a;--chip:#2a2a27}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.5 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:28px 16px 60px}
h1{font-size:22px;margin:0 0 4px}p.sub{color:var(--muted);margin:0 0 20px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:18px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;min-width:110px}
.stat b{display:block;font-size:22px}.stat span{color:var(--muted);font-size:13px}
.filters{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}
.filters button{border:1px solid var(--line);background:var(--card);color:var(--fg);
border-radius:999px;padding:5px 12px;cursor:pointer;font:inherit;font-size:13px}
.filters button.on{background:var(--fg);color:var(--bg)}
details{background:var(--card);border:1px solid var(--line);border-radius:10px;margin-bottom:8px}
summary{list-style:none;cursor:pointer;padding:12px 14px;display:grid;
grid-template-columns:70px 44px 1fr;gap:12px;align-items:center}
summary::-webkit-details-marker{display:none}
.badge{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.alert .badge{color:var(--alert)}.watch .badge{color:var(--watch)}.close .badge{color:var(--close)}
.score{font-family:var(--mono);font-size:14px;color:var(--muted)}
.t{min-width:0}.t div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.t .ref{font-family:var(--mono);font-size:12px;color:var(--muted)}
.body{padding:0 14px 14px;border-top:1px solid var(--line)}
pre.narr{white-space:pre-wrap;font:inherit;background:var(--chip);border-radius:8px;padding:10px 12px}
.claim{border-left:3px solid var(--line);padding:4px 0 4px 10px;margin:10px 0}
.claim.v{border-color:var(--fg)}
.claim .k{font-size:12px;color:var(--muted)}
.claim q{display:block;font-family:var(--mono);font-size:12.5px;background:var(--chip);
padding:6px 8px;border-radius:6px;margin-top:4px;quotes:none;overflow-wrap:anywhere}
.trace{font-family:var(--mono);font-size:12px;color:var(--muted);overflow-wrap:anywhere}
@media (max-width:600px){summary{grid-template-columns:56px 34px 1fr}}
"""

JS = """
document.querySelectorAll('.filters button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));
 b.classList.add('on');const r=b.dataset.r;
 document.querySelectorAll('details.case').forEach(d=>{
  d.style.display=(r==='all'||d.classList.contains(r))?'':'none'})});
"""


def _e(s):
    return html.escape(str(s if s is not None else ""))


def render_html(cases, meta):
    cnt = collections.Counter(c["route"] for c in cases)
    out = [f"<!doctype html><html lang=en><head><meta charset=utf-8>"
           f"<meta name=viewport content='width=device-width,initial-scale=1'>"
           f"<title>TenderLens audit queue</title><style>{CSS}</style></head>"
           f"<body><main><h1>TenderLens - audit queue</h1>"
           f"<p class=sub>{_e(meta.get('scope'))} · generated "
           f"{_e(meta.get('generated'))} · awards "
           f"{'loaded' if meta.get('awards') else 'not loaded'}. "
           f"Scores are uncalibrated orderings, not probabilities. "
           f"Every case is a hypothesis; a human decides.</p>"
           f"<div class=stats>"]
    for k, lab in (("alert", "alert"), ("watch", "watch"),
                   ("close", "close")):
        out.append(f"<div class=stat><b>{cnt[k]}</b><span>{lab}</span></div>")
    out.append(f"<div class=stat><b>{sum(1 for c in cases if c['readable'])}"
               f"</b><span>with documents</span></div></div>")
    # Open on the most serious route that has any cases, and highlight the
    # button that matches what is actually shown.
    first = next((k for k in ("alert", "watch") if cnt[k]), "all")
    out.append("<div class=filters>" + "".join(
        f"<button data-r={k}{' class=on' if k == first else ''}>{k}</button>"
        for k in ("all", "alert", "watch", "close")) + "</div>")
    for c in cases:
        shown = first == "all" or c["route"] == first
        hidden = "" if shown else " style='display:none'"
        out.append(
            f"<details class='case {c['route']}'{hidden}><summary>"
            f"<span class=badge>{c['route']}</span>"
            f"<span class=score>{c['score']}</span>"
            f"<span class=t><div>{_e(c['title']) or '(untitled)'}</div>"
            f"<div class=ref>{_e(c['reference'])} · {_e(c['entity'])} · "
            f"{_e(c['method'])}</div></span></summary><div class=body>"
            f"<pre class=narr>{_e(c['narrative'])}</pre>")
        for x in c["claims"]:
            s = x.get("source") or {}
            if x.get("plain"):
                tag = f"{x.get('nature')} · {x.get('points', 0)} pt"
            else:
                tag = (f"{x.get('agent')} · {x['kind']} · "
                       f"{x.get('confidence','')} · {x.get('points', 0)} pt")
            if not x["verified"]:
                tag += " · UNVERIFIED"
            elif x.get("not_scored_because"):
                tag += f" · not scored: {x['not_scored_because']}"
            plain = bool(x.get("plain"))
            src = _e(s.get("file"))
            if not plain and s.get("where"):
                src += " - " + _e(s.get("where"))
            out.append(f"<div class='claim{' v' if x['verified'] else ''}'>"
                       f"<div class=k>{_e(tag)}</div>"
                       f"{_e(x.get('plain') or x['claim'])}"
                       f"<div class=k>Source: {src}</div>")
            # On the plain page a notice field is already in the sentence;
            # the quote is shown for document and award evidence only.
            if s.get("line") and (not plain or x.get("agent") != "M1"):
                out.append(f"<q>{_e(str(s['line'])[:400])}</q>")
            out.append("</div>")
        tr = c.get("trace", {})
        out.append("<details class=trace><summary>Technical details"
                   "</summary>")
        for k in ("M1", "M2", "M3"):
            v = tr.get(k)
            v = [v] if isinstance(v, str) else (v or [])
            for line in v:
                out.append(f"{k}: {_e(line)}<br>")
        out.append("</details></div></details>")
    out.append(f"</main><script>{JS}</script></body></html>")
    return "".join(out)
