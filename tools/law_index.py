"""
THE LAW INDEX - the law texts cut into passages, searched two ways and the
two rankings fused (hybrid search):

    keywords  BM25 over the words (a light stem, the auditor's words -> the
              texts' words). No model: it runs anywhere, finds exact terms
              ("B3000T", "art. 509").
    meaning   cosine between embeddings (a local model, tools/local_llm.py):
              finds a passage worded differently from the question ("pick
              one supplier without asking others" -> limited tendering).
    hybrid    Reciprocal Rank Fusion: each passage scores 1/(RRF_K + rank)
              in each ranking, summed. Ranks, not scores, are fused, so the
              two scales never have to be compared.
DEFAULT: hybrid when embeddings exist, else keywords (user decision
2026-11-25). Measured on 15 questions (tests/law_eval.py): hit@6 keywords
8, meaning 14, hybrid 13; MRR 0.25 / 0.70 / 0.53. Hybrid keeps the exact
terms (article numbers, "B3000T") that meaning alone can miss.

Corpus: the texts `python tenderlens.py prepare laws` wrote to reports/laws/
(official regulations, directive, trade agreements, Supply Manual), plus
the rules the system checks (tools/laws.py), so the index is never empty.

A passage is a few paragraphs (about PASSAGE_WORDS words) under one heading;
it never crosses a heading, so it keeps its section ("Article 509: ...").

    from tools import law_index
    idx = law_index.load()
    idx.search("brand name or equivalent", k=5)   # keywords only
    idx.attach_vectors(local_llm.embed)            # once: the passages' vectors
    idx.search(q, k=5, qvec=local_llm.embed([q], "query")[0])   # hybrid
    idx.read("cfta5:12")                           # -> the passage, neighbours
"""

import glob
import io
import math
import os
import re
from collections import Counter

from tools import laws, names

TEXT_DIR = os.path.join("reports", "laws")
PASSAGE_WORDS = 180
K1, B = 1.5, 0.75
RRF_K = 60              # the usual constant of Reciprocal Rank Fusion
POOL = 50               # passages taken from each ranking before fusing
STOP = set("""a an and are as at be by for from has have in is it its of on
or that the this to was were will with which who whom shall may must not
any all such other than then there these those their they under per""".split())


# The auditor's words -> the texts' words (added to a query, never to a
# passage): keyword search does not know that "split" is "divide".
SYNONYMS = {
    "split": "divide divided dividing", "splitting": "divide dividing",
    "brand": "trademark trade name", "brands": "trademark trade name",
    "sole": "non-competitive limited tendering without soliciting bids",
    "noncompetitive": "limited tendering without soliciting bids",
    "deadline": "time period", "deadlines": "time periods",
    "acan": "advance contract award notice",
    "threshold": "value valuation", "complaint": "challenge tribunal",
    "complain": "challenge tribunal", "fraud": "integrity",
}


