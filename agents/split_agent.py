"""
SPLIT-PURCHASE AGENT - one need cut into several contracts, so that each
stays under the limit for buying without competition.

The limit: a goods contract may be given without bids when it does not
exceed $25,000 (Government Contracts Regulations, s. 6(b)(i)). Cutting one
need into pieces to stay under it skips the competition the rule requires.
This works from the award file alone: no tender documents are needed.

RULES (code, exact) find the groups:
    same buyer, same supplier, non-competitive goods awards of $25,000 or
    less each, the later ones within WINDOW_DAYS of the first, together
    over $25,000.

THE AGENT judges each group:
    GOAL   split (one need cut into pieces) / separate (different needs) /
           unclear (the records cannot tell)
    TOOLS  history  {}                   every award from this buyer to this
                                         supplier, any method, any value
           habit    {}                   how often this buyer gives ANY
                                         supplier a non-competitive goods
                                         award just under the limit
           detail   {"contract": "..."}  one award in full
           decide   {"verdict", "reason", "quote"}
           finish   {}
    LOOP   decide -> act -> ... (agents/agent_loop.py) until a verdict, the
           model finishes, or MAX_STEPS; the last call has no tools.

What stays in CODE, never left to the model:
  * the groups, every number and every date;
  * "split" needs a quote found, word for word, in at least two of the
    group's own titles or descriptions: the words that make it one need;
  * a "split" ranks a WATCH, never an alert: the award file cannot show
    intent, the auditor opens the files;
  * every verdict is stored (reports/splits/verdicts.csv) and never asked
    again; the auditor's verdict (from the auditor's page) decides the
    route.

    python tenderlens.py splits                     groups + rule verdicts
    python tenderlens.py splits --agent             + the agent (Groq)
    python tenderlens.py splits --labels tests/split_labels.csv
"""

import argparse
import collections
import csv
import io
import json
import os
import re
import sys
import time
from datetime import date
from typing import TypedDict

from agents import agent_loop
from tools import names

csv.field_size_limit(10_000_000)
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

AWARDS = os.path.join("data", "awards.csv")
OUT_DIR = os.path.join("reports", "splits")
VERDICTS = os.path.join(OUT_DIR, "verdicts.csv")
VERDICT_COLS = ["group", "verdict", "reason", "quote", "decided_by", "date"]
LIMIT = 25_000          # goods, Government Contracts Regulations s. 6(b)(i)
WINDOW_DAYS = 30
NEAR = 0.95             # a contract at 95% of the limit or more is "just under"
MAX_STEPS = 5
HISTORY_SHOWN = 15
VERDICTS_ALLOWED = ("split", "separate", "unclear")

C = {"ref": "referenceNumber-numeroReference",
     "contract": "contractNumber-numeroContrat",
     "amend": "amendmentNumber-numeroModification",
     "date": "contractAwardDate-dateAttributionContrat",
     "amount": "contractAmount-montantContrat",
     "total": "totalContractValue-valeurTotaleContrat",
     "method": "procurementMethod-methodeApprovisionnement-eng",
     "cat": "procurementCategory-categorieApprovisionnement",
     "unspsc": "unspsc",
     "title": "title-titre-eng",
     "desc": "awardDescription-descriptionAttribution-eng",
     "buyer": "contractingEntityName-nomEntitContractante-eng",
     "supplier": "supplierLegalName-nomLegalFournisseur-eng",
     "end_user": "endUserEntitiesName-nomEntitesUtilisateurFinal-eng",
     "region": "regionsOfDelivery-regionsLivraison-eng",
     "start": "contractStartDate-contratDateDebut",
     "end": "contractEndDate-dateFinContrat"}


