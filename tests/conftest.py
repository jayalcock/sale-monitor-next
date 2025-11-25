import sys
from pathlib import Path

# Ensure src/ is on sys.path so `sale_monitor` imports work without PYTHONPATH
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))