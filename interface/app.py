"""THE AUDITOR'S PAGE - a small local web app over the knowledge bases.

    python tenderlens.py serve [--port 8000]     # opens http://127.0.0.1:8000

    /                       overview: each family's cases to review
    /group/<g>              the queue: cases ranked, clean / confirm
    /group/<g>/base         every tender of the family and its pile
    /group/<g>/case/<ref>   one case: its evidence, how each component
                            (rules or agent) reached it, the verdict
    /doc/<ref>              a document's text, the quoted line marked
    /families               the families in scope; start a new one
    /compare                rules vs agents on the demo's chosen tenders
    /agents                 what the agents did: calls, tokens, quota, errors
    /synth                  the synthetic test's results
    /jobs/<id>              a command started from the page, its output live

Every command runs in the MODE chosen in the sidebar: rules only; hybrid
(rules + agents, live: uses the model's quota); or hybrid replayed from the
cache (no network, no quota - for the demo). The page reads what the runs
wrote (interface/data.py) and starts commands as if typed by hand
(interface/jobs.py). It listens on this computer only. Styles and script:
interface/static/.
"""

import argparse
import collections
import html
import json
import os
import re
import threading
import webbrowser
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from interface import data, jobs

app = FastAPI(title="TenderLens")
app.mount("/static", StaticFiles(directory=os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "static")), name="static")

MODES = {"rules": "Rules", "hybrid": "Hybrid", "replay": "Replay"}
AGENT_FLAGS = ["--agent", "--m3-agent", "--m4-checks"]


def command(name, g, ref, mode):
    """What a button runs, as a person would type it; None when the mode
    makes no sense for it (new tenders are in no cache to replay)."""
    agents = AGENT_FLAGS if mode != "rules" else []
    if name in ("update", "start"):
        if mode == "replay":
            return None
        args = (["update"] if name == "update" else
                ["update", "--groups", g, "--no-download"])
        return args + (["--agents"] if mode == "hybrid" else [])
    if name in ("rerun", "tender"):
        args = ["run", "--group", g, "--reprocess",
                "all" if name == "rerun" else ref] + agents
        return ["--offline"] + args if mode == "replay" else args + ["--online"]
    if name == "synth":
        args = ["eval", "synth", "--run"] + (["--agent"] if agents else [])
        return ["--offline"] + args if mode == "replay" else args
    if name == "demo":           # its three setups always include the agents
        return ["demo"] + ([] if mode == "hybrid" else ["--offline"])
    return None


COMMAND_NAMES = {"update": "Get new tenders", "rerun": "Re-run every tender",
                 "tender": "Run one tender again", "synth": "Synthetic test",
                 "demo": "Compare rules and agents", "start": "Start a family"}


# ------------------------------------------------------------- small parts
def e(s):
    return html.escape(str(s if s is not None else ""))


def when(stamp):
    """20261112-131153 -> 12 Nov 2026, 13:11"""
    m = re.match(r"(\d{4})-?(\d\d)-?(\d\d)[ T-]?(\d\d):?(\d\d)", stamp or "")
    if not m:
        return stamp or "never"
    y, mo, d, h, mi = m.groups()
    month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()[int(mo) - 1]
    return f"{int(d)} {month} {y}, {h}:{mi}"


def sev(c):
    return f"<span class='sev {c['route']}'>{c['route'].capitalize()}</span>"


def who(decided):
    """Who settled something: an agent (a model) or the rules."""
    if "agent" in (decided or "").lower():
        return "<span class='who agent'>Agent</span>"
    return "<span class='who rules'>Rules</span>"


def claim_who(x):
    return who(x.get("decided_by") or (x.get("source") or {}).get("checked_how"))


def reasons(c, n=2):
    """The case's strongest claims, in plain words."""
    best = sorted((x for x in c["claims"] if x.get("verified")),
                  key=lambda x: -x.get("points", 0))[:n]
    return [(x.get("plain") or x["claim"], claim_who(x)) for x in best]


def state(t):
    if t["verdict"] == "confirmed":
        return "<span class='state confirmed'>Confirmed</span>"
    if t["verdict"] == "clean":
        return "<span class='state clean'>Clean</span>"
    return ("<span class=state>"
            + {"quarantine": "To review", "base": "In the base",
               "none": "No documents"}.get(t["pile"], e(t["pile"]))
            + "</span>")


def verdict_buttons(g, t, small=True):
    ref, size = e(t["reference"]), " small" if small else ""
    out = []
    if t["pile"] == "quarantine" and t["verdict"] != "clean":
        out.append(f"<button class='btn{size} clean' onclick=\"verdict('{g}',"
                   f"'{ref}','clean')\">Mark clean</button>")
    if t["verdict"] != "confirmed":
        out.append(f"<button class='btn{size} confirm' onclick=\"verdict("
                   f"'{g}','{ref}','confirmed')\">Confirm</button>")
    return "".join(out)


