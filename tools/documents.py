"""
DOCUMENTS - one tender's text, and where things sit in it.

Every tool that reads a tender goes through here:

    tender_documents(ref)            the tender's extracted documents
                                     (reports/text/<category>/<ref>__*.txt)
    requirement_mask / _view         which lines are the requirements section
    question_mask(lines)             which lines are a bidder speaking (Q&A)
    is_amendment / amendment_view    an amendment, reduced to what the buyer
                                     changes or answers
    quote_in_documents(docs, ...)    are these words really in the document?
                                     Every quote an agent gives is checked here.
"""


import os
import re

from prepare import census

TEXT_DIR = "reports/text"


def tender_documents(reference, text_dir=None):
    """Every extracted document belonging to one tender.
    text_dir defaults to the module's TEXT_DIR, read at call time so a
    test run (tests/synth_rig.py) can point the whole system elsewhere."""
    text_dir = text_dir or TEXT_DIR
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


# Bidders' questions in an amendment's Q&A. Text between "Question #11" and
# "Answer #11" is written by a SUPPLIER, so a brand in it is the supplier
# naming its own product, never the buyer requiring one.
#
# Measured 2026-10-15: the first --online run over 2510 produced exactly one
# level-1 finding, and it was this - "3.7 We use a Honda GX390 engine mounted
# in rear of gooseneck. Is this sufficient...?" under Question #11, answered
# "The engine must meet the requirements of the purchase description." The
# model-code rule (decision 29) correctly saw "Honda GX390"; it could not see
# who was speaking.
# The whole word is safe followed by any number. A single letter is not:
# "A 5.08 cm (2 in) rear receiver hitch" would read as Answer 5, and a loose
# "Q" could open a question block over a real requirement and hide it. So a
# single letter needs a separator right after its number - "Q1:", "A1.",
# "Q 3)" - and a dot only if no digit follows it, which rules out decimals.
_QA_NUM = r"\s*(?:#|no\.?|n[oº]\.?)?\s*\d+"
_QA_SEP = r"\s*(?:[:)\-]|\.(?!\d)|$)"
RE_QUESTION = re.compile(
    rf"^\s*(?:question{_QA_NUM}\b|q{_QA_NUM}{_QA_SEP})", re.I)
# Answers are often unnumbered ("Answer:", "Response:"). Requiring a number
# left 26 files ending INSIDE a question block, hiding the buyer's answers -
# which are buyer text and can carry real requirements.
# Measured 2026-10-17: "a) Seating must be ..." - a list item inside a
# bidder's question - matched the loose form as an answer marker and closed
# the question at once, so the bidder's "FORD F550" became a finding. A
# single letter now counts only as a capital A or R with a colon or dash
# ("A:", "R -") or with a number ("A1.", "R 2)").
RE_ANSWER = re.compile(
    rf"^\s*(?:(?i:answer|response|réponse|reply)\b"
    rf"|[AR]{_QA_NUM}{_QA_SEP}|[AR]\s*[:\-])")


QA_LOOKAHEAD = 40       # lines; 0 = close a question at its first blank line


# Decided 2026-10-17 (user): in an AMENDMENT, level 1 reads only what the
# buyer changes or answers - change clauses ("is deleted and replaced",
# "insert", "add the following"), answer blocks, and any requirement section
# the amendment restates (some reissue the whole RFP). Everything else is
# blanked before candidates are looked for. Both false alarms so far came
# from bidders' text in amendments (Honda GX390, ProHeat X45). The "or
# equivalent" check still reads the WHOLE amendment: a buyer's allowance
# anywhere still clears.
RE_AMEND_NAME = re.compile(r"amend|amd|modif|addend|(?<![a-z])q&?a\d", re.I)
RE_AMEND_HEAD = re.compile(
    r"\b(?:amendment|amendement|modification|modificatif|addendum)\s*"
    r"(?:no\.?|n[o°º]\.?|#|number)?\s*\d", re.I)
RE_CHANGE = re.compile(
    r"\b(?:delete[sd]?|replace[sd]?|insert(?:ed)?|add(?:ed)?\s+(?:the\s+"
    r"following|to)|is\s+(?:amended|added|revised)|amended\s+to\s+read|"
    r"now\s+reads?|shall\s+now|supprim\w*|remplac\w*|ajout\w*|insér\w*|"
    r"est\s+modifi\w*)\b", re.I)
AMEND_BLOCK = 40        # lines kept after a change clause or an answer


