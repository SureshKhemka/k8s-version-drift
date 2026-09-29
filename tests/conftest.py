import sys
from pathlib import Path

# Lets tests reuse the eval harness (evals.support) for realistic fact sheets.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
