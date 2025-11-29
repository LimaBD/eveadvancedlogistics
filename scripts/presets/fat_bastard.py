"""Preset runner for the 'fat_bastard' cargo-hauling profile."""
from __future__ import annotations

import sys
from typing import Sequence

try:  # pragma: no cover - fallback for direct execution
    from ._runner import run_profile
except ImportError:  # pragma: no cover - executed when run as a plain script
    from _runner import run_profile  # type: ignore

PROFILE_NAME = "fat_bastard"
PROFILE_ARGS: Sequence[str] = [
    "--forge-adjacent-only",
    "--max-total-jumps",
    "8",
    "--max-investment",
    "50000000",
    "--max-volume",
    "33000",
    "--current-system",
    "Jita",
]


def main(argv: Sequence[str]) -> int:
    extra_args = list(argv)
    return run_profile(PROFILE_NAME, PROFILE_ARGS, extra_args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
