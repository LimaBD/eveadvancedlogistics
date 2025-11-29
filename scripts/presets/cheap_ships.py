"""Preset runner for the 'cheap_ships' ship-only hauling profile."""
from __future__ import annotations

import sys
from typing import Sequence

try:  # pragma: no cover - fallback for direct execution
    from ._runner import run_profile
except ImportError:  # pragma: no cover - executed when run as a plain script
    from _runner import run_profile  # type: ignore

PROFILE_NAME = "cheap_ships"
PROFILE_ARGS: Sequence[str] = [
    "--items",
    "data/ships.json",
    "--max-investment",
    "50000000",
    "--min-profit-per-jump",
    "1000000",
    "--max-items",
    "1",
    "--max-units",
    "1",
]


def main(argv: Sequence[str]) -> int:
    extra_args = list(argv)
    return run_profile(PROFILE_NAME, PROFILE_ARGS, extra_args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