# ------------------------------------------------------------ the records
def money(s):
    """'33,175.49' / '33175.49' / '$1 234,50' -> float; 0.0 if empty.
    Three digits after a lone comma are thousands (decision 23)."""
    s = re.sub(r"[^\d.,]", "", s or "")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head.replace(",", "") + ("" if len(tail) == 3 else ".") + tail
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def key(s):
    """A buyer's or supplier's name as its words (tools/names.py)."""
    return " ".join(names.words(s))


def load_records(path=AWARDS):
    """One record per contract: its newest amendment."""
    best = {}
    with io.open(path, encoding="utf-8-sig", errors="replace",
                 newline="") as f:
        for r in csv.DictReader(f):
            d = (r.get(C["date"]) or "")[:10]
            rec = {k: (r.get(col) or "").strip() for k, col in C.items()}
            rec["date"] = d
            rec["value"] = money(rec["total"]) or money(rec["amount"])
            rec["buyer_key"] = key(rec["buyer"])
            rec["supplier_key"] = key(rec["supplier"])
            rec["id"] = rec["contract"] or rec["ref"]
            amend = int(re.sub(r"\D", "", rec["amend"]) or 0)
            old = best.get(rec["id"])
            if old is None or (amend, d) >= (old[0], old[1]["date"]):
                best[rec["id"]] = (amend, rec)
    return [rec for _, rec in best.values()]


def is_goods(rec):
    return rec["cat"].replace("*", "").strip() == "GD"


def non_competitive(rec):
    return rec["method"].lower().startswith("non-competitive")


def in_families(rec, families):
    return any(u[:4] in families for u in re.findall(r"\d{8}", rec["unspsc"]))


# ---------------------------------------------------------------- the rule
def find_groups(records, families=None):
    """Same buyer and supplier, non-competitive goods awards of LIMIT or
    less each, within WINDOW_DAYS of the first, together over LIMIT."""
    pool = collections.defaultdict(list)
    for r in records:
        if not (r["date"] and is_goods(r) and non_competitive(r)
                and 0 < r["value"] <= LIMIT and r["supplier_key"]):
            continue
        if families and not in_families(r, families):
            continue
        pool[(r["buyer_key"], r["supplier_key"])].append(r)
    groups = []
    for (b, s), recs in sorted(pool.items()):
        recs.sort(key=lambda r: r["date"])
        i = 0
        while i < len(recs):
            first = date.fromisoformat(recs[i]["date"])
            j = i
            while (j + 1 < len(recs) and (date.fromisoformat(
                    recs[j + 1]["date"]) - first).days <= WINDOW_DAYS):
                j += 1
            part = recs[i:j + 1]
            if len(part) > 1 and sum(r["value"] for r in part) > LIMIT:
                groups.append(make_group(part))
            i = j + 1
    return groups


RE_CODE = re.compile(r"\b(?=[A-Z0-9-]*\d)(?=[A-Z0-9-]*[A-Z])"
                     r"[A-Z0-9]+(?:-[A-Z0-9]+)+\b")


def codes(text):
    """Reference codes in a title ('EP916-240077', 'CW2420122_...' is not
    one): letters and digits joined by hyphens."""
    return set(RE_CODE.findall(text or ""))


def make_group(recs):
    """A group and the facts code computes about it."""
    r0 = recs[0]
    total = sum(r["value"] for r in recs)
    shared = collections.Counter(c for r in recs for c in codes(r["title"]))
    days = (date.fromisoformat(recs[-1]["date"])
            - date.fromisoformat(r0["date"])).days
    return {"id": f"{r0['date']}|{r0['buyer_key'][:40]}|"
                  f"{r0['supplier_key'][:40]}",
            "buyer": r0["buyer"], "supplier": r0["supplier"],
            "buyer_key": r0["buyer_key"], "supplier_key": r0["supplier_key"],
            "records": recs, "total": round(total, 2), "days": days,
            "same_day": len({r["date"] for r in recs}) < len(recs),
            "near_limit": [r["id"] for r in recs
                           if r["value"] >= NEAR * LIMIT],
            "shared_codes": sorted(c for c, n in shared.items() if n > 1)}


