"""Make the repo root importable (``common``, ``filings``, ``price_alerts``)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
