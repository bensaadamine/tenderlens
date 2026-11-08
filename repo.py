"""
The repository layout, in one place. Every script imports this first.

The code is split by role - agents/, tools/, prepare/, tests/ - and modules
import each other by plain name (`import brands`). Importing `repo` puts
those folders on the import path and makes the repository root the working
folder, so data paths (data/, reports/) mean the same thing whatever folder
a script was started from.
"""

import importlib.util
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
for _d in ("agents", "tools", "prepare", "tests", "experiments", ""):
    _p = os.path.join(ROOT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.chdir(ROOT)

_census = None


def census():
    """prepare/04_census.py as a module - its name starts with a digit, so
    it cannot be imported by name. Loaded once, shared by every caller."""
    global _census
    if _census is None:
        spec = importlib.util.spec_from_file_location(
            "census", os.path.join(ROOT, "prepare", "04_census.py"))
        _census = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_census)
    return _census