def stem(w):
    """A light stem, so that divide, divided and dividing meet."""
    for suf in ("ing", "ed", "es", "e", "s"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            return w[:-len(suf)]
    return w


def tokens(text):
    return [stem(w) for w in names.words(text) if w not in STOP and len(w) > 1]


def query_tokens(query):
    words = names.words(query)
    extra = " ".join(SYNONYMS.get(w, "") for w in words)
    return tokens(query + " " + extra)


def read_source(path):
    """A text written by prepare laws -> (meta, body)."""
    with io.open(path, encoding="utf-8") as f:
        raw = f.read()
    head, _, body = raw.partition("\n\n")
    meta = dict(l.split(": ", 1) for l in head.splitlines() if ": " in l)
    return meta, body


def passages_of(sid, title, url, body):
    """Cut a text into passages: paragraphs grouped under their heading."""
    out, section, buf = [], "", []

    def flush():
        if buf:
            out.append({"id": f"{sid}:{len(out)}", "source": sid,
                        "title": title, "url": url, "section": section,
                        "text": "\n".join(buf)})
            buf.clear()

    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        if para.startswith("## "):
            flush()
            section = para[3:].strip()[:160]
            continue
        if buf and len(" ".join(buf).split()) + len(para.split()) \
                > PASSAGE_WORDS:
            flush()
        buf.append(para)
    flush()
    return out


def rules_passages():
    """The rules page itself, one passage per rule."""
    out = []
    for i, r in enumerate(laws.RULES):
        text = r["plain"] + (f" “{r['quote']}”" if r["quote"]
                             else "")
        out.append({"id": f"rules:{i}", "source": "rules",
                    "title": r["source"], "url": r["url"],
                    "section": r["title"], "text": text})
    return out


class Index:
    def __init__(self, passages):
        self.passages = passages
        self.by_id = {p["id"]: i for i, p in enumerate(passages)}
        self.tf = [Counter(tokens(p["section"] + " " + p["text"]))
                   for p in passages]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = (sum(self.len) / len(self.len)) if passages else 1
        df = Counter(w for t in self.tf for w in t)
        n = len(passages)
        self.idf = {w: math.log(1 + (n - d + 0.5) / (d + 0.5))
                    for w, d in df.items()}
        self.vecs = None

    def attach_vectors(self, embed):
        """The passages' vectors: embed(texts, kind, titles) -> unit vectors
        (cached by the embedder, so only new passages cost anything)."""
        if self.vecs is None:
            self.vecs = embed([p["section"] + "\n" + p["text"]
                               for p in self.passages], "document",
                              [p["title"] for p in self.passages])
        return self

    def sources(self):
        """(id, title) of each official text; the rules page is "rules"."""
        return sorted({(p["source"], p["title"]) for p in self.passages
                       if p["source"] != "rules"}) + [
            ("rules", "TenderLens: the rules the system checks")]

    def keywords(self, query, source=None):
        """[(BM25 score, passage index)], best first."""
        q = query_tokens(query)
        scored = []
        for i, tf in enumerate(self.tf):
            if source and self.passages[i]["source"] != source:
                continue
            s = 0.0
            for w in q:
                f = tf.get(w)
                if f:
                    s += self.idf[w] * f * (K1 + 1) / (
                        f + K1 * (1 - B + B * self.len[i] / self.avg))
            if s > 0:
                scored.append((s, i))
        return sorted(scored, reverse=True)

    def meaning(self, qvec, source=None):
        """[(cosine, passage index)], best first (unit vectors: a dot)."""
        scored = [(sum(a * b for a, b in zip(qvec, v)), i)
                  for i, v in enumerate(self.vecs or [])
                  if not source or self.passages[i]["source"] == source]
        return sorted(scored, reverse=True)

    def search(self, query, k=5, source=None, qvec=None, mode=None):
        """The k best passages. mode: "keywords", "meaning" or "hybrid";
        default hybrid when a query vector and the passages' vectors
        exist, else keywords. Each passage says how it was found ("how")."""
        mode = mode or ("hybrid" if qvec and self.vecs else "keywords")
        kw = self.keywords(query, source) if mode != "meaning" else []
        mn = (self.meaning(qvec, source) if mode != "keywords" and qvec
              and self.vecs else [])
        if mode == "keywords":
            top = [(s, i, "keywords") for s, i in kw[:k]]
        elif mode == "meaning":
            top = [(s, i, "meaning") for s, i in mn[:k]]
        else:
            fused, found = {}, {}
            for name, ranking in (("keywords", kw), ("meaning", mn)):
                for rank, (_, i) in enumerate(ranking[:POOL]):
                    fused[i] = fused.get(i, 0.0) + 1.0 / (RRF_K + rank + 1)
                    found.setdefault(i, []).append(name)
            best = sorted(fused.items(), key=lambda x: -x[1])[:k]
            top = [(sc, i, " + ".join(found[i])) for i, sc in best]
        return [dict(self.passages[i], score=round(sc, 4), how=how)
                for sc, i, how in top]

    def get(self, pid):
        i = self.by_id.get(str(pid).strip())
        return None if i is None else self.passages[i]

    def read(self, pid):
        """A passage with the one before and after it in the same text."""
        i = self.by_id.get(str(pid).strip())
        if i is None:
            return []
        src = self.passages[i]["source"]
        return [self.passages[j] for j in (i - 1, i, i + 1)
                if 0 <= j < len(self.passages)
                and self.passages[j]["source"] == src]


_CACHE = {}


def load(text_dir=TEXT_DIR):
    """The index over every text in text_dir, plus the rules page. Rebuilt
    when a text changes."""
    paths = sorted(glob.glob(os.path.join(text_dir, "*.txt")))
    key = (text_dir, tuple((p, os.path.getmtime(p)) for p in paths))
    if key not in _CACHE:
        ps = rules_passages()
        for p in paths:
            meta, body = read_source(p)
            sid = os.path.splitext(os.path.basename(p))[0]
            ps += passages_of(sid, meta.get("TITLE", sid),
                              meta.get("URL", ""), body)
        _CACHE.clear()
        _CACHE[key] = Index(ps)
    return _CACHE[key]
