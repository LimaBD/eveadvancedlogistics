"""Calculate jump counts locally using a cached stargate graph."""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Mapping, Optional, Tuple

DEFAULT_SYSTEMS_PATH = Path("data/systems.json")
DEFAULT_GRAPH_PATH = Path("data/system_graph.json")
VALID_FLAGS = ("shortest", "secure", "insecure")


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


def load_system_indexes(systems_path: Path) -> tuple[Dict[str, int], Dict[int, str], Dict[int, int]]:
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
    system_regions: Dict[int, int] = {}
    for entry in data:
        if "id" not in entry or "name" not in entry:
            continue
        system_id = int(entry["id"])
        system_name = str(entry["name"])
        region_id = entry.get("region_id")
        name_to_id[system_name.lower()] = system_id
        id_to_name[system_id] = system_name
        if region_id is not None:
            try:
                system_regions[system_id] = int(region_id)
            except (TypeError, ValueError):
                continue

    if not name_to_id:
        raise ValueError(f"No valid system records found in {systems_path}")

    return name_to_id, id_to_name, system_regions


def resolve_system_identifier(
    raw_value: str | int, name_to_id: Mapping[str, int], id_to_name: Mapping[int, str]
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


def load_graph(graph_path: Path) -> Dict[int, Tuple[int, ...]]:
    if not graph_path.exists():
        raise FileNotFoundError(
            f"Graph file {graph_path} not found. Run scripts/build_system_graph.py first."
        )
    with graph_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    systems_block = raw.get("systems") if isinstance(raw, Mapping) else None
    if not isinstance(systems_block, Mapping):
        raise ValueError(f"Graph file {graph_path} missing 'systems' mapping")
    adjacency: Dict[int, Tuple[int, ...]] = {}
    for raw_system_id, neighbors in systems_block.items():
        try:
            system_id = int(raw_system_id)
        except (TypeError, ValueError):
            continue
        if not isinstance(neighbors, Iterable):
            continue
        normalized_neighbors: List[int] = []
        for neighbor in neighbors:
            try:
                normalized_neighbors.append(int(neighbor))
            except (TypeError, ValueError):
                continue
        adjacency[system_id] = tuple(sorted(set(normalized_neighbors)))
    if not adjacency:
        raise ValueError(f"Graph file {graph_path} contained no adjacency entries")
    return adjacency


def reconstruct_route(
    visited: Mapping[int, int | None], destination_id: int
) -> Tuple[int, ...]:
    route: List[int] = []
    current = destination_id
    while current is not None:
        route.append(current)
        current = visited[current]
    route.reverse()
    return tuple(route)


def shortest_route(
    adjacency: Mapping[int, Tuple[int, ...]], origin_id: int, destination_id: int
) -> Tuple[int, ...]:
    if origin_id == destination_id:
        return (origin_id,)

    frontier: Deque[int] = deque([origin_id])
    visited: Dict[int, int | None] = {origin_id: None}

    while frontier:
        current = frontier.popleft()
        for neighbor in adjacency.get(current, ()):  # type: ignore[arg-type]
            if neighbor in visited:
                continue
            visited[neighbor] = current
            if neighbor == destination_id:
                return reconstruct_route(visited, destination_id)
            frontier.append(neighbor)

    raise ValueError(
        f"No path between system ids {origin_id} and {destination_id} found in local graph"
    )


class LocalJumpCache:
    """In-memory jump-distance cache backed by the local stargate graph."""

    def __init__(self, *, graph_path: Path | str = DEFAULT_GRAPH_PATH, flag: str = "shortest"):
        if flag not in VALID_FLAGS:
            raise ValueError(
                f"Flag '{flag}' is not supported offline. Choose from {sorted(VALID_FLAGS)}"
            )
        if flag != "shortest":
            print(
                f"Warning: offline routing only supports 'shortest'; treating '{flag}' as 'shortest'.",
                file=sys.stderr,
            )
        self.graph_path = Path(graph_path)
        self.flag = "shortest"
        self._adjacency = load_graph(self.graph_path)
        self._cache: Dict[Tuple[int, int], Optional[int]] = {}

    def jumps(self, origin_id: int, destination_id: int) -> Optional[int]:
        if origin_id == destination_id:
            return 0
        key = (origin_id, destination_id)
        if key in self._cache:
            return self._cache[key]
        try:
            route = shortest_route(self._adjacency, origin_id, destination_id)
        except ValueError:
            self._cache[key] = None
            return None
        jumps = max(0, len(route) - 1)
        self._cache[key] = jumps
        self._cache[(destination_id, origin_id)] = jumps
        return jumps

    def flush(self) -> None:
        """Compatibility shim for legacy callers; no-op for local planner."""
        pass


def format_result(route: Tuple[int, ...], id_to_name: Mapping[int, str]) -> JumpResult:
    origin_id = route[0]
    destination_id = route[-1]
    origin_name = id_to_name.get(origin_id, str(origin_id))
    destination_name = id_to_name.get(destination_id, str(destination_id))
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
        description="Calculate jump counts using a locally cached stargate graph."
    )
    parser.add_argument("origin", help="Origin system name or ID")
    parser.add_argument("destination", help="Destination system name or ID")
    parser.add_argument(
        "--graph-file",
        type=Path,
        default=DEFAULT_GRAPH_PATH,
        help="Path to the generated system graph (default: data/system_graph.json).",
    )
    parser.add_argument(
        "--systems-file",
        type=Path,
        default=DEFAULT_SYSTEMS_PATH,
        help="Path to systems metadata exported by fetch_systems.py (default: data/systems.json).",
    )
    parser.add_argument(
        "--show-route",
        action="store_true",
        help="Print the full per-system route instead of only the jump count.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    adjacency = load_graph(args.graph_file)
    name_to_id, id_to_name, _ = load_system_indexes(args.systems_file)
    origin_id = resolve_system_identifier(args.origin, name_to_id, id_to_name)
    destination_id = resolve_system_identifier(args.destination, name_to_id, id_to_name)
    route = shortest_route(adjacency, origin_id, destination_id)
    result = format_result(route, id_to_name)

    print(f"Route (local shortest) from {result.origin_name} -> {result.destination_name}: {result.jumps} jump(s).")

    if args.show_route:
        print("Route path:")
        for index, system_name in enumerate(result.route_names(), start=1):
            print(f"  {index:02d}. {system_name}")


if __name__ == "__main__":
    main()