def rule_verdict(g):
    """The rules' own answer, to compare with the agent's: a reference code
    in two titles, or two awards on one day -> split."""
    return "split" if (g["shared_codes"] or g["same_day"]) else "separate"


# ---------------------------------------------------------------- the tools
def line(r, desc=True):
    out = (f"{r['date']} | ${r['value']:,.0f} | {r['method'][:28]} | "
           f"{r['id']} | {r['title'][:90]}")
    if desc and r["desc"] and key(r["desc"]) != key(r["title"]):
        out += f" | {r['desc'][:150]}"
    return out


class Index:
    def __init__(self, records):
        self.by_pair = collections.defaultdict(list)
        self.by_buyer = collections.defaultdict(list)
        self.by_id = {}
        for r in records:
            self.by_pair[(r["buyer_key"], r["supplier_key"])].append(r)
            self.by_buyer[r["buyer_key"]].append(r)
            self.by_id[r["id"]] = r

    def history(self, g):
        recs = sorted(self.by_pair[(g["buyer_key"], g["supplier_key"])],
                      key=lambda r: r["date"], reverse=True)
        methods = collections.Counter(r["method"][:28] or "?" for r in recs)
        head = (f"{len(recs)} award(s) from this buyer to this supplier: "
                + ", ".join(f"{m} {n}" for m, n in methods.most_common()))
        rows = [line(r, desc=False) for r in recs[:HISTORY_SHOWN]]
        more = len(recs) - HISTORY_SHOWN
        return "\n".join([head] + rows + ([f"... {more} more"] if more > 0
                                          else []))

    def habit(self, g):
        nc = [r for r in self.by_buyer[g["buyer_key"]]
              if is_goods(r) and non_competitive(r) and r["value"] > 0]
        under = [r for r in nc if r["value"] <= LIMIT]
        near = [r for r in under if r["value"] >= 0.8 * LIMIT]
        sups = collections.Counter(r["supplier_key"] for r in near)
        mine = sups.get(g["supplier_key"], 0)
        return (f"This buyer: {len(nc)} non-competitive goods award(s); "
                f"{len(under)} at or under ${LIMIT:,}; {len(near)} between "
                f"${0.8 * LIMIT:,.0f} and ${LIMIT:,}, to {len(sups)} "
                f"supplier(s), {mine} of them to this supplier.")

    def detail(self, contract):
        r = self.by_id.get(str(contract or "").strip())
        if r is None:
            return f"no award '{contract}' (use an id shown in a line)"
        return "\n".join(f"{k}: {r[k]}" for k in (
            "id", "ref", "date", "method", "title", "desc", "end_user",
            "region", "start", "end") if r[k]) + f"\nvalue: ${r['value']:,.2f}"


def quote_holds(g, quote):
    """The quote appears, word for word, in two or more of the group's own
    titles or descriptions."""
    if len(names.words(quote)) < 2 and not codes(quote):
        return False
    hits = sum(1 for r in g["records"]
               if names.says(r["title"] + " \n " + r["desc"], quote))
    return hits >= 2


# ------------------------------------------------------------------ prompt
SYSTEM = f"""You help a government auditor in Canada. A goods contract may
be given to a supplier without competition only if it is ${LIMIT:,} or
less. Cutting ONE need into several contracts so that each stays under that
limit avoids the competition the rule requires.

Below is a GROUP: one buyer gave one supplier several non-competitive goods
contracts, each under the limit, within {WINDOW_DAYS} days, together over it.
Decide whether it is:
  split     ONE need cut into pieces: the same project, order or reference
            code, the same items for the same place, pieces bought on the
            same day, values placed just under the limit.
  separate  different needs that happen to be close in time: different
            items, different end users or places, a supplier this buyer
            uses for routine small orders all year.
  unclear   the records cannot tell.
A wrong "split" accuses a buyer; when the records do not show one need, say
separate or unclear. Values and dates are computed for you; never compute.

Reply with ONE JSON object and nothing else:
{{"thought": "<one sentence>", "action": "<tool>", "args": {{...}}}}

Tools:
history  {{}}                    every award from this buyer to this supplier
habit    {{}}                    how often this buyer gives ANY supplier a
                                 non-competitive goods award near the limit
detail   {{"contract": "<id>"}}  one award in full (end user, place, dates)
decide   {{"verdict": "split|separate|unclear", "reason": "<one sentence>",
          "quote": "<for split: words that appear in two or more of the
                    group's titles or descriptions and show it is one need>"}}
finish   {{}}                    stop without a verdict
"""