def tbl(head, rows, right=()):
    ths = "".join(f"<th{' class=r' if i in right else ''}>{h}</th>"
                  for i, h in enumerate(head))
    return (f"<div class=frame><table class=tbl><tr>{ths}</tr>{''.join(rows)}"
            f"</table></div>")


def known(g):
    if g not in data.groups():
        raise HTTPException(404, "unknown family")


def layout(title, head, body, on=""):
    """The page: sidebar (families, tools, mode, running command) + head +
    body."""
    fams = []
    for g in data.groups():
        n = data.summary(g)["to_review"]
        fams.append(f"<a href='/group/{g}'{' class=on' if on == g else ''}>"
                    f"<span>{g} {e(data.NAMES.get(g, ''))}</span><span class="
                    f"'count{' hot' if n else ''}'>{n}</span></a>")
    tools = [("families", "/families", "Families of tenders"),
             ("compare", "/compare", "Rules vs agents"),
             ("agents", "/agents", "Agent activity"),
             ("synth", "/synth", "Synthetic test")]
    tool_links = "".join(f"<a href='{u}'{' class=on' if on == k else ''}>"
                         f"<span>{t}</span></a>" for k, u, t in tools)
    seg = "".join(f"<input type=radio name=mode id=m-{k} value={k}"
                  f"{' checked' if k == 'rules' else ''}><label for=m-{k}>"
                  f"{v}</label>" for k, v in MODES.items())
    live = jobs.current()
    running = (f"<div class=running>Running: <a href='/jobs/{live}'>"
               f"{e(describe(live)[0])}</a><br><span class=muted>Verdicts wait "
               f"until it ends.</span></div>" if live else "")
    return HTMLResponse(
        f"<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{e(title)} - TenderLens</title>"
        f"<link rel=stylesheet href='/static/style.css'></head><body>"
        f"<div class=shell><aside class=side>"
        f"<a class=brand href='/'><b>TenderLens</b><span>Procurement audit "
        f"queue</span></a>"
        f"<nav><p>Review</p><a href='/'{' class=on' if on == 'home' else ''}>"
        f"<span>Overview</span></a>{''.join(fams)}</nav>"
        f"<nav><p>The system</p>{tool_links}</nav>"
        f"<div class=mode><p>Mode for commands</p><div class=seg>{seg}</div>"
        f"<p id=mode-note></p>{running}</div></aside>"
        f"<main class=page>{head}<div id=msg></div>{body}</main></div>"
        f"<dialog id=ask><h2></h2><div class=text></div><div class=row>"
        f"<button class=btn onclick=\"this.closest('dialog').close()\">Cancel"
        f"</button><button class='btn primary go'>Start</button></div></dialog>"
        f"<script src='/static/app.js'></script></body></html>")


def page_head(title, lede="", acts=""):
    return (f"<header class=head><div><h1>{e(title)}</h1>"
            + (f"<p class=lede>{lede}</p>" if lede else "")
            + f"</div><div class=acts>{acts}</div></header>")


def describe(job_id):
    """A command's id -> (what it is, mode, when)."""
    stamp, _, name = (job_id or "").partition("_")
    parts = name.split("-")
    mode = parts[-1] if parts[-1] in MODES else ""
    group = parts[1] if len(parts) > 1 and parts[1] not in MODES else ""
    what = COMMAND_NAMES.get(parts[0], parts[0])
    return (what + (f" ({group})" if group else ""),
            MODES.get(mode, ""), when(stamp))


