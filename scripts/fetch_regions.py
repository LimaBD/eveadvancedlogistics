"""Fetch EVE Online region metadata and save it to JSON."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
REGIONS_URL = f"{ESI_BASE}/universe/regions/?datasource={DATASOURCE}"
REGION_DETAIL_URL = (
    f"{ESI_BASE}/universe/regions/{{region_id}}/?datasource={DATASOURCE}"
)
USER_AGENT = "EveAdvancedLogistics/0.1 (regions fetcher)"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
DEFAULT_OUTPUT = Path("data/regions.json")


def fetch_with_retries(session: requests.Session, url: str) -> Dict:
    """Fetch JSON with basic retry and backoff."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:  # pragma: no cover - network heavy
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Failed to fetch {url}") from exc
            time.sleep(BACKOFF_SECONDS * attempt)  # simple incremental backoff
    raise RuntimeError(f"Failed to fetch {url}")


def fetch_region_ids(session: requests.Session) -> List[int]:
    ids = fetch_with_retries(session, REGIONS_URL)
    if not isinstance(ids, list):
        raise ValueError("Unexpected payload for regions list")
    return ids


def fetch_region_detail(session: requests.Session, region_id: int) -> Dict[str, str]:
    url = REGION_DETAIL_URL.format(region_id=region_id)
    data = fetch_with_retries(session, url)
    name = data.get("name")
    if not name:
        raise ValueError(f"Region {region_id} missing name field")
    return {"id": region_id, "name": name}


def fetch_all_regions(
    session: requests.Session, region_ids: Iterable[int], max_workers: int
) -> List[Dict[str, str]]:
    # Fan out detail fetches to keep runtime manageable without slamming ESI.
    regions: List[Dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_region_detail, session, rid): rid for rid in region_ids}
        for future in as_completed(futures):
            regions.append(future.result())
    return sorted(regions, key=lambda entry: entry["name"])


def write_regions_file(regions: List[Dict[str, str]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(regions, indent=2, sort_keys=False), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch EVE Online region names and IDs into a JSON file."
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=8,
        help="Number of parallel workers for region detail fetches.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })

    region_ids = fetch_region_ids(session)
    regions = fetch_all_regions(session, region_ids, max_workers=max(1, args.max_workers))
    output_path = DEFAULT_OUTPUT
    write_regions_file(regions, output_path)
    print(f"Wrote {len(regions)} regions to {output_path}")


if __name__ == "__main__":
    main()
