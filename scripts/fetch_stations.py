"""Fetch EVE Online NPC station metadata and persist it locally."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Tuple

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
SYSTEMS_URL = f"{ESI_BASE}/universe/systems/?datasource={DATASOURCE}"
SYSTEM_DETAIL_URL = f"{ESI_BASE}/universe/systems/{{system_id}}/?datasource={DATASOURCE}"
STATION_DETAIL_URL = f"{ESI_BASE}/universe/stations/{{station_id}}/?datasource={DATASOURCE}"
USER_AGENT = "EveAdvancedLogistics/0.1 (station fetcher)"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
DEFAULT_OUTPUT = Path("data/stations.json")


def fetch_with_retries(session: requests.Session, url: str) -> Dict:
    """Fetch JSON with retry/backoff to respect flaky ESI responses."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:  # pragma: no cover - network heavy
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Failed to fetch {url}") from exc
            time.sleep(BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"Failed to fetch {url}")


def fetch_system_ids(session: requests.Session) -> List[int]:
    payload = fetch_with_retries(session, SYSTEMS_URL)
    if not isinstance(payload, list):
        raise ValueError("Unexpected payload for systems list")
    return [int(system_id) for system_id in payload]


def fetch_system_station_ids(session: requests.Session, system_id: int) -> Tuple[int, List[int]]:
    url = SYSTEM_DETAIL_URL.format(system_id=system_id)
    payload = fetch_with_retries(session, url)
    stations = payload.get("stations", []) or []
    parsed = [int(station_id) for station_id in stations]
    return system_id, parsed


def gather_station_ids(
    session: requests.Session,
    system_ids: Sequence[int],
    *,
    max_workers: int,
) -> List[int]:
    unique_ids: Dict[int, None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_system_station_ids, session, system_id): system_id
            for system_id in system_ids
        }
        for future in as_completed(futures):
            _, station_ids = future.result()
            for station_id in station_ids:
                unique_ids.setdefault(station_id, None)
    return list(unique_ids.keys())


def fetch_station_detail(session: requests.Session, station_id: int) -> Dict:
    url = STATION_DETAIL_URL.format(station_id=station_id)
    payload = fetch_with_retries(session, url)
    name = payload.get("name")
    system_id = payload.get("system_id")
    type_id = payload.get("type_id")
    if name is None or system_id is None:
        raise ValueError(f"Station {station_id} missing required fields")
    entry = {
        "id": int(station_id),
        "name": str(name),
        "system_id": int(system_id),
    }
    if isinstance(type_id, int):
        entry["type_id"] = type_id
    owner_id = payload.get("owner")
    if isinstance(owner_id, int):
        entry["owner_id"] = owner_id
    services = payload.get("services") or []
    if services:
        entry["services"] = sorted(str(service) for service in services)
    return entry


def fanout_station_fetch(
    session: requests.Session,
    station_ids: Sequence[int],
    *,
    max_workers: int,
) -> List[Dict]:
    stations: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_station_detail, session, station_id): station_id
            for station_id in station_ids
        }
        for future in as_completed(futures):
            stations.append(future.result())
    return sorted(stations, key=lambda entry: entry["name"])


def write_stations_file(stations: List[Dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(stations, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch NPC station metadata into data/stations.json."
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Parallel workers for system and station detail fetches.",
    )
    parser.add_argument(
        "--max-systems",
        type=int,
        default=None,
        help="Limit the number of systems to scan (useful for smoke tests).",
    )
    parser.add_argument(
        "--max-stations",
        type=int,
        default=None,
        help="Stop after fetching the first N station details (testing helper).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination JSON path (default: data/stations.json).",
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

    system_ids = fetch_system_ids(session)
    if args.max_systems:
        system_ids = system_ids[: max(1, args.max_systems)]

    station_ids = gather_station_ids(session, system_ids, max_workers=max_workers)
    if not station_ids:
        print("No stations discovered from the scanned systems.")
        return

    if args.max_stations:
        station_ids = station_ids[: max(1, args.max_stations)]

    stations = fanout_station_fetch(session, station_ids, max_workers=max_workers)
    write_stations_file(stations, args.output)
    print(f"Wrote {len(stations)} stations to {args.output}")


if __name__ == "__main__":
    main()