# ------------------------------------------------------------------ pages
@app.get("/", response_class=HTMLResponse)
def home():
    fams = []
    for g in data.groups():
        s = data.summary(g)
        old = (f"<p class=note>{s['no_case']} of the cases to review were made "
               f"before cases were saved, so they show no evidence yet. Re-run "
               f"every tender to rebuild them.</p>" if s["no_case"] else "")
        review = (f"Review {s['to_review']} case"
                  f"{'s' if s['to_review'] != 1 else ''}"
                  if s["to_review"] else "Open the queue")
        fams.append(
            f"<div class=family><div><h2><span class=mono>{g}</span>"
            f"{e(s['name'])}</h2><div class=figures>"
            f"<div class=todo><b>{s['to_review']}</b><span>to review</span></div>"
            f"<div><b>{s['confirmed']}</b><span>confirmed</span></div>"
            f"<div><b>{s['base']}</b><span>in the base</span></div>"
            f"<div><b>{s['none']}</b><span>without documents</span></div></div>"
            f"<p class='small muted'>Last run {when(s['last_run'])}</p></div>"
            f"<div class=acts><a class='btn primary' href='/group/{g}'>{review}"
            f"</a><a class=btn href='/group/{g}/base'>Knowledge base</a>"
            f"<button class=btn onclick=\"run('rerun','{g}')\">Re-run every "
            f"tender</button></div>{old}</div>")
    recent = []
    for j in jobs.recent(6):
        what, mode, at = describe(j)
        out = jobs.output(j)
        end = out[0][-80:] if out else ""
        status = ("<span class='pill live'>Running</span>" if out and out[1]
                  else "<span class='pill ok'>Finished</span>"
                  if "[finished]" in end else
                  "<span class='pill bad'>Stopped</span>")
        recent.append(f"<tr><td><a href='/jobs/{j}'>{e(what)}</a></td><td>"
                      f"{e(mode)}</td><td>{e(at)}</td><td>{status}</td></tr>")
    body = (
        f"<section>{''.join(fams)}</section>"
        f"<section><div class=tools>"
        f"<div><h3>Start another family</h3><p>Twenty families of goods are "
        f"in scope. Starting one downloads its documents and runs every "
        f"tender.</p><a class=btn href=/families>See the families</a></div>"
        f"<div><h3>Rules vs agents</h3><p>Seven chosen tenders, run with rules "
        f"alone and with the agents, side by side.</p><a class=btn "
        f"href=/compare>Compare</a></div>"
        f"<div><h3>Synthetic test</h3><p>Brand names planted in copies of real "
        f"tenders: does the system find them, without false alarms?</p>"
        f"<a class=btn href=/synth>See the results</a></div></div></section>"
        + (f"<section><h2>Recent commands</h2>"
           + tbl(["Command", "Mode", "Started", "Result"], recent)
           + "</section>" if recent else ""))
    head = page_head(
        "Overview", "Cases waiting for review in each family of tenders. Each "
        "case is a hypothesis with its evidence attached; you decide.",
        "<button class='btn primary' onclick=\"run('update')\">Get new "
        "tenders</button>")
    return layout("Overview", head, body, "home")


TABS = {"review": "To review", "judged": "Judged", "all": "All cases"}


@app.get("/group/{g}", response_class=HTMLResponse)
def queue(g: str, show: str = "review"):
    known(g)
    every = data.ranked(data.tenders(g))
    picks = {"review": [t for t in every if data.to_review(t)],
             "judged": [t for t in every if t["verdict"]],
             "all": [t for t in every if t["case"]]}
    show = show if show in TABS else "review"
    tabs = "".join(f"<a href='?show={k}'{' class=on' if k == show else ''}>"
                   f"{v}<span class=count>{len(picks[k])}</span></a>"
                   for k, v in TABS.items())
    items, bare = [], []
    for t in picks[show]:
        c, ref = t["case"], t["reference"]
        acts = verdict_buttons(g, t) if t["pile"] == "quarantine" else ""
        side = f"<div class=side-acts>{state(t)}<div>{acts}</div></div>"
        if not c:
            bare.append(f"<div class=item><div class=edge></div><div class="
                        f"body><span class=mono>{e(ref)}</span></div>{side}"
                        f"</div>")
            continue
        winner = (c.get("award") or {}).get("supplier")
        why = "".join(f"<div class=reason>{w}<span>{e(text)}</span></div>"
                      for text, w in reasons(c))
        items.append(
            f"<div class='item {c['route']}'><div class=edge></div>"
            f"<div class=body><div class=top>{sev(c)}<a class=title "
            f"href='/group/{g}/case/{quote(ref)}'>{e(c['title']) or ref}</a>"
            f"</div><div class=meta>{e(c.get('entity'))}, published "
            f"{e(c.get('published'))}. "
            + (f"Won by {e(winner)}." if winner else "Winner unknown.")
            + f"</div>{why}</div>{side}</div>")
    empty = {"review": "Nothing left to review in this family.",
             "judged": "No verdict yet.", "all": "No case yet."}[show]
    body = (f"<div class=queue>{''.join(items)}</div>" if items else
            "" if bare else f"<div class='queue empty'>{empty}</div>")
    if bare:
        body += (
            f"<details class='component bare'><summary><b>{len(bare)} "
            f"case{'s' if len(bare) != 1 else ''} without evidence yet</b>"
            f"<span class=muted>made before cases were saved</span></summary>"
            f"<div class=inner><p class=muted>Re-run every tender of the "
            f"family to rebuild them with today's rules. Tenders you already "
            f"judged keep your verdict.</p><p><button class=btn onclick=\"run("
            f"'rerun','{g}')\">Re-run every tender</button></p><div "
            f"class=queue>{''.join(bare)}</div></div></details>")
    head = page_head(
        f"{g} {data.NAMES.get(g, '')}",
        "Alert first, then watch; within each, the higher score first. The "
        "score orders the queue, it is not a probability. Mark clean sends a "
        "tender back to the base; Confirm keeps it apart as a real case.",
        f"<nav class=tabs>{tabs}</nav><a class=btn href='/group/{g}/base'>"
        f"Knowledge base</a>")
    return layout(f"{g} queue", head, body, g)


PILES = {"all": "All", "quarantine": "Quarantine", "base": "Base",
         "none": "No documents"}


