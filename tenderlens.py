"""
TENDERLENS - the orchestrator.

Runs the four agents over a set of tenders, one tender at a time, and writes
the ranked audit queue:

    python tenderlens.py --group 2510            # whole category
    python tenderlens.py --tender cb-258-72766150   # one tender, full trace
    python tenderlens.py --deep-refs reports/m1_deep_refs.txt --group 2510
        -> the tenders M1 wants read; feed to 02_fetch_docs.py --refs

Writes:
    reports/audit_queue.json    every case, every claim, every trace
    reports/audit_queue.csv     one row per tender - for precision@50 later
    reports/audit_queue.html    the dashboard; open it in a browser

---------------------------------------------------------------- the chain

    M1 screen(notice)  ->  route + signals         (notice only, free)
    M2 audit(spec)     ->  level 1 + level 2       (documents)
    M3 investigate     ->  award, buyer->supplier  (award notices)
    M4 build_case      ->  alert / watch / close   (fuses, verifies, scores)

THE ONE ORCHESTRATION DECISION, flagged for review: M1 controls HOW DEEP M2
goes, not WHETHER a readable tender is read. M1 sees only the notice, and a
specification rigged inside an OPEN competition - the case levels 1 and 2
exist for - looks perfectly ordinary on the notice. Letting M1 close it
would make the system blind to its own target. So every tender with
documents gets M2 at standard depth (rules, offline, free), and M1's "deep"
route buys the expensive extras: the LLM judge, when --llm is given.

The agent works per tender (CLAUDE.md); batching belongs to the tools, so
the shared data - notices, observations, awards - is loaded once here and
handed to each agent.
"""

import argparse
import collections
import csv
import html
import io
import json
import os
import sys
import time

import level2
import m1_screener
import m2_spec_auditor
import m3_investigator
import m4_case_builder

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

OUT_JSON = "reports/audit_queue.json"
OUT_CSV = "reports/audit_queue.csv"
OUT_HTML = "reports/audit_queue.html"
ROUTE_ORDER = {"alert": 0, "watch": 1, "close": 2}


def load_shared(verbose=True):
    t = time.time()
    notices = m1_screener.load_notices()
    wstats = m1_screener.method_windows(notices)
    if verbose:
        print(f"notices: {len(notices):,} ({time.time()-t:.0f}s)")
    try:
        rows = level2.load_observations()
    except SystemExit as e:
        print(f"level 2 knowledge base unavailable: {e}")
        rows = []
    graph = m3_investigator.load_graph()
    if verbose:
        print(f"level-2 observations: {len(rows):,} | awards: "
              f"{'%s rows' % format(graph['rows'], ',') if graph else 'NOT LOADED (run m3_investigator.py --fetch)'}")
    return notices, wstats, rows, graph


def run_one(ref, shared, use_llm=False, online=False):
    notices, wstats, rows, graph = shared
    m1 = m1_screener.screen(ref, notices, wstats)
    if m1.get("documents"):
        depth = "full" if (use_llm and m1["route"].startswith("deep")) \
            else "standard"
        m2 = m2_spec_auditor.audit(ref, depth=depth, rows=rows,
                                   online=online)
    else:
        m2 = {"reference": ref, "readable": False, "findings": [],
              "trace": ["no documents held for this tender"]}
    m3 = m3_investigator.investigate(ref, notices.get(ref), graph)
    case = m4_case_builder.build_case(m1, m2, m3)
    case["m1_route"] = m1.get("route")
    return case


def select(notices, group=None, refs=None):
    if refs:
        return [r for r in refs if r in notices]
    out = []
    for ref, row in notices.items():
        code = m3_investigator.first_unspsc4(row.get(m1_screener.C_UNSPSC))
        if group and code != group:
            continue
        out.append(ref)
    return sorted(out)


def write_outputs(cases, meta):
    os.makedirs("reports", exist_ok=True)
    cases.sort(key=lambda c: (ROUTE_ORDER[c["route"]], -c["score"],
                              c["reference"]))
    with io.open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "cases": cases}, f, indent=1, default=str)
    with io.open(OUT_CSV, "w", encoding="utf-8", newline="") as f:
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
    with io.open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(render_html(cases, meta))


