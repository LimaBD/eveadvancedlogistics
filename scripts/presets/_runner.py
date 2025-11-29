"""Helper utilities for running item_arbitrage with canned presets."""
from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Sequence

ITEM_ARBITRAGE_PATH = Path(__file__).resolve().parents[1] / "item_arbitrage.py"


def _format_command(cmd: Sequence[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def run_profile(profile_name: str, profile_args: Sequence[str], extra_args: Sequence[str]) -> int:
    if not ITEM_ARBITRAGE_PATH.exists():
        raise FileNotFoundError(
            f"Expected item_arbitrage.py at {ITEM_ARBITRAGE_PATH}, but it does not exist."
        )
    command = [sys.executable, str(ITEM_ARBITRAGE_PATH), *profile_args, *extra_args]
    pretty_command = _format_command(command)
    print(f"Running preset '{profile_name}' via:\n  {pretty_command}\n")
    result = subprocess.run(command, check=False)
    return result.returncode
