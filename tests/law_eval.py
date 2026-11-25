"""
THE RULES ASSISTANT'S SEARCH, MEASURED - does the right passage come back?

15 questions in an auditor's words (tests/law_questions.csv), each with what
a relevant passage is: its heading (the texts' own articles: "Valuation",
"Time Periods", "3.15.5 Advance Contract Award Notice"...) and/or a phrase
in its text. Written from the questions, before any search was run.

For each search mode - keywords (BM25), meaning (embeddings), hybrid (the
two fused) - it reports:
    hit@6   a relevant passage among the 6 the assistant is given
    MRR     1 / rank of the first relevant passage (0 if none in the top 50)

    python tenderlens.py eval laws            # embeddings from Ollama (once)
    python tenderlens.py --offline eval laws  # cached embeddings only
"""

import argparse
import csv
import io
import os
import re
import sys
import time

from tools import law_index

QUESTIONS = os.path.join("tests", "law_questions.csv")
K = 6
DEPTH = 50
OUT = os.path.join("reports", "law_eval")


def relevant(row):
    sx = re.compile(row["section"]) if row["section"] else None
    tx = re.compile(row["text"], re.I) if row["text"] else None
    return lambda p: (p["source"] != "rules"
                      and (not sx or bool(sx.search(p["section"])))
                      and (not tx or bool(tx.search(p["text"]))))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--modes", default="keywords,meaning,hybrid")
    a = ap.parse_args()
    offline = os.environ.get("TENDERLENS_OFFLINE") == "1"
    idx = law_index.load()
    with io.open(QUESTIONS, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    modes = [m for m in a.modes.split(",") if m]
    qvecs, note = {}, ""
    if any(m != "keywords" for m in modes):
        from tools import local_llm
        try:
            t = time.time()
            idx.attach_vectors(lambda *x: local_llm.embed(
                *x, cache_only=offline))
            vs = local_llm.embed([r["question"] for r in rows], "query",
                                 cache_only=offline)
            qvecs = {r["question"]: v for r, v in zip(rows, vs)}
            note = (f"embeddings: {local_llm.embed_model()}, "
                    f"{len(idx.vecs)} passages ({time.time() - t:.0f}s)")
        except local_llm.Unavailable as e:
            note = f"no embeddings ({e}): keywords only"
            modes = ["keywords"]
    lines, totals = [], {m: [0, 0.0] for m in modes}
    for r in rows:
        rel = relevant(r)
        n_rel = sum(map(rel, idx.passages))
        cells = []
        for m in modes:
            got = idx.search(r["question"], DEPTH, qvec=qvecs.get(
                r["question"]), mode=m)
            rank = next((i + 1 for i, p in enumerate(got) if rel(p)), 0)
            hit = 0 < rank <= K
            totals[m][0] += hit
            totals[m][1] += 1 / rank if rank else 0
            cells.append(f"{'yes' if hit else 'no ':3s} {rank or '-':>3}")
        lines.append(f"{r['question'][:58]:58s} {n_rel:3d}  "
                     + "   ".join(cells))
    n = len(rows)
    head = (f"{'question':58s} rel  " + "   ".join(f"{m[:7]:>7s}"
                                                  for m in modes))
    summary = [f"{m:9s} hit@{K} {t[0]}/{n}   MRR {t[1] / n:.2f}"
               for m, t in totals.items()]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    text = "\n".join([f"LAW SEARCH - {stamp} - {len(idx.passages)} passages"
                      f" - {note or 'keywords only'}", "", head] + lines
                     + ["", "(each cell: in the top 6?  rank of the first "
                            "relevant passage)", ""] + summary)
    print(text)
    os.makedirs(OUT, exist_ok=True)
    with io.open(os.path.join(OUT, f"{stamp}.txt"), "w",
                 encoding="utf-8") as f:
        f.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
