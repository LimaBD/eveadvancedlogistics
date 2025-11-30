"""Download EVE market orders once so processing can run offline."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

import requests


ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
ORDERS_URL = f"{ESI_BASE}/markets/{{region_id}}/orders/"
USER_AGENT = "EveAdvancedLogistics/0.3 (order fetcher)"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_SECONDS = 1.5
DEFAULT_REGIONS = Path("data/regions.json")
DEFAULT_ORDERS_SNAPSHOT = Path("data/market_orders_snapshot.json")
DEFAULT_REQUEST_DELAY = 0.1
THE_FORGE_REGION_ID = 10000002
FORGE_ADJACENT_REGION_IDS = {
    THE_FORGE_REGION_ID,
    10000016,  # Lonetrek
    10000033,  # The Citadel
    10000029,  # Geminate
    10000003,  # Vale of the Silent
}
PLAYER_STRUCTURE_MIN_LOCATION_ID = 1_000_000_000_000


@dataclass
class Region:
    region_id: int
    name: str


@dataclass
class Order:
    order_id: int
    type_id: int
    price: float
    volume_remain: int
    min_volume: int
    is_buy_order: bool
    system_id: int
    location_id: int
    region_id: int
    region_name: str


@dataclass
class FetchProfile:
    profile_name: Optional[str]
    regions: Path
    request_delay: float
    max_pages: Optional[int]
    forge_adjacent_only: bool


def is_player_structure(location_id: int) -> bool:
    """Player-built structures have large (>1e12) location IDs in ESI."""
    return location_id >= PLAYER_STRUCTURE_MIN_LOCATION_ID


def _expand_path(value: object | None, *, base_dir: Path, default: Path) -> Path:
    if value is None:
        return default
    candidate = value if isinstance(value, Path) else Path(str(value))
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    return candidate


def load_json_list(path: Path) -> List[Dict]:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a list in {path}")
    return payload


def build_regions(regions_payload: Iterable[Mapping]) -> List[Region]:
    regions: List[Region] = []
    for entry in regions_payload:
        try:
            region_id = int(entry["id"])
            name = str(entry["name"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid region entry: {entry}") from exc
        regions.append(Region(region_id=region_id, name=name))
    return regions


def load_profile_config(profile_path: Path) -> FetchProfile:
    profile_path = profile_path.expanduser().resolve()
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile file {profile_path} does not exist")
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Profile {profile_path} must contain a JSON object")
    base_dir = profile_path.parent
    name = str(payload.get("name") or profile_path.stem)
    regions = _expand_path(payload.get("regions"), base_dir=base_dir, default=DEFAULT_REGIONS)
    request_delay = float(payload.get("request_delay", DEFAULT_REQUEST_DELAY))
    max_pages = payload.get("max_pages")
    max_pages_int = None if max_pages is None else int(max_pages)
    forge_adjacent_only = bool(payload.get("forge_adjacent_only", False))
    return FetchProfile(
        profile_name=name,
        regions=regions,
        request_delay=request_delay,
        max_pages=max_pages_int,
        forge_adjacent_only=forge_adjacent_only,
    )


class HttpClient:
    """Thin wrapper with retry/backoff for ESI requests."""

    def __init__(self, session: requests.Session, request_delay: float) -> None:
        self.session = session
        self.request_delay = max(0.0, request_delay)

    def get_json(self, url: str, *, params: Dict[str, str] | None = None) -> Tuple[object, Mapping[str, str]]:
        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                if self.request_delay:
                    time.sleep(self.request_delay)
                if response.status_code == 404:
                    return {}, response.headers
                if response.status_code == 420:
                    reset = int(response.headers.get("x-esi-error-limit-reset", "1"))
                    time.sleep(reset + 1)
                    continue
                if response.status_code >= 500:
                    raise requests.HTTPError(f"ESI server error {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                return payload, response.headers
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                if attempt == MAX_RETRIES:
                    break
                time.sleep(BACKOFF_SECONDS * attempt)
        raise RuntimeError(f"Failed to GET {url}") from last_exc


def fetch_orders_for_region(http_client: HttpClient, region: Region, page: int) -> Tuple[List[Dict], int]:
    params = {
        "datasource": DATASOURCE,
        "order_type": "all",
        "page": str(page),
    }
    url = ORDERS_URL.format(region_id=region.region_id)
    payload, headers = http_client.get_json(url, params=params)
    if not payload:
        return [], int(headers.get("X-Pages", "0"))
    if not isinstance(payload, list):
        raise ValueError(f"Unexpected payload for region {region.region_id}: {payload}")
    max_pages = int(headers.get("X-Pages", "1"))
    return payload, max_pages


def serialize_order(entry: Mapping, region: Region) -> Order:
    return Order(
        order_id=int(entry.get("order_id") or 0),
        type_id=int(entry.get("type_id") or 0),
        price=float(entry.get("price") or 0.0),
        volume_remain=int(entry.get("volume_remain") or 0),
        min_volume=int(entry.get("min_volume") or 1),
        is_buy_order=bool(entry.get("is_buy_order", False)),
        system_id=int(entry.get("system_id") or 0),
        location_id=int(entry.get("location_id") or 0),
        region_id=region.region_id,
        region_name=region.name,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch market orders and store them locally.")
    parser.add_argument(
        "--profile",
        type=Path,
        help="Optional profile to inherit region/request settings from.",
    )
    parser.add_argument(
        "--regions",
        type=Path,
        help="Path to regions.json (defaults to profile value or data/regions.json).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_ORDERS_SNAPSHOT,
        help="Destination JSON file for the captured snapshot (default: data/market_orders_snapshot.json).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Optional override for max pages per region for this fetch run.",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        help="Seconds to sleep between ESI calls (defaults to profile or 0.1).",
    )
    parser.add_argument(
        "--forge-adjacent-only",
        dest="forge_adjacent_only",
        action="store_true",
        help="Restrict to The Forge and neighboring regions.",
    )
    parser.add_argument(
        "--all-regions",
        dest="forge_adjacent_only",
        action="store_false",
        help="Explicitly disable any forge adjacency filter.",
    )
    parser.set_defaults(forge_adjacent_only=None)
    return parser.parse_args()


def main() -> None:
    cli_args = parse_args()
    profile = None
    if cli_args.profile is not None:
        try:
            profile = load_profile_config(cli_args.profile)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Failed to load profile {cli_args.profile}: {exc}", file=sys.stderr)
            sys.exit(1)

    regions_path = cli_args.regions or (profile.regions if profile else DEFAULT_REGIONS)
    try:
        regions_payload = load_json_list(regions_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Failed to load regions data from {regions_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    regions = build_regions(regions_payload)
    forge_only = (
        cli_args.forge_adjacent_only
        if cli_args.forge_adjacent_only is not None
        else (profile.forge_adjacent_only if profile else False)
    )
    if forge_only:
        before = len(regions)
        regions = [region for region in regions if region.region_id in FORGE_ADJACENT_REGION_IDS]
        if not regions:
            print("No regions remain after applying forge-adjacent-only filter", file=sys.stderr)
            sys.exit(1)
        print(f"Forge adjacency filter active: scanning {len(regions)} of {before} regions.")
    if not regions:
        print("No regions to scan", file=sys.stderr)
        sys.exit(1)

    request_delay = (
        cli_args.request_delay
        if cli_args.request_delay is not None
        else (profile.request_delay if profile else DEFAULT_REQUEST_DELAY)
    )

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    http_client = HttpClient(session, request_delay=request_delay)

    snapshot_orders: List[Dict[str, object]] = []
    stats: Dict[int, Dict[str, int]] = {}
    skipped_player_orders_total = 0
    max_pages_override = (
        cli_args.max_pages
        if cli_args.max_pages is not None
        else (profile.max_pages if profile else None)
    )

    for region in regions:
        print(f"Fetching region {region.name} ({region.region_id})")
        page = 1
        max_pages_seen = 1
        per_region_orders = 0
        per_region_player_skips = 0
        while True:
            if max_pages_override is not None and page > max_pages_override:
                break
            try:
                payload, max_pages = fetch_orders_for_region(http_client, region, page)
            except RuntimeError as exc:
                print(f"Warning: failed to fetch region {region.name} page {page}: {exc}", file=sys.stderr)
                break
            max_pages_seen = max(max_pages_seen, max_pages)
            if not payload:
                break
            for entry in payload:
                order = serialize_order(entry, region)
                if order.volume_remain <= 0:
                    continue
                if is_player_structure(order.location_id):
                    per_region_player_skips += 1
                    continue
                snapshot_orders.append(asdict(order))
                per_region_orders += 1
            if page >= max_pages:
                break
            page += 1
        stats[region.region_id] = {
            "pages": max_pages_seen,
            "orders": per_region_orders,
            "player_structures": per_region_player_skips,
        }
        skipped_player_orders_total += per_region_player_skips

    if not snapshot_orders:
        print("No orders captured; nothing to write", file=sys.stderr)
        sys.exit(1)

    payload = {
        "profile": profile.profile_name if profile else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source_profile": str(cli_args.profile) if cli_args.profile else None,
        "request_delay": request_delay,
        "regions": [
            {
                "region_id": region.region_id,
                "name": region.name,
                "pages": stats.get(region.region_id, {}).get("pages", 0),
                "orders": stats.get(region.region_id, {}).get("orders", 0),
                "player_structures_skipped": stats.get(region.region_id, {}).get("player_structures", 0),
            }
            for region in regions
        ],
        "orders": snapshot_orders,
    }

    output_path = cli_args.output.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    print(
        f"Captured {len(snapshot_orders):,} orders across {len(regions)} regions into {output_path}"
    )
    if skipped_player_orders_total:
        print(
            f"Skipped {skipped_player_orders_total:,} player-structure orders (non-dockable locations removed)."
        )


if __name__ == "__main__":
    main()
