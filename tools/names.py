"""
BRAND AND COMPANY NAMES - one way to read them, for every comparison.

A name is read as its WORDS: lowercase letters and digits, any length ("GE",
"3M" and "AG" count), accents kept; everything else separates words. Each
comparison then states its own rule on those words:

    begins_with(name, shorter)   merging spellings (M2): "FormLabs Form"
                                 begins with "Formlabs"
    says(text, name)             M2's quote check: the quote holds the whole
                                 name, word after word ("General Electric"
                                 is not said by "General Motors")
    part_of(brand, company)      brand = winner (M4): every word of the brand
                                 is a word of the company's name ("GE
                                 Healthcare" is not part of "Siemens
                                 Healthcare")

No fuzzy matching: a near-match between a brand and a company is the kind
of coincidence that becomes an accusation.
"""

import re

RE_WORD = re.compile(r"[0-9a-zà-ÿ]+")


def words(s):
    return RE_WORD.findall((s or "").lower())


def begins_with(name, shorter):
    """The words of `name` start with all the words of `shorter`."""
    n, s = words(name), words(shorter)
    return bool(s) and n[:len(s)] == s


def says(text, name):
    """The whole name appears in the text, word after word, as words."""
    t, n = words(text), words(name)
    return bool(n) and any(t[i:i + len(n)] == n
                           for i in range(len(t) - len(n) + 1))


def part_of(brand, company):
    """Every word of the brand is a word of the company's name."""
    b = set(words(brand))
    return bool(b) and b <= set(words(company))