@app.get("/group/{g}/base", response_class=HTMLResponse)
def base(g: str, pile: str = "all"):
    known(g)
    rows = data.tenders(g)
    n = collections.Counter(t["pile"] for t in rows)
    n["all"] = len(rows)
    pile = pile if pile in PILES else "all"
    tabs = "".join(f"<a href='?pile={k}'{' class=on' if k == pile else ''}>"
                   f"{v}<span class=count>{n[k]}</span></a>"
                   for k, v in PILES.items())
    shown = sorted((t for t in rows if pile in ("all", t["pile"])),
                   key=lambda t: t["pub_date"], reverse=True)
    lines = []
    for t in shown:
        ref, c = t["reference"], t["case"]
        name = (f"<a href='/group/{g}/case/{quote(ref)}'>{e(ref)}</a>"
                if c else e(ref))
        lines.append(f"<tr><td class=mono>{name}</td><td>{e(t['pub_date'])}"
                     f"</td><td>{e(c['title'] if c else '')}</td><td>"
                     f"{state(t)}</td><td class=r>{t['score']}</td></tr>")
    body = (
        "<dl class=setups><dt>Base</dt><dd>Closed tenders. Later tenders are "
        "compared with them.</dd><dt>Quarantine</dt><dd>Alert and watch "
        "cases, kept apart until you judge them.</dd><dt>No documents</dt>"
        "<dd>Nothing to read.</dd></dl>"
        + tbl(["Reference", "Published", "Title", "State", "Score"], lines,
              right=(4,)))
    head = page_head(
        f"{g} knowledge base",
        "Every tender of the family, in the order it arrived. Each one was "
        "judged against the base as it stood at the time.",
        f"<nav class=tabs>{tabs}</nav>")
    return layout(f"{g} knowledge base", head, body, g)


# ---------------------------------------------------------------- a case
COMPONENTS = [("M1", "M1 Screener", "the notice"),
              ("M2", "M2 Spec Auditor", "the documents"),
              ("M3", "M3 Investigator", "the award history"),
              ("M4", "M4 Case Builder", "the case")]
RE_STEP = re.compile(r"agent step (\d+): (\S+) (.*?) -> (.*)$")


def asked(args):
    """A tool call's arguments, readable: {"text": "Gamry"} -> text: Gamry."""
    try:
        d = json.loads(args)
    except ValueError:
        return args
    if not isinstance(d, dict):
        return args
    if not d:
        return "(nothing)"
    if isinstance(d.get("verdicts"), list):      # classify: one line a name
        return "; ".join(f"line {v.get('line')}: {v.get('letter')}"
                         for v in d["verdicts"] if isinstance(v, dict))
    return "; ".join(f"{k}: {v}" for k, v in d.items())
SHOWN_ELSEWHERE = {"Why it is listed", "Brand names checked"}


def sections(narrative):
    """The case's plain-language narrative, as {heading: [lines]}."""
    out = {}
    for block in (narrative or "").split("\n\n"):
        lines = [l.strip() for l in block.strip().splitlines() if l.strip()]
        if len(lines) > 1:
            out[lines[0]] = [l[2:] if l.startswith("- ") else l
                             for l in lines[1:]]
    return out


def evidence(c, ref, docs):
    out = []
    for x in c["claims"]:
        s = x.get("source") or {}
        where = s.get("file") or ""
        if s.get("where") and s.get("file") in docs:
            where += f", {s['where']}"
        quote_html = ""
        if s.get("line") and x.get("agent") != "M1":
            text = e(str(s["line"])[:400])
            if s.get("file") in docs:
                href = (f"/doc/{quote(ref)}?file={quote(s['file'], safe='')}"
                        + (f"#L{x['line_no']}" if x.get("line_no") else ""))
                quote_html = (f"<a class=quote href='{href}' title='Open the "
                              f"document at this line'>{text}</a>")
            else:
                quote_html = f"<span class=quote>{text}</span>"
        status = ("" if x.get("verified") else " Not verified, so not scored.")
        out.append(
            f"<div class=evidence><div class=top>{claim_who(x)}"
            f"<span>{e(x.get('nature') or x.get('agent'))}, "
            f"{x.get('points', 0)} point{'s' if x.get('points') != 1 else ''}"
            f".{status}</span></div><div class=claim>"
            f"{e(x.get('plain') or x['claim'])}</div>{quote_html}"
            f"<div class=where>From {e(where)}</div></div>")
    return "".join(out) or "<p class=muted>No claim.</p>"


