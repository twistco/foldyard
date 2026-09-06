"""`python -m foldyard` → the CLI. Used by the recipe hot path under a plain system
python3 (no venv): `PYTHONPATH=foldyard/src python3 -m foldyard mode env`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
