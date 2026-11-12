import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[3])  # the repository root, so app.engine imports
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
