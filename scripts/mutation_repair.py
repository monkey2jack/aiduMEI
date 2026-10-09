#!/usr/bin/env python3
"""Source-checkout wrapper; installed entry point: ducky.mutation_repair:main."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ducky.mutation_repair import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
