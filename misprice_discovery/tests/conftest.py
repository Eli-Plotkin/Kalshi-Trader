"""Pytest config: make misprice_discovery/ importable as a flat module root.

The package modules use unqualified imports (`from helpers import ...`,
`from build_nba_research_dataset import ...`) because they're run directly as
scripts. Adding the package dir to sys.path lets tests resolve those imports
without touching the source files.
"""

import os
import sys

PACKAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)