def requirement_mask(lines):
    """(one bool per line: inside the requirements section, "section"), or
    (all True, "whole") when no section is found."""
    try:
        mask, mode = census.scope_mask(lines)
        if mode == "section":
            return list(mask), mode
    except Exception:
        pass
    return [True] * len(lines), "whole"


def requirement_view(text):
    """The document with everything outside its requirements section blanked
    - same length, so offsets and line numbers still point into the
    original."""
    lines = text.split("\n")
    mask, mode = requirement_mask(lines)
    if mode != "section":
        return text
    return "\n".join(l if k else " " * len(l) for l, k in zip(lines, mask))


def _norm_quote(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def quote_in_documents(docs, file, line):
    """Are these words really in the named document? (else in any of the
    tender's documents, when the name is written another way). A line cut
    with '...' counts if every piece is there, in order."""
    parts = [_norm_quote(p)
             for p in re.split(r"\.\.\.+|\u2026", str(line or ""))]
    parts = [p for p in parts if p]
    if not parts or sum(len(p) for p in parts) < 4:
        return False
    named = [d for d in docs if d["document"] == file]
    for d in named or docs:
        text, at = d.setdefault("_norm", _norm_quote(d["text"])), 0
        for p in parts:
            at = text.find(p, at)
            if at < 0:
                break
            at += len(p)
        else:
            return True
    return False


def is_amendment(doc):
    """By file name, or by the cover page ("Amendment No. ... 002")."""
    if RE_AMEND_NAME.search(doc.get("document", "")):
        return True
    head = [l for l in doc["text"].splitlines()[:200] if l.strip()][:25]
    return bool(RE_AMEND_HEAD.search("\n".join(head)))


def amendment_view(text):
    """The amendment with everything but buyer changes, answers and restated
    requirement sections blanked - same length, so every character offset and
    line number still points into the original."""
    lines = text.split("\n")
    keep = [False] * len(lines)
    mask, mode = requirement_mask(lines)
    if mode == "section":
        keep = list(mask)
    # A kept block is one paragraph: it ends at the first blank line after
    # it has some text past the marker line, at a bidder's question, or
    # after AMEND_BLOCK lines.
    left, has = 0, False
    for i, line in enumerate(lines):
        if RE_QUESTION.match(line):
            left = 0                       # a bidder speaks: stop keeping
            # visible, so _candidates knows the text after it is a
            # bidder's, even where that text holds a change word
            keep[i] = True
        elif RE_ANSWER.match(line) or RE_CHANGE.search(line):
            left, has = AMEND_BLOCK, False
            keep[i] = True
            left -= 1
            continue
        if left:
            if not line.strip():
                if has:
                    left = 0
                    continue
            else:
                has = True
            keep[i] = True
            left -= 1
    return "\n".join(l if k else " " * len(l) for l, k in zip(lines, keep))


def question_mask(lines):
    """For each line: is it inside a bidder's question (amendment Q&A)?
    Shared by the brand rules (_candidates) and the M2 agent, so both use one
    definition of "a bidder is speaking".
    A bidder's question is ONE PARAGRAPH: the block closes at an answer
    marker, or at the first blank line after the question's text. Without
    the blank-line close, a stray "Question 7" inside a catalogue RFSO
    body opened a block of 4,943 lines and hid most of its specification.
    Measured 2026-10-17: the blank-line close let a real bidder's text
    through - "Question 28" quotes the spec clause, a blank line, then
    "We will not provide a battery blanket as the coach is configured with
    a ProHeat X45 Plus ..." read as the buyer naming a brand. So when an
    answer marker follows within QA_LOOKAHEAD lines, the block runs to it;
    only without one does the blank-line close apply.
    """
    mask = []
    in_question = has_text = False
    until = -1
    for i, line in enumerate(lines):
        mq = RE_QUESTION.match(line)
        if mq:
            in_question = True
            has_text = bool(line[mq.end():].strip(" :.)-#\t"))
            until = -1
            for j in range(i + 1, min(len(lines), i + 1 + QA_LOOKAHEAD)):
                if RE_QUESTION.match(lines[j]):
                    break
                if RE_ANSWER.match(lines[j]):
                    until = j
                    break
        elif in_question:
            if RE_ANSWER.match(line):
                in_question = False
            elif i < until:
                pass               # an answer is coming: still the question
            elif not line.strip():
                if has_text:
                    in_question = False
            else:
                has_text = True
        mask.append(in_question)
    return mask