FINAL_SYSTEM = SYSTEM.split("Reply with ONE JSON")[0] + """\
You have finished looking. Decide now from what is shown.

Reply with ONE JSON object and nothing else:
{"verdict": "split|separate|unclear", "reason": "<one sentence>",
 "quote": "<for split: words found in two or more of the group's titles>"}
"""


def _tools():
    from tools import llm
    t = llm.tool
    return [
        t("history", "Every award from this buyer to this supplier."),
        t("habit", "How often this buyer gives any supplier a "
                   "non-competitive goods award near the limit."),
        t("detail", "One award in full.",
          {"contract": {"type": "string"}}, ["contract"]),
        t("decide", "The verdict, a one-sentence reason, and for split the "
                    "words that appear in two or more of the group's titles.",
          {"verdict": {"type": "string", "enum": list(VERDICTS_ALLOWED)},
           "reason": {"type": "string"}, "quote": {"type": "string"}},
          ["verdict", "reason"]),
        t("finish", "Stop without a verdict."),
    ]


class AgentState(TypedDict):
    verdict: str
    reason: str
    quote: str
    history: list
    steps: int
    done: bool
    stop_reason: str


def render_group(g):
    out = [f"BUYER: {g['buyer']}", f"SUPPLIER: {g['supplier']}",
           f"{len(g['records'])} contracts in {g['days']} day(s), together "
           f"${g['total']:,.0f} (limit ${LIMIT:,} each):"]
    out += ["  " + line(r) for r in g["records"]]
    facts = []
    if g["same_day"]:
        facts.append("two or more were awarded on the same day")
    if g["near_limit"]:
        facts.append(f"{len(g['near_limit'])} at {NEAR:.0%} of the limit "
                     f"or more")
    if g["shared_codes"]:
        facts.append("reference code in two titles: "
                     + ", ".join(g["shared_codes"]))
    if facts:
        out.append("Computed: " + "; ".join(facts) + ".")
    return "\n".join(out)


def render(state, g):
    out = [render_group(g), "", f"Steps left: {MAX_STEPS - state['steps']}."]
    if state["history"]:
        out += ["", "What you did so far:"]
        for h in state["history"]:
            out.append(f"- step {h['step']}: {h['action']} "
                       f"{json.dumps(h['args'], ensure_ascii=False)[:160]}")
            out.append("  -> " + h["observation"][:1500]
                       .replace("\n", "\n     "))
    return "\n".join(out)


