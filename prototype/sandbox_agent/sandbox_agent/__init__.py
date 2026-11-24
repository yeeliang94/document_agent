"""Command line, test packets, scoring and experiments for the sandbox engine in app/engine.

The engine itself lives in the main app (app/engine), so the app and this prototype run the same code. This folder
keeps its own Python environment; the line below lets it import the engine from the repository root.
"""

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[3])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
