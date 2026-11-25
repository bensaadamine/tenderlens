"""
THE LAW TEXTS - download the official texts the rules assistant reads, once,
and keep their text.

    python tenderlens.py prepare laws            # Windows (needs the network)
    python tenderlens.py prepare laws --text     # re-extract the text only

Each source is downloaded to data/laws/<id>.<html|pdf> and its text written
to reports/laws/<id>.txt, headed by its title, address and date. A failed
download keeps the previous file and is reported; the others go on. The
list of sources is SOURCES below: official government and WTO pages.
"""

import argparse
import html
import io
import os
import re
import sys
import time
from html.parser import HTMLParser

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

RAW_DIR = os.path.join("data", "laws")
TEXT_DIR = os.path.join("reports", "laws")
INTL = ("https://www.international.gc.ca/trade-commerce/"
        "trade-agreements-accords-commerciaux/agr-acc/")
SM = ("https://canadabuys.canada.ca/en/how-procurement-works/"
      "policies-and-guidelines/supply-manual/")

SOURCES = [
    ("gcr", "Government Contracts Regulations (SOR/87-402)",
     "https://laws-lois.justice.gc.ca/eng/regulations/SOR-87-402/"
     "FullText.html"),
    ("dmp", "Treasury Board Directive on the Management of Procurement",
     "https://www.tbs-sct.canada.ca/pol/doc-eng.aspx?id=32692"),
    ("cfta5", "Canadian Free Trade Agreement, Chapter Five: Government "
              "Procurement",
     "https://www.cfta-alec.ca/cfta-agreement/"
     "chapter-five-government-procurement"),
    ("sm3", "Supply Manual, Chapter 3: Procurement Strategy", SM + "chapter-3"),
    ("sm3a", "Supply Manual, Chapter 3: Annexes", SM + "chapter-3-annexes"),
    ("cusma13", "CUSMA, Chapter 13: Government Procurement",
     INTL + "cusma-aceum/text-texte/13.aspx?lang=eng"),
    ("ceta19", "CETA, Chapter 19: Government Procurement",
     INTL + "ceta-aecg/text-texte/19.aspx?lang=eng"),
    ("gpa", "WTO Agreement on Government Procurement (2012)",
     "https://www.wto.org/english/docs_e/legal_e/rev-gpr-94_01_e.htm"),
    ("citt", "CITT procurement inquiries guide",
     "https://citt-tcce.gc.ca/en/procurement-inquiries/"
     "procurement-inquiries-guide"),
]

BLOCK = {"p", "div", "li", "tr", "br", "section", "article", "table",
         "ul", "ol", "dl", "dt", "dd", "blockquote", "pre", "figure"}
HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}
SKIP = {"script", "style", "nav", "header", "footer", "noscript", "svg",
        "form", "button", "select", "aside"}


class Text(HTMLParser):
    """An HTML page -> plain text: one block per line, headings marked
    '## ', menus and scripts left out."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.head = [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP:
            self.skip += 1
        elif tag in HEAD:
            self.out.append("\n\n## ")
            self.head = True
        elif tag in BLOCK:
            self.out.append("\n")
        elif tag in ("td", "th"):
            self.out.append(" | ")

    def handle_endtag(self, tag):
        if tag in SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in HEAD:
            self.out.append("\n\n")
            self.head = False
        elif tag in BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(re.sub(r"\s+", " ", data))


def html_text(raw):
    p = Text()
    p.feed(raw)
    text = "".join(p.out)
    lines = [l.strip(" |") for l in text.splitlines()]
    text = "\n".join(l for l in lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def pdf_text(path):
    from pdfminer.high_level import extract_text
    return extract_text(path)


def extract(sid, title, url, raw_path):
    if raw_path.endswith(".pdf"):
        body = pdf_text(raw_path)
    else:
        with io.open(raw_path, encoding="utf-8", errors="replace") as f:
            body = html_text(f.read())
    stamp = time.strftime("%Y-%m-%d", time.localtime(
        os.path.getmtime(raw_path)))
    os.makedirs(TEXT_DIR, exist_ok=True)
    out = os.path.join(TEXT_DIR, f"{sid}.txt")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(f"TITLE: {title}\nURL: {url}\nFETCHED: {stamp}\n\n{body}\n")
    return out, len(body.split())


def download(sess, sid, url, timeout=60):
    r = sess.get(url, timeout=timeout,
                 headers={"User-Agent": "TenderLens/0.1 (research)"})
    r.raise_for_status()
    pdf = (r.content[:5] == b"%PDF-")
    path = os.path.join(RAW_DIR, f"{sid}.{'pdf' if pdf else 'html'}")
    tmp = path + ".part"
    os.makedirs(RAW_DIR, exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, path)
    return path


def raw_path(sid):
    for ext in ("html", "pdf"):
        p = os.path.join(RAW_DIR, f"{sid}.{ext}")
        if os.path.exists(p):
            return p
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--text", action="store_true",
                    help="re-extract the text from the files already here")
    ap.add_argument("--only", default="", help="comma-separated source ids")
    a = ap.parse_args()
    only = {x.strip() for x in a.only.split(",") if x.strip()}
    sess = None
    if not a.text:
        import requests
        sess = requests.Session()
    failed = []
    for sid, title, url in SOURCES:
        if only and sid not in only:
            continue
        if sess is not None:
            try:
                download(sess, sid, url)
            except Exception as e:
                failed.append(sid)
                print(f"  {sid:8s} download failed: {str(e)[:120]}")
        p = raw_path(sid)
        if not p:
            print(f"  {sid:8s} no file")
            continue
        out, words = extract(sid, title, url, p)
        print(f"  {sid:8s} {words:>7,} words  {title}")
    print(f"\n-> {TEXT_DIR}" + (f"  ({len(failed)} failed: "
                               f"{', '.join(failed)})" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
