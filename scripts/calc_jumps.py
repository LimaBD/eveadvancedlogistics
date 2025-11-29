"""CLI and helper utilities to compute the number of jumps between EVE systems."""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Tuple

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
ROUTE_URL = f"{ESI_BASE}/route/{{origin}}/{{destination}}/"
USER_AGENT = "EveAdvancedLogistics/0.1 (jump calculator)"
REQUEST_TIMEOUT = 15
DEFAULT_SYSTEMS_PATH = Path("data/systems.json")
VALID_FLAGS = {"shortest", "secure", "insecure"}


@dataclass(frozen=True)
class JumpResult:
    """Structured result for reuse in other scripts."""

    origin_id: int
    destination_id: int
    origin_name: str
    destination_name: str
    route: Tuple[int, ...]
    name_lookup: Mapping[int, str]

    @property
    def jumps(self) -> int:
        return max(0, len(self.route) - 1)

    def route_names(self) -> Tuple[str, ...]:
        return tuple(self.name_lookup.get(system_id, str(system_id)) for system_id in self.route)


def load_system_indexes(systems_path: Path) -> tuple[Dict[str, int], Dict[int, str]]:
    """Build lookup tables from the cached systems export."""
    if not systems_path.exists():
        raise FileNotFoundError(
            f"Systems file {systems_path} not found. Run scripts/fetch_systems.py first."
        )

    with systems_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if not isinstance(data, list):
        raise ValueError(f"Unexpected structure inside {systems_path}")

    name_to_id: Dict[str, int] = {}
    id_to_name: Dict[int, str] = {}
    for entry in data:
        if "id" not in entry or "name" not in entry:
            continue
        system_id = int(entry["id"])
        system_name = str(entry["name"])
        name_to_id[system_name.lower()] = system_id
        id_to_name[system_id] = system_name

    if not name_to_id:
        raise ValueError(f"No valid system records found in {systems_path}")

    return name_to_id, id_to_name


def resolve_system_identifier(
    raw_value: str | int, name_to_id: Dict[str, int], id_to_name: Dict[int, str]
) -> int:
    """Resolve a provided system name or ID using the cached metadata."""
    if isinstance(raw_value, int):
        if raw_value not in id_to_name:
            raise ValueError(f"System ID {raw_value} not found in local cache")
        return raw_value

    value = str(raw_value).strip()
    if not value:
        raise ValueError("System value cannot be empty")

    if value.isdigit():
        system_id = int(value)
        if system_id not in id_to_name:
            raise ValueError(f"System ID {system_id} not found in local cache")
        return system_id

    lookup_key = value.lower()
    if lookup_key not in name_to_id:
        raise ValueError(f"System name '{value}' not found in local cache")
    return name_to_id[lookup_key]


def fetch_route(origin_id: int, destination_id: int, flag: str) -> Tuple[int, ...]:
    if flag not in VALID_FLAGS:
        raise ValueError(f"Flag '{flag}' is not supported. Choose from {sorted(VALID_FLAGS)}")

    params = {"datasource": DATASOURCE, "flag": flag}
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    url = ROUTE_URL.format(origin=origin_id, destination=destination_id)
    response = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Unexpected response shape from route API")
    return tuple(int(system_id) for system_id in payload)


def calculate_jump_count(
    origin: str | int,
    destination: str | int,
    *,
    flag: str = "shortest",
    systems_path: Path | str = DEFAULT_SYSTEMS_PATH,
) -> JumpResult:
    """Resolve both endpoints and fetch a route from ESI."""
    systems_file = Path(systems_path)
    name_to_id, id_to_name = load_system_indexes(systems_file)

    origin_id = resolve_system_identifier(origin, name_to_id, id_to_name)
    destination_id = resolve_system_identifier(destination, name_to_id, id_to_name)

    route = fetch_route(origin_id, destination_id, flag)
    origin_name = id_to_name[origin_id]
    destination_name = id_to_name[destination_id]
    return JumpResult(
        origin_id=origin_id,
        destination_id=destination_id,
        origin_name=origin_name,
        destination_name=destination_name,
        route=route,
        name_lookup=id_to_name,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Calculate jump counts between two EVE systems using the ESI route API."
    )
    parser.add_argument("origin", help="Origin system name or ID")
    parser.add_argument("destination", help="Destination system name or ID")
    parser.add_argument(
        "--flag",
        default="shortest",
        choices=sorted(VALID_FLAGS),
        help="Routing mode supported by ESI (shortest, secure, insecure).",
    )
    parser.add_argument(
        "--systems-file",
        type=Path,
        default=DEFAULT_SYSTEMS_PATH,
        help="Path to systems JSON exported by fetch_systems.py (default: data/systems.json).",
    )
    parser.add_argument(
        "--show-route",
        action="store_true",
        help="Print the full per-system route instead of only the jump count.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = calculate_jump_count(
        args.origin,
        args.destination,
        flag=args.flag,
        systems_path=args.systems_file,
    )

    print(
        f"Route ({args.flag}) from {result.origin_name} -> {result.destination_name}: {result.jumps} jump(s)."
    )

    if args.show_route:
        print("Route path:")
        for index, system_name in enumerate(result.route_names(), start=1):
            print(f"  {index:02d}. {system_name}")


if __name__ == "__main__":
    main()