def timeline(c):
    """Each component's trace; an agent's steps as a table: the tool the
    model chose, what it asked, what it got back."""
    out = []
    for k, name, reads in COMPONENTS:
        lines = (c.get("trace") or {}).get(k) or []
        notes, steps = [], []
        for line in [lines] if isinstance(lines, str) else lines:
            m = RE_STEP.search(line)
            if m:
                n, tool, args, got = m.groups()
                steps.append(f"<tr><td class=r>{n}</td><td class=tool>"
                             f"{e(tool)}</td><td class=asked>{e(asked(args)[:220])}"
                             f"</td><td class=got>{e(got[:420])}</td></tr>")
            elif line.strip():
                notes.append(f"<li>{e(line.strip())}</li>")
        chips = "<span class='who rules'>Rules</span>" + (
            "<span class='who agent'>Agent</span>" if steps else "")
        table = ("<div class='frame steps'><table class=tbl><tr><th class=r>"
                 "Step</th><th>Tool the model chose</th><th>Asked</th><th>Got "
                 "back</th></tr>" + "".join(steps) + "</table></div>"
                 if steps else "")
        out.append(f"<details class=component{' open' if steps else ''}>"
                   f"<summary><b>{name}</b><span class=muted>reads {reads}"
                   f"</span>{chips}</summary><div class=inner><ul>"
                   f"{''.join(notes)}</ul>{table}</div></details>")
    return "".join(out)


def case_main(c, ref, docs):
    sec = sections(c.get("narrative"))
    names = (c.get("m2") or {}).get("names") or []
    name_rows = [f"<tr><td><b>{e(n['name'])}</b></td><td>{e(n['result'])}"
                 f"</td><td>{who(n.get('decided_by'))} <span class=muted>"
                 f"{e(re.sub(r'^(rules?|the M2 agent): ', '', n.get('decided_by') or ''))}"
                 f"</span></td></tr>" for n in names]
    other = "".join(f"<div class=box><h3>{e(h)}</h3><ul>"
                    + "".join(f"<li>{e(l)}</li>" for l in lines)
                    + "</ul></div>"
                    for h, lines in sec.items() if h not in SHOWN_ELSEWHERE)
    return (
        f"<section><h2>Why it is listed</h2>{evidence(c, ref, docs)}</section>"
        + (f"<section><h2>Brand names checked</h2>"
           + tbl(["Name", "Result", "Settled by"], name_rows) + "</section>"
           if names else "")
        + (f"<section><div class=lists>{other}</div></section>" if other
           else "")
        + "<section><h2>How it was reached</h2><p class=muted>Rules where "
          "exactness matters, agents where judgement is needed. Each agent "
          "step is a tool the model chose; the code then checks its answer: "
          "the quote must be in the document.</p>" + timeline(c)
        + "</section>")


def facts_box(c, ref):
    award = c.get("award") or {}
    return (f"<div class=box><h3>Facts</h3><dl class=facts>"
            f"<dt>Route</dt><dd>{sev(c)}</dd>"
            f"<dt>Score</dt><dd>{c['score']} <span class=muted>(orders the "
            f"queue)</span></dd><dt>Buyer</dt><dd>{e(c.get('entity'))}</dd>"
            f"<dt>Method</dt><dd>{e(c.get('method'))}</dd>"
            f"<dt>Published</dt><dd>{e(c.get('published'))}</dd>"
            f"<dt>Winner</dt><dd>{e(award.get('supplier') or 'unknown')}</dd>"
            f"<dt>Reference</dt><dd class=mono>{e(ref)}</dd></dl></div>")


def docs_box(ref, docs):
    items = "".join(f"<li><a href='/doc/{quote(ref)}?file={quote(d, safe='')}'>"
                    f"{e(d)}</a></li>" for d in docs)
    return (f"<div class=box><h3>Documents</h3><ul class=docs>"
            f"{items or '<li class=muted>None extracted.</li>'}</ul></div>")


@app.get("/group/{g}/case/{ref}", response_class=HTMLResponse)
def case(g: str, ref: str):
    known(g)
    t = data.find(g, ref)
    if not t:
        raise HTTPException(404, "unknown tender")
    c, docs = t["case"], data.document_names(ref)
    judge = (f"<div class=box><h3>Your verdict</h3><p>Now: {state(t)}</p>"
             f"<textarea id=note placeholder='Why (optional)'></textarea>"
             f"<div class=row>{verdict_buttons(g, t, small=False)}</div></div>"
             f"<div class=box><h3>Run it again</h3><p class='small muted'>In "
             f"the mode chosen in the sidebar. Hybrid shows the agents at "
             f"work on this tender.</p><button class=btn onclick=\"run("
             f"'tender','{g}','{e(ref)}')\">Run this tender again</button>"
             f"</div>")
    if not c:
        body = (f"<div class=case><div><p class=muted>This tender was processed "
                f"before cases were saved. Run it again to see its evidence."
                f"</p></div><aside class=panel>{judge}{docs_box(ref, docs)}"
                f"</aside></div>")
        return layout(ref, page_head(ref), body, g)
    body = (f"<div class=case><div>{case_main(c, ref, docs)}</div>"
            f"<aside class=panel>{judge}{facts_box(c, ref)}"
            f"{docs_box(ref, docs)}</aside></div>")
    head = page_head(c["title"] or ref,
                     f"<a href='/group/{g}'>{g} queue</a>")
    return layout(c["title"] or ref, head, body, g)


