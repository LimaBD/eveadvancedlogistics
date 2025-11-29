"""Fetch all published EVE Online ship types and save them to JSON."""
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
SHIP_CATEGORY_ID = 6  # https://esi.evetech.net/ui/#/Universe/get_universe_categories_category_id
CATEGORY_URL = f"{ESI_BASE}/universe/categories/{SHIP_CATEGORY_ID}/?datasource={DATASOURCE}"
GROUP_URL = f"{ESI_BASE}/universe/groups/{{group_id}}/?datasource={DATASOURCE}"
TYPE_URL = f"{ESI_BASE}/universe/types/{{type_id}}/?datasource={DATASOURCE}"
USER_AGENT = "EveAdvancedLogistics/0.1 (ships fetcher)"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
DEFAULT_OUTPUT = Path("data/ships.json")


def fetch_with_retries(session: requests.Session, url: str) -> Dict:
    """Fetch JSON with a minimal retry/backoff strategy."""
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


def fetch_ship_group_ids(session: requests.Session) -> List[int]:
    payload = fetch_with_retries(session, CATEGORY_URL)
    group_ids = payload.get("groups") or payload.get("group_ids")
    if not isinstance(group_ids, list):
        raise ValueError("Ship category payload missing group_ids list")
    return [int(group_id) for group_id in group_ids]


def fetch_group_detail(session: requests.Session, group_id: int) -> Dict:
    url = GROUP_URL.format(group_id=group_id)
    payload = fetch_with_retries(session, url)
    name = payload.get("name")
    type_ids = payload.get("types")
    if not name:
        raise ValueError(f"Group {group_id} missing name field")
    if not isinstance(type_ids, list):
        raise ValueError(f"Group {group_id} missing types list")
    if not payload.get("published", False):  # keep the dataset to user-facing ships
        return {"group_id": group_id, "group_name": name, "type_ids": []}
    return {
        "group_id": group_id,
        "group_name": name,
        "type_ids": [int(type_id) for type_id in type_ids],
    }


def fanout_group_fetch(
    session: requests.Session, group_ids: Sequence[int], max_workers: int
) -> List[Dict]:
    groups: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_group_detail, session, group_id): group_id
            for group_id in group_ids
        }
        for future in as_completed(futures):
            group_payload = future.result()
            if group_payload["type_ids"]:
                groups.append(group_payload)
    return sorted(groups, key=lambda entry: entry["group_name"])


def unique_type_ids(groups: Iterable[Dict]) -> List[int]:
    seen = {}
    for group in groups:
        for type_id in group["type_ids"]:
            seen[type_id] = None
    return list(seen.keys())


def fetch_type_detail(session: requests.Session, type_id: int) -> Dict:
    url = TYPE_URL.format(type_id=type_id)
    payload = fetch_with_retries(session, url)
    name = payload.get("name")
    group_id = payload.get("group_id")
    if not name:
        raise ValueError(f"Type {type_id} missing name field")
    if group_id is None:
        raise ValueError(f"Type {type_id} missing group_id field")
    return {
        "id": type_id,
        "name": name,
        "group_id": int(group_id),
        "published": bool(payload.get("published", True)),
    }


def fanout_type_fetch(
    session: requests.Session, type_ids: Sequence[int], max_workers: int
) -> List[Dict]:
    ships: List[Dict] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(fetch_type_detail, session, type_id): type_id
            for type_id in type_ids
        }
        for future in as_completed(futures):
            ships.append(future.result())
    return ships


def write_ships_file(ships: List[Dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(ships, indent=2, sort_keys=False), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch the published ship type list (id, name, group) from ESI."
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=12,
        help="Parallel workers for network fetches (groups + types).",
    )
    parser.add_argument(
        "--max-types",
        type=int,
        default=None,
        help="Fetch only the first N type IDs (useful while testing).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination JSON path (default: data/ships.json).",
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

    group_ids = fetch_ship_group_ids(session)
    groups = fanout_group_fetch(session, group_ids, max_workers=max_workers)
    if not groups:
        raise RuntimeError("No published ship groups discovered")

    type_ids = unique_type_ids(groups)
    if args.max_types:
        type_ids = type_ids[: max(1, args.max_types)]

    ships = fanout_type_fetch(session, type_ids, max_workers=max_workers)
    group_name_lookup = {group["group_id"]: group["group_name"] for group in groups}
    for ship in ships:
        ship["group_name"] = group_name_lookup.get(ship["group_id"], "Unknown")
    ships = sorted(ships, key=lambda entry: entry["name"])

    write_ships_file(ships, args.output)
    print(f"Wrote {len(ships)} ships to {args.output}")


if __name__ == "__main__":
    main()