def make_nodes(g, index, ask):
    def one_reply(state, final=False):
        reply = agent_loop.ask_model(ask, FINAL_SYSTEM if final else SYSTEM,
                                     render(state, g))
        if "action" not in reply and "verdict" in reply:
            return "decide", reply                   # the final call
        return agent_loop.parse_action(reply)[:2]

    def decide(state):
        try:
            action, args = one_reply(state)
            if state["steps"] + 1 >= MAX_STEPS and action != "decide":
                action, args = one_reply(state, final=True)
        except Exception as e:
            return {"done": True,
                    "stop_reason": f"model unavailable: {str(e)[:200]}"}
        return {"history": state["history"] + [{
                    "step": state["steps"] + 1, "action": action,
                    "args": args, "observation": ""}],
                "steps": state["steps"] + 1}

    def act(state):
        h = dict(state["history"][-1])
        action, args = h["action"], h["args"]
        out, done = {}, False
        if action == "history":
            obs = index.history(g)
        elif action == "habit":
            obs = index.habit(g)
        elif action == "detail":
            obs = index.detail(args.get("contract"))
        elif action == "decide":
            v = str(args.get("verdict", "")).strip().lower()
            reason = str(args.get("reason", ""))[:300]
            quote = str(args.get("quote", ""))[:200]
            if v not in VERDICTS_ALLOWED:
                obs = "refused: verdict must be split, separate or unclear"
            elif v == "split" and not quote_holds(g, quote):
                obs = ("refused: for split, quote words that appear in two "
                       "or more of the group's titles or descriptions; "
                       "otherwise say separate or unclear")
            else:
                obs, done = f"verdict: {v}", True
                out = {"verdict": v, "reason": reason,
                       "quote": quote if v == "split" else ""}
        elif action == "finish":
            obs, done = "finished", True
        elif not action:
            obs = "error: reply with one JSON object with an 'action'"
        else:
            obs = (f"error: unknown action '{action}'. Use one of: "
                   f"history, habit, detail, decide, finish")
        h["observation"] = obs
        stop = done or state["steps"] >= MAX_STEPS
        reason = ("verdict given" if out else
                  "finished by the model" if done else
                  "step budget used" if stop else "")
        return {**out, "history": state["history"][:-1] + [h],
                "done": stop, "stop_reason": reason}

    return decide, act


def judge(g, index, ask=None, use_graph=True):
    """Run the agent on one group. Returns {"verdict" ("" if none),
    "reason", "quote", "steps", "stop_reason", "engine", "trace",
    "models"}."""
    models = collections.Counter()
    if ask is None:
        from tools import llm
        call = llm.json_asker(models, strong=True, max_tokens=2000,
                              tag="split_agent")
        ask = lambda system, user: call(
            system, user, tools=None if system == FINAL_SYSTEM else _tools())
    state = AgentState(verdict="", reason="", quote="", history=[], steps=0,
                       done=False, stop_reason="")
    decide, act = make_nodes(g, index, ask)
    state, engine = agent_loop.run(state, decide, act, AgentState, MAX_STEPS,
                                   use_graph)
    trace = [f"step {h['step']}: {h['action']} "
             f"{json.dumps(h['args'], ensure_ascii=False)[:160]} -> "
             f"{h['observation'].replace(chr(10), ' | ')[:300]}"
             for h in state["history"]]
    return {"verdict": state["verdict"], "reason": state["reason"],
            "quote": state["quote"], "steps": state["steps"],
            "stop_reason": state["stop_reason"], "engine": engine,
            "trace": trace, "models": dict(models)}