@app.get("/doc/{ref}", response_class=HTMLResponse)
def doc(ref: str, file: str):
    lines = data.document(ref, file)
    if lines is None:
        raise HTTPException(404, "unknown document")
    text = "".join(f"<div id=L{i}><span>{i}</span>{e(l)}</div>"
                   for i, l in enumerate(lines, 1))
    head = page_head(file, f"Tender {e(ref)}. The text as extracted from the "
                     f"document; the quoted line is marked.",
                     "<a class=btn href='javascript:history.back()'>Back</a>")
    return layout(file, head, f"<div class=doc>{text}</div>")


# ------------------------------------------------------ the system's pages
@app.get("/families", response_class=HTMLResponse)
def families():
    rows = []
    for f in data.families():
        if f["started"]:
            what = "<span class='pill ok'>Started</span>"
            act = f"<a class='btn small' href='/group/{f['group']}'>Open</a>"
        elif f["not_readable"]:
            what = (f"<span class=pill>Not readable</span> <span class='small "
                    f"muted'>{e(f['not_readable'])}</span>")
            act = ""
        else:
            what = "<span class=muted>Not started</span>"
            act = (f"<button class='btn small' data-g='{f['group']}' "
                   f"data-name='{e(f['name'])}' data-n='{f['usable']}' "
                   f"onclick='askStart(this.dataset)'>Start</button>")
        rows.append(f"<tr><td class=mono>{f['group']}</td><td>{e(f['name'])}"
                    f"</td><td class=r>{f['usable']}</td><td>{what}</td>"
                    f"<td class=r>{act}</td></tr>")
    head = page_head(
        "Families of tenders",
        "The twenty families of goods in scope, by product code (the first "
        "four digits of UNSPSC). Starting one downloads its documents, "
        "extracts their text and runs every tender; you are asked to confirm "
        "first.")
    body = tbl(["Code", "What is bought", "Tenders with documents", "State",
                ""], rows, right=(2, 4))
    return layout("Families", head, body, "families")


SETUPS = {"rules": ("Rules only", "No model is called."),
          "recheck": ("Rules + re-check", "The rules, then M4 sends every "
                      "flagged brand back to the M2 agent before ranking."),
          "agents": ("Every agent", "The M2 agent judges every open brand "
                     "name, the M3 agent the winner's spellings, M4 "
                     "re-checks the rest.")}


@app.get("/compare", response_class=HTMLResponse)
def compare(run: str = ""):
    from pipeline.demo import TENDERS
    runs = data.demo_runs()
    stamp = run if run in runs else (runs[0] if runs else "")
    cases, summary = data.demo(stamp) if stamp else ({}, "")
    setups = [s for s in SETUPS if s in cases]
    rows = []
    for ref, why in TENDERS:
        first = (cases.get("rules") or {}).get(ref)
        cells = []
        for s in setups:
            c = cases[s].get(ref)
            if not c:
                cells.append("<td class=muted>not run</td>")
                continue
            changed = (s != "rules" and first and c["route"] != first["route"])
            cells.append(
                f"<td{' class=changed' if changed else ''}><a href='/compare/"
                f"{stamp}/{s}/{quote(ref)}'>{sev(c)}</a>"
                + ("<small>changed by the agents</small>" if changed else "")
                + "</td>")
        title = next((cases[s][ref]["title"] for s in setups
                      if ref in cases[s]), ref)
        rows.append(f"<tr><td><b>{e(title)}</b><div class='small muted'>"
                    f"{e(why)}</div></td>{''.join(cells)}</tr>")
    picker = ("<select onchange=\"location.href='?run='+this.value\">"
              + "".join(f"<option value={s}{' selected' if s == stamp else ''}"
                        f">{when(s)}</option>" for s in runs[:10])
              + "</select>" if runs else "")
    head = page_head(
        "Rules vs agents",
        "The hybrid system on seven chosen tenders, run three ways. Where a "
        "column differs from the rules, the agents changed the outcome: a "
        "lock-in the rules miss, a false alarm they clear. Open a cell to "
        "see that case and each agent's steps.",
        f"{picker}<button class='btn primary' onclick=\"run('demo')\">Run the "
        f"comparison</button>")
    body = (
        "<dl class=setups>" + "".join(f"<dt>{a}</dt><dd>{b}</dd>"
                                      for a, b in SETUPS.values()) + "</dl>"
        + tbl(["Tender"] + [SETUPS[s][0] for s in setups], rows)
        + (f"<section style='margin-top:22px'><details class=component>"
           f"<summary><b>The run's own summary</b></summary><div class=inner>"
           f"<pre class=report>{e(summary)}</pre></div></details></section>"
           if summary else ""))
    return layout("Rules vs agents", head, body, "compare")