# --------------------------------------------------------------- dashboard
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
            tag = (f"{x.get('agent')} · {x['kind']} · {x.get('confidence','')}"
                   f" · {x.get('points', 0)} pt")
            if not x["verified"]:
                tag += " · UNVERIFIED"
            elif x.get("not_scored_because"):
                tag += f" · not scored: {x['not_scored_because']}"
            out.append(f"<div class='claim{' v' if x['verified'] else ''}'>"
                       f"<div class=k>{_e(tag)}</div>{_e(x['claim'])}"
                       f"<div class=k>{_e(s.get('file'))}"
                       f"{' - ' + _e(s.get('where')) if s.get('where') else ''}"
                       f"</div>")
            if s.get("line"):
                out.append(f"<q>{_e(str(s['line'])[:400])}</q>")
            out.append("</div>")
        tr = c.get("trace", {})
        out.append("<div class=trace>")
        for k in ("M1", "M2", "M3"):
            v = tr.get(k)
            v = [v] if isinstance(v, str) else (v or [])
            for line in v:
                out.append(f"{k}: {_e(line)}<br>")
        out.append("</div></div></details>")
    out.append(f"</main><script>{JS}</script></body></html>")
    return "".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", help="UNSPSC-4 group, e.g. 2510")
    ap.add_argument("--tender", help="run one tender and print the case")
    ap.add_argument("--refs", help="file with one reference per line")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--llm", action="store_true",
                    help="let M2 use the LLM judge on tenders M1 routes deep "
                         "(spends tokens)")
    ap.add_argument("--online", action="store_true",
                    help="let M2 verify unknown brands online (Wikidata)")
    ap.add_argument("--deep-refs", metavar="FILE",
                    help="write the references M1 routes deep, for "
                         "02_fetch_docs.py --refs, and stop")
    a = ap.parse_args()

    shared = load_shared()
    notices = shared[0]

    if a.deep_refs:
        refs = select(notices, a.group)
        deep = []
        for r in refs:
            res = m1_screener.screen(r, notices, shared[1])
            if res["route"].startswith("deep"):
                deep.append(r)
        with io.open(a.deep_refs, "w", encoding="utf-8") as f:
            f.write("\n".join(deep) + "\n")
        print(f"{len(deep)} M1-deep reference(s) -> {a.deep_refs}")
        return

    if a.tender:
        c = run_one(a.tender, shared, a.llm, a.online)
        print(f"\n{c['reference']}  ->  {c['route'].upper()}  "
              f"(score {c['score']}, M1 {c['m1_route']})\n")
        print(c["narrative"])
        print()
        for x in c["claims"]:
            mark = "+" if x["verified"] else "?"
            print(f" {mark} [{x.get('agent')} {x['kind']} {x.get('points',0)}pt] "
                  f"{x['claim'][:110]}")
            if x.get("not_scored_because"):
                print(f"      not scored: {x['not_scored_because']}")
        print()
        for k in ("M1", "M2", "M3"):
            v = c["trace"][k]
            for line in ([v] if isinstance(v, str) else v):
                print(f"  {k}: {line}")
        return

    refs = None
    if a.refs:
        with io.open(a.refs, encoding="utf-8") as f:
            refs = [l.strip() for l in f if l.strip()]
    if not a.group and not refs:
        ap.error("--group, --refs or --tender")
    todo = select(notices, a.group, refs)
    if a.limit:
        todo = todo[:a.limit]
    print(f"running the chain on {len(todo):,} tender(s)")
    if a.online:
        print("  --online: brand names not yet in reports/brand_cache.json "
              "are looked up on Wikidata, one at a time. The first run is "
              "slow (minutes); every later run reuses the cache.")
    cases, t = [], time.time()
    for i, ref in enumerate(todo, 1):
        t1 = time.time()
        cases.append(run_one(ref, shared, a.llm, a.online))
        dt = time.time() - t1
        # A silent minute looks exactly like a hang. Say which tender is
        # slow and why, as it happens.
        if dt > 5:
            print(f"  {i:5d}/{len(todo)}  {ref}  {dt:.0f}s"
                  + ("  (Wikidata lookups)" if a.online else ""))
        elif i % 50 == 0:
            print(f"  {i:5d}/{len(todo)}  ({time.time()-t:.0f}s)")
    meta = {"scope": f"UNSPSC-4 {a.group}" if a.group else a.refs,
            "generated": time.strftime("%Y-%m-%d %H:%M"),
            "tenders": len(cases), "awards": shared[3] is not None,
            "llm": a.llm, "weights": m4_case_builder.WEIGHTS,
            "alert_at": m4_case_builder.ALERT,
            "watch_at": m4_case_builder.WATCH}
    write_outputs(cases, meta)
    cnt = collections.Counter(c["route"] for c in cases)
    print(f"\nalert {cnt['alert']} | watch {cnt['watch']} | "
          f"close {cnt['close']}   ({time.time()-t:.0f}s)")
    print(f"  {OUT_HTML}\n  {OUT_CSV}\n  {OUT_JSON}")


if __name__ == "__main__":
    main()