# ---------------------------------------------------------- stored verdicts
def by_who(path=VERDICTS):
    """{group id: {"agent": row, "auditor": row}} - the newest of each."""
    out = collections.defaultdict(dict)
    if os.path.exists(path):
        with io.open(path, encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                who = "auditor" if r["decided_by"] == "auditor" else "agent"
                out[r["group"]][who] = r
    return out


def save_verdict(row, path=VERDICTS):
    new = not os.path.exists(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with io.open(path, "a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=VERDICT_COLS)
        if new:
            w.writeheader()
        w.writerow(row)


def route(verdict):
    """split -> watch (never alert: the award file cannot show intent)."""
    return "watch" if verdict == "split" else "close"


# -------------------------------------------------------------------- main
def load_labels(path):
    with io.open(path, encoding="utf-8", newline="") as f:
        rows = csv.DictReader(l for l in f if not l.startswith("#"))
        return {r["group"]: r["label"].strip().lower() for r in rows}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--families", default="",
                    help="comma-separated UNSPSC-4 families (default: every "
                         "goods category - no documents are needed)")
    ap.add_argument("--agent", action="store_true",
                    help="judge each group not yet decided with the agent")
    ap.add_argument("--labels", help="a labels CSV (group,label): compare")
    ap.add_argument("--export", help="write the groups, without any verdict, "
                                     "to this file (for blind labelling)")
    ap.add_argument("--awards", default=AWARDS)
    a = ap.parse_args()

    records = load_records(a.awards)
    fams = {f.strip() for f in a.families.split(",") if f.strip()} or None
    groups = find_groups(records, fams)
    index = Index(records)
    if a.export:
        with io.open(a.export, "w", encoding="utf-8") as f:
            for g in groups:
                f.write(f"## {g['id']}\n{render_group(g)}\n\nHISTORY:\n"
                        f"{index.history(g)}\nHABIT: {index.habit(g)}\n\n")
        print(f"{len(groups)} group(s) -> {a.export}")
        return 0

    stored = by_who()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(OUT_DIR, stamp)
    os.makedirs(out_dir, exist_ok=True)
    rows = []
    for g in groups:
        st = stored.get(g["id"], {}).get("agent")
        aud = stored.get(g["id"], {}).get("auditor")
        res = None
        if a.agent and not st:
            res = judge(g, index)
            if res["verdict"]:
                st = {"group": g["id"], "verdict": res["verdict"],
                      "reason": res["reason"], "quote": res["quote"],
                      "decided_by": "agent",
                      "date": time.strftime("%Y-%m-%d")}
                save_verdict(st)
        rows.append({"group": g["id"], "buyer": g["buyer"],
                     "supplier": g["supplier"], "n": len(g["records"]),
                     "total": g["total"], "days": g["days"],
                     "rule": rule_verdict(g),
                     "agent": (st or {}).get("verdict", ""),
                     "reason": (st or {}).get("reason", ""),
                     "quote": (st or {}).get("quote", ""),
                     "auditor": (aud or {}).get("verdict", ""),
                     "route": route((aud or st or {}).get("verdict", "")),
                     "lines": [line(r) for r in g["records"]],
                     "group_text": render_group(g),
                     "stop": (res or {}).get("stop_reason", ""),
                     "trace": (res or {}).get("trace", [])})

    with io.open(os.path.join(out_dir, "groups.json"), "w",
                 encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    lines = [f"SPLIT PURCHASES - {stamp} - {len(groups)} group(s) "
             f"(non-competitive goods awards of ${LIMIT:,} or less, same "
             f"buyer and supplier, within {WINDOW_DAYS} days, together over "
             f"${LIMIT:,})", ""]
    for r in rows:
        lines.append(f"{r['rule']:9s} {r['agent'] or '-':9s} {r['route']:6s}"
                     f" ${r['total']:>9,.0f} {r['n']}x {r['days']:>2}d  "
                     f"{r['supplier'][:30]:30s} {r['buyer'][:40]}")
        if r["reason"]:
            lines.append(f"{'':27s}{r['reason'][:150]}")
        if r["stop"] and not r["agent"]:
            lines.append(f"{'':27s}no verdict: {r['stop'][:120]}")
    if a.labels:
        labels = load_labels(a.labels)
        lines.append("")
        for who in ("rule", "agent"):
            judged = [r for r in rows if r["group"] in labels and r[who]]
            right = sum(r[who] == labels[r["group"]] for r in judged)
            fa = sum(r[who] == "split" and labels[r["group"]] != "split"
                     for r in judged)
            miss = sum(r[who] != "split" and labels[r["group"]] == "split"
                       for r in judged)
            lines.append(f"{who:6s} {right}/{len(judged)} as labelled; "
                         f"false split {fa}; missed split {miss}")
    text = "\n".join(lines)
    print(text)
    with io.open(os.path.join(out_dir, "summary.txt"), "w",
                 encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"\n-> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