@app.get("/compare/{stamp}/{setup}/{ref}", response_class=HTMLResponse)
def compare_case(stamp: str, setup: str, ref: str):
    if stamp not in data.demo_runs():
        raise HTTPException(404, "unknown run")
    c = data.demo(stamp)[0].get(setup, {}).get(ref)
    if not c:
        raise HTTPException(404, "unknown case")
    docs = data.document_names(ref)
    others = "".join(
        f"<a class='btn small' href='/compare/{stamp}/{s}/{quote(ref)}'>{a}</a>"
        for s, (a, _) in SETUPS.items() if s != setup)
    side = (f"<div class=box><h3>Setup</h3><p><b>{SETUPS[setup][0]}</b>. "
            f"{SETUPS[setup][1]}</p><p class='small muted'>The same tender in "
            f"the other setups:</p><div class=row>{others}</div></div>"
            if setup in SETUPS else "")
    body = (f"<div class=case><div>{case_main(c, ref, docs)}</div>"
            f"<aside class=panel>{side}{facts_box(c, ref)}{docs_box(ref, docs)}"
            f"</aside></div>")
    head = page_head(c["title"], f"<a href='/compare?run={stamp}'>Rules vs "
                     f"agents</a>, run of {when(stamp)}")
    return layout(c["title"], head, body, "compare")


@app.get("/agents", response_class=HTMLResponse)
def agents():
    u, (err_counts, last_err), runs = (data.usage(), data.errors(),
                                       data.agent_runs())
    t = u["today"]
    used = min(100, round(100 * t["groq_tokens"] / data.GROQ_DAILY))
    per_agent = [
        f"<tr><td>{e(name)}</td><td class=r>{a['calls']:,}</td><td class=r>"
        f"{round(100 * a['cached'] / a['calls'])}%</td><td class=r>"
        f"{a['tokens']:,}</td><td>{e(when(u['last'].get(name)))}</td></tr>"
        for name, a in sorted(u["agents"].items(),
                              key=lambda kv: -kv[1]["calls"])]
    days = []
    for day in sorted(u["days"], reverse=True)[:14]:
        spent = sum(u["days"][day].values())
        by = ", ".join(f"{k.split(' - ')[0]} {v:,}"
                       for k, v in u["days"][day].most_common() if v)
        days.append(f"<tr><td>{e(day)}</td><td class=r>{spent:,}</td><td>"
                    f"<div class=bar><i style='width:"
                    f"{min(100, round(100 * spent / data.GROQ_DAILY))}%'></i>"
                    f"</div></td><td class='small muted'>{e(by)}</td></tr>")
    run_rows = [
        f"<tr><td>{e(k)}</td><td class=r>{v['runs']}</td><td class=r>"
        f"{v['steps'] / v['runs'] if v['runs'] else 0:.1f}</td><td>"
        + e("; ".join(f"{s} ({n})" for s, n in v["stops"].most_common())
            or "none yet") + "</td></tr>" for k, v in runs.items()]
    errs = [f"<tr><td>{e(a)}</td><td>{e(k)}</td><td class=r>{n}</td></tr>"
            for (a, k), n in err_counts.most_common(10)]
    last = [f"<tr><td>{e(when(r.get('ts')))}</td><td>"
            f"{e(data.agent_name(r.get('tag')))}</td><td>{e(r.get('error'))}"
            f"</td><td class=mono>{e((r.get('detail') or '')[:140])}</td></tr>"
            for r in last_err]
    body = (
        f"<section><h2>Today</h2><div class=figures>"
        f"<div><b>{t['calls']:,}</b><span>model calls</span></div>"
        f"<div><b>{t['cached']:,}</b><span>answered from the cache</span></div>"
        f"<div><b>{t['groq_tokens']:,}</b><span>tokens paid</span></div>"
        f"<div><b>{used}%</b><span>of the free daily quota</span></div></div>"
        f"<div class='bar big'><i style='width:{used}%'></i></div></section>"
        f"<section><h2>Per agent, since the start</h2>"
        + tbl(["Agent", "Calls", "From the cache", "Tokens paid", "Last call"],
              per_agent, right=(1, 2, 3))
        + "</section><section><h2>Tokens paid per day</h2><p class=muted>The "
          "bar is the share of Groq's free daily quota, 200,000 tokens.</p>"
        + tbl(["Day", "Tokens", "Share of the quota", "By agent"], days,
              right=(1,))
        + "</section><section><h2>How the investigations ended</h2><p "
          "class=muted>Every case in the knowledge bases and the latest "
          "comparison run. An agent stops when every name is settled, at its "
          "step budget, or when no model answers.</p>"
        + tbl(["Agent", "Investigations", "Steps on average", "How they ended"],
              run_rows, right=(1, 2))
        + "</section><section><h2>Failed calls</h2><p class=muted>HTTP 429: "
          "too many calls in a minute, retried after the wait the server asks "
          "for. HTTP 400: the server refused the model's answer; asked again, "
          "never cached. URLError: no network.</p>"
        + tbl(["Agent", "Error", "Times"], errs, right=(2,))
        + "<h3 style='margin-top:18px'>The latest ones</h3>"
        + tbl(["When", "Agent", "Error", "Detail"], last) + "</section>")
    head = page_head(
        "Agent activity",
        "What the agents did, read from the system's own logs: every model "
        "call, every failed one, and every case's trace. An answer already in "
        "the cache costs nothing.")
    return layout("Agent activity", head, body, "agents")


