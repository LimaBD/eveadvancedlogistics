"""Fetch EVE Online solar system metadata and persist it locally."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
SYSTEMS_URL = f"{ESI_BASE}/universe/systems/?datasource={DATASOURCE}"
SYSTEM_DETAIL_URL = f"{ESI_BASE}/universe/systems/{{system_id}}/?datasource={DATASOURCE}"
CONSTELLATION_DETAIL_URL = (
    f"{ESI_BASE}/universe/constellations/{{constellation_id}}/?datasource={DATASOURCE}"
)
USER_AGENT = "EveAdvancedLogistics/0.1 (systems fetcher)"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
DEFAULT_OUTPUT = Path("data/systems.json")


def fetch_with_retries(session: requests.Session, url: str) -> Dict:
    """Fetch JSON with a lightweight retry/backoff loop."""
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


def fetch_system_detail(session: requests.Session, system_id: int) -> Dict:
    url = SYSTEM_DETAIL_URL.format(system_id=system_id)
    payload = fetch_with_retries(session, url)
    name = payload.get("name")
    constellation_id = payload.get("constellation_id")
    if name is None or constellation_id is None:
        raise ValueError(f"System {system_id} missing required fields")
    security_status = payload.get("security_status")
    star_id = payload.get("star_id")
    return {
        "id": int(system_id),
        "name": str(name),
        "constellation_id": int(constellation_id),
        "security_status": float(security_status) if security_status is not None else None,
        "star_id": int(star_id) if isinstance(star_id, int) else None,
    }


def fanout_system_fetch(
    session: requests.Session, system_ids: Sequence[int], max_workers: int
) -> List[Dict]:
    systems: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_system_detail, session, system_id): system_id
            for system_id in system_ids
        }
        for future in as_completed(futures):
            systems.append(future.result())
    return systems


def fetch_constellation_detail(session: requests.Session, constellation_id: int) -> Dict:
    url = CONSTELLATION_DETAIL_URL.format(constellation_id=constellation_id)
    payload = fetch_with_retries(session, url)
    region_id = payload.get("region_id")
    if region_id is None:
        raise ValueError(f"Constellation {constellation_id} missing region_id field")
    return {
        "id": int(constellation_id),
        "name": payload.get("name", f"Constellation {constellation_id}"),
        "region_id": int(region_id),
    }


def fanout_constellation_fetch(
    session: requests.Session, constellation_ids: Iterable[int], max_workers: int
) -> Dict[int, Dict]:
    constellations: Dict[int, Dict] = {}
    unique_ids = list(dict.fromkeys(int(cid) for cid in constellation_ids))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_constellation_detail, session, constellation_id): constellation_id
            for constellation_id in unique_ids
        }
        for future in as_completed(futures):
            constellation_payload = future.result()
            constellations[constellation_payload["id"]] = constellation_payload
    return constellations


def enrich_systems_with_regions(
    systems: Iterable[Dict], constellations: Dict[int, Dict]
) -> List[Dict]:
    enriched: List[Dict] = []
    for system in systems:
        constellation_data = constellations.get(system["constellation_id"], {})
        entry = {
            "id": system["id"],
            "name": system["name"],
            "constellation_id": system["constellation_id"],
            "constellation_name": constellation_data.get("name"),
            "region_id": constellation_data.get("region_id"),
            "security_status": system["security_status"],
        }
        if system.get("star_id") is not None:
            entry["star_id"] = system["star_id"]
        enriched.append(entry)
    return sorted(enriched, key=lambda entry: entry["name"])


def write_systems_file(systems: List[Dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(systems, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch EVE Online solar system metadata into data/systems.json."
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Parallel workers for system/constellation detail fetches.",
    )
    parser.add_argument(
        "--max-systems",
        type=int,
        default=None,
        help="Fetch only the first N systems (handy for local testing).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination JSON path (default: data/systems.json).",
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

    systems = fanout_system_fetch(session, system_ids, max_workers=max_workers)
    constellation_ids = {system["constellation_id"] for system in systems}
    constellations = fanout_constellation_fetch(session, constellation_ids, max_workers=max_workers)

    enriched_systems = enrich_systems_with_regions(systems, constellations)
    write_systems_file(enriched_systems, args.output)
    print(f"Wrote {len(enriched_systems)} systems to {args.output}")


if __name__ == "__main__":
    main()
