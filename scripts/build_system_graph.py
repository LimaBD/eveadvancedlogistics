"""Generate a local stargate adjacency graph for EVE systems."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Set

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
SYSTEMS_URL = f"{ESI_BASE}/universe/systems/?datasource={DATASOURCE}"
SYSTEM_DETAIL_URL = f"{ESI_BASE}/universe/systems/{{system_id}}/?datasource={DATASOURCE}"
STARGATE_DETAIL_URL = f"{ESI_BASE}/universe/stargates/{{stargate_id}}/?datasource={DATASOURCE}"
USER_AGENT = "EveAdvancedLogistics/0.1 (graph builder)"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
DEFAULT_SYSTEMS_PATH = Path("data/systems.json")
DEFAULT_GRAPH_PATH = Path("data/system_graph.json")


def fetch_with_retries(session: requests.Session, url: str) -> Mapping:
    """Fetch a JSON payload with simple retry/backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, Mapping) or isinstance(payload, list):
                return payload
            raise ValueError(f"Unexpected payload structure for {url}")
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Failed to fetch {url}") from exc
            time.sleep(BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"Failed to fetch {url}")


def load_system_ids(systems_path: Path | None, session: requests.Session) -> List[int]:
    """Prefer IDs from a local systems file, otherwise fall back to ESI."""
    if systems_path and systems_path.exists():
        with systems_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, list):
            raise ValueError(f"Unexpected structure inside {systems_path}")
        ids = []
        for entry in data:
            entry_id = entry.get("id")
            if isinstance(entry_id, int):
                ids.append(entry_id)
        if not ids:
            raise ValueError(f"No usable IDs found in {systems_path}")
        return ids

    payload = fetch_with_retries(session, SYSTEMS_URL)
    if not isinstance(payload, list):
        raise ValueError("Unexpected payload for systems list")
    return [int(system_id) for system_id in payload]


def fetch_system_stargates(session: requests.Session, system_id: int) -> List[int]:
    url = SYSTEM_DETAIL_URL.format(system_id=system_id)
    payload = fetch_with_retries(session, url)
    stargates = payload.get("stargates") if isinstance(payload, Mapping) else None
    if not stargates:
        return []
    return [int(gate_id) for gate_id in stargates]


def fetch_stargate_destination(session: requests.Session, stargate_id: int) -> int:
    url = STARGATE_DETAIL_URL.format(stargate_id=stargate_id)
    payload = fetch_with_retries(session, url)
    destination = payload.get("destination") if isinstance(payload, Mapping) else None
    if not isinstance(destination, Mapping):
        raise ValueError(f"Stargate {stargate_id} missing destination block")
    system_id = destination.get("system_id")
    if system_id is None:
        raise ValueError(f"Stargate {stargate_id} missing destination.system_id")
    return int(system_id)


def gather_system_stargates(
    session: requests.Session, system_ids: Sequence[int], max_workers: int
) -> Dict[int, List[int]]:
    """Fetch stargate ids for every system in parallel."""
    system_to_gates: Dict[int, List[int]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_system_stargates, session, system_id): system_id
            for system_id in system_ids
        }
        for future in as_completed(futures):
            system_id = futures[future]
            system_to_gates[system_id] = future.result()
    return system_to_gates


def gather_gate_destinations(
    session: requests.Session, stargate_ids: Iterable[int], max_workers: int
) -> Dict[int, int]:
    """Fetch the destination system for every stargate id."""
    deduped_ids = list(dict.fromkeys(int(gate_id) for gate_id in stargate_ids))
    gate_to_system: Dict[int, int] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_stargate_destination, session, gate_id): gate_id
            for gate_id in deduped_ids
        }
        for future in as_completed(futures):
            gate_id = futures[future]
            gate_to_system[gate_id] = future.result()
    return gate_to_system


def build_adjacency(
    system_to_gates: Mapping[int, Iterable[int]], gate_destinations: Mapping[int, int]
) -> Dict[int, List[int]]:
    adjacency: Dict[int, Set[int]] = {}
    for system_id, gate_ids in system_to_gates.items():
        neighbors = adjacency.setdefault(system_id, set())
        for gate_id in gate_ids:
            destination_system = gate_destinations.get(gate_id)
            if destination_system is None:
                continue
            neighbors.add(destination_system)
            adjacency.setdefault(destination_system, set()).add(system_id)
    return {system_id: sorted(neighbors) for system_id, neighbors in adjacency.items()}


def write_graph(adjacency: Mapping[int, Sequence[int]], output_path: Path) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system_count": len(adjacency),
        "edge_count": sum(len(neighbors) for neighbors in adjacency.values()),
        "systems": {str(system_id): neighbors for system_id, neighbors in adjacency.items()},
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch stargate connections and persist a reusable adjacency graph "
            "(data/system_graph.json by default)."
        )
    )
    parser.add_argument(
        "--systems-file",
        type=Path,
        default=DEFAULT_SYSTEMS_PATH,
        help="Optional systems metadata file to source IDs from (default: data/systems.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_GRAPH_PATH,
        help="Destination JSON for the adjacency graph (default: data/system_graph.json).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Parallel worker pool size for the ESI fanout (default: 16).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    max_workers = max(1, args.max_workers)

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })

    system_ids = load_system_ids(args.systems_file, session)
    print(f"Discovered {len(system_ids)} system ids")

    system_to_gates = gather_system_stargates(session, system_ids, max_workers=max_workers)
    gate_ids = [gate_id for gates in system_to_gates.values() for gate_id in gates]
    print(f"Fetched {len(gate_ids)} raw stargate references")

    gate_destinations = gather_gate_destinations(session, gate_ids, max_workers=max_workers)
    print(f"Resolved {len(gate_destinations)} unique stargate destinations")

    adjacency = build_adjacency(system_to_gates, gate_destinations)
    write_graph(adjacency, args.output)
    print(
        f"Wrote adjacency for {len(adjacency)} systems to {args.output} "
        f"({sum(len(n) for n in adjacency.values())} directed edges)"
    )


if __name__ == "__main__":
    main()