@app.get("/synth", response_class=HTMLResponse)
def synth(run: str = ""):
    runs = data.synth_runs()
    pick = next((p for s, p in runs if s == run), runs[0][1] if runs else None)
    shown = run or (runs[0][0] if runs else "")
    text = open(pick, encoding="utf-8").read() if pick else ""
    picker = ("<select onchange=\"location.href='?run='+this.value\">"
              + "".join(f"<option value={s}{' selected' if s == shown else ''}"
                        f">{when(s)}</option>" for s, _ in runs[:10])
              + "</select>" if runs else "")
    head = page_head(
        "Synthetic test",
        "Brand names planted in copies of 40 real motor-vehicle tenders, in "
        "seven ways: copies that should be flagged, and copies that should "
        "not (an \"or equivalent\" nearby, a bidder's question, no change). "
        "It measures how often a planted brand is found, and that the legal "
        "ones raise no alarm.",
        f"{picker}<button class='btn primary' onclick=\"run('synth')\">Run "
        f"the test</button>")
    body = (f"<pre class=report>{e(text)}</pre>" if text else
            "<div class='queue empty'>No run yet.</div>")
    return layout("Synthetic test", head, body, "synth")


@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_page(job_id: str):
    out = jobs.output(job_id)
    if not out:
        raise HTTPException(404, "unknown command")
    text, live = out
    what, mode, at = describe(job_id)
    status = ("<span class='pill live'>Running</span>" if live else
              "<span class='pill ok'>Finished</span>"
              if "[finished]" in text[-80:] else
              "<span class='pill bad'>Stopped</span>")
    acts = ("<button class=btn onclick=\"post('/api/stop')\">Stop</button>"
            if live else "") + "<a class=btn href='/'>Overview</a>"
    head = page_head(what, f"{status} Started {at}"
                     + (f", mode {mode}." if mode else "."), acts)
    body = (f"<pre class=console id=log data-job='{e(job_id)}'"
            f"{' data-live=1' if live else ''}>{e(text)}</pre>")
    return layout(what, head, body)


# -------------------------------------------------------------------- API
def refuse(why, code=400):
    return JSONResponse({"error": why}, status_code=code)


@app.post("/api/verdict")
async def api_verdict(request: Request):
    b = await request.json()
    g = b.get("group")
    if g not in data.groups():
        return refuse("Unknown family.")
    if jobs.running():
        return refuse("A command is running. Verdicts wait until it ends.",
                      409)
    said = data.record_verdict(g, b.get("reference") or "",
                               b.get("verdict") or "",
                               (b.get("note") or "").strip())
    return (refuse(said[6:].capitalize() + ".") if said.startswith("skip")
            else {"done": said})


@app.post("/api/jobs/{name}")
async def api_job(name: str, request: Request):
    b = await request.json()
    g, ref, mode = (b.get("group") or "", b.get("reference") or "",
                    b.get("mode") or "rules")
    startable = {f["group"] for f in data.families()
                 if not f["started"] and not f["not_readable"]}
    if (mode not in MODES
            or (name in ("rerun", "tender") and g not in data.groups())
            or (name == "tender" and not data.find(g, ref))
            or (name == "start" and g not in startable)):
        return refuse("Unknown command.")
    args = command(name, g, ref, mode)
    if not args:
        return refuse("New tenders are in no cache, so they cannot be "
                      "replayed. Choose Rules or Hybrid to get them.")
    job_id = jobs.start("-".join(x for x in (name, g, mode) if x), args)
    return {"id": job_id} if job_id else refuse(
        "A command is already running. Wait for it to end, or stop it.", 409)


@app.get("/api/jobs/{job_id}")
def api_job_output(job_id: str):
    out = jobs.output(job_id)
    if not out:
        raise HTTPException(404, "unknown command")
    return {"text": out[0], "live": out[1]}


@app.post("/api/stop")
def api_stop():
    jobs.stop()
    return {"done": "stopped"}


def main():
    ap = argparse.ArgumentParser(description="the auditor's page")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open the page in the browser")
    a = ap.parse_args()
    import uvicorn
    url = f"http://127.0.0.1:{a.port}"
    if not a.no_browser:
        threading.Timer(1.5, webbrowser.open, [url]).start()
    print(f"TenderLens page: {url}   (Ctrl+C to stop)")
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
