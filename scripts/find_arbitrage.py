"""Scan EVE Online regional markets for ship arbitrage opportunities."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
ORDERS_URL = f"{ESI_BASE}/markets/{{region_id}}/orders/"
SYSTEM_URL = f"{ESI_BASE}/universe/systems/{{system_id}}/"
USER_AGENT = "EveAdvancedLogistics/0.1 (arbitrage scanner)"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_SECONDS = 1.5
DEFAULT_SHIPS = Path("data/ships.json")
DEFAULT_REGIONS = Path("data/regions.json")
DEFAULT_FINAL_OUTPUT = Path("data/arbitrage_summary.txt")


@dataclass(frozen=True)
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
class Opportunity:
    type_id: int
    ship_name: str
    buy_from: Order  # seller's order we will consume
    sell_to: Order  # buyer's order we will fill
    unit_profit: float
    tradable_units: int
    total_profit: float
    margin: float


class HttpClient:
    """Thin wrapper with retry/backoff for ESI requests."""

    def __init__(self, session: requests.Session, request_delay: float) -> None:
        self.session = session
        self.request_delay = max(0.0, request_delay)

    def get_json(self, url: str, *, params: Dict[str, str] | None = None) -> Tuple[object, Dict[str, str]]:
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

class SystemNameResolver:
    """Resolve system IDs to names with local caching."""

    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client
        self.cache: Dict[int, str] = {}

    def resolve(self, system_id: int) -> str:
        if system_id <= 0:
            return f"System {system_id}"
        if system_id in self.cache:
            return self.cache[system_id]
        url = SYSTEM_URL.format(system_id=system_id)
        params = {"datasource": DATASOURCE}
        try:
            payload, _ = self.http_client.get_json(url, params=params)
        except RuntimeError:
            name = f"System {system_id}"
        else:
            name = payload.get("name") or f"System {system_id}"
        self.cache[system_id] = name
        return name


class ArbitrageTracker:
    def __init__(self, ship_lookup: Dict[int, str]):
        self.ship_lookup = ship_lookup
        self.best_buy_orders: Dict[int, Order] = {}
        self.best_sell_orders: Dict[int, Order] = {}

    def ingest(self, order_payload: Dict, region: Region) -> None:
        type_id = int(order_payload.get("type_id", 0))
        if type_id not in self.ship_lookup:
            return
        order = Order(
            order_id=int(order_payload.get("order_id") or 0),
            type_id=type_id,
            price=float(order_payload.get("price", 0.0)),
            volume_remain=int(order_payload.get("volume_remain", 0)),
            min_volume=int(order_payload.get("min_volume", 1)),
            is_buy_order=bool(order_payload.get("is_buy_order", False)),
            system_id=int(order_payload.get("system_id") or 0),
            location_id=int(order_payload.get("location_id") or 0),
            region_id=region.region_id,
            region_name=region.name,
        )
        if order.is_buy_order:
            best = self.best_buy_orders.get(type_id)
            if best is None or order.price > best.price:
                self.best_buy_orders[type_id] = order
        else:
            best = self.best_sell_orders.get(type_id)
            if best is None or order.price < best.price:
                self.best_sell_orders[type_id] = order

    def opportunities(
        self,
        *,
        min_margin: float,
        min_profit: float,
        max_investment: Optional[float],
    ) -> List[Opportunity]:
        opps: List[Opportunity] = []
        shared_type_ids = self.best_buy_orders.keys() & self.best_sell_orders.keys()
        for type_id in shared_type_ids:
            seller = self.best_sell_orders[type_id]
            buyer = self.best_buy_orders[type_id]
            if buyer.price <= seller.price:
                continue
            unit_profit = buyer.price - seller.price
            if seller.min_volume > 1 or buyer.min_volume > 1:
                continue
            if seller.volume_remain <= 0 or buyer.volume_remain <= 0:
                continue
            if max_investment is not None and seller.price > max_investment:
                continue
            tradable_units = 1
            total_profit = unit_profit * tradable_units
            margin_ratio = buyer.price / seller.price if seller.price else 0.0
            if margin_ratio - 1 < min_margin:
                continue
            if total_profit < min_profit:
                continue
            opps.append(
                Opportunity(
                    type_id=type_id,
                    ship_name=self.ship_lookup[type_id],
                    buy_from=seller,
                    sell_to=buyer,
                    unit_profit=unit_profit,
                    tradable_units=tradable_units,
                    total_profit=total_profit,
                    margin=margin_ratio,
                )
            )
        return sorted(opps, key=lambda entry: entry.margin, reverse=True)


def load_json_list(path: Path) -> List[Dict]:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a list in {path}")
    return payload


def build_ship_lookup(ships_payload: Iterable[Dict]) -> Dict[int, str]:
    lookup: Dict[int, str] = {}
    for entry in ships_payload:
        if not entry.get("published", True):
            continue
        lookup[int(entry["id"])] = entry["name"]
    return lookup


def build_regions(regions_payload: Iterable[Dict]) -> List[Region]:
    regions: List[Region] = []
    for entry in regions_payload:
        try:
            region_id = int(entry["id"])
            name = str(entry["name"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid region entry: {entry}") from exc
        regions.append(Region(region_id=region_id, name=name))
    return regions


def fetch_orders_for_region(
    http_client: HttpClient, region: Region, page: int
) -> Tuple[List[Dict], int]:
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


def format_location(order: Order, resolver: SystemNameResolver) -> str:
    system_name = resolver.resolve(order.system_id)
    return f"{order.region_name} / {system_name} ({order.location_id})"


def render_opportunity_report(
    opportunities: List[Opportunity],
    resolver: SystemNameResolver,
    *,
    top_n: int,
    page: int,
    pages_processed: int,
) -> str:
    lines: List[str] = ["", f"=== After completing page {page} across {pages_processed} regions ==="]
    if not opportunities:
        lines.append("No qualifying arbitrage opportunities yet.\n")
        return "\n".join(lines)
    header_line = (
        f"{'Ship':<30} | {'Buy From (price)':<60} | "
        f"{'Sell To (price)':<60} | {'Units':>7} | {'Total Profit':>15} | {'Margin':>8}"
    )
    lines.append(header_line)
    lines.append("-" * len(header_line))
    for opportunity in opportunities[:top_n]:
        buy_loc = format_location(opportunity.buy_from, resolver)
        sell_loc = format_location(opportunity.sell_to, resolver)
        buy_price = f"{opportunity.buy_from.price:,.2f}"
        sell_price = f"{opportunity.sell_to.price:,.2f}"
        row = (
            f"{opportunity.ship_name:<30}"
            f" | {buy_loc} @ {buy_price:<15}"
            f" | {sell_loc} @ {sell_price:<15}"
            f" | {opportunity.tradable_units:>7}"
            f" | {opportunity.total_profit:>15,.2f}"
            f" | {opportunity.margin:>7.2f}x"
        )
        lines.append(row)
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enumerate ship arbitrage opportunities by walking the entire regional market.",
    )
    parser.add_argument("--ships", type=Path, default=DEFAULT_SHIPS, help="Path to data/ships.json.")
    parser.add_argument("--regions", type=Path, default=DEFAULT_REGIONS, help="Path to data/regions.json.")
    parser.add_argument(
        "--min-margin",
        type=float,
        default=0.02,
        help="Minimum margin (as decimal, e.g. 0.05 for 5%%).",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=10_000_000.0,
        help="Minimum total ISK profit to report (default: 10M).",
    )
    parser.add_argument(
        "--max-investment",
        type=float,
        default=85_000_000.0,
        help="Maximum ISK to spend on a single hull (default: 85M).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=15,
        help="Display only the top N opportunities each refresh (default: 15).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit scanning to this many pages per region (useful while testing).",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.1,
        help="Seconds to sleep after each ESI call to avoid throttling (default: 0.25).",
    )
    parser.add_argument(
        "--ship-limit",
        type=int,
        default=None,
        help="Consider only the first N ships from the dataset (testing helper).",
    )
    parser.add_argument(
        "--final-output",
        type=Path,
        default=DEFAULT_FINAL_OUTPUT,
        help="Write the final snapshot to this path (default: arbitrage_summary.txt).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        ships_payload = load_json_list(args.ships)
        regions_payload = load_json_list(args.regions)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Failed to load prerequisite data: {exc}", file=sys.stderr)
        sys.exit(1)

    ship_lookup = build_ship_lookup(ships_payload)
    if args.ship_limit:
        limited_ids = set(list(ship_lookup.keys())[: max(1, args.ship_limit)])
        ship_lookup = {type_id: ship_lookup[type_id] for type_id in limited_ids}
    regions = build_regions(regions_payload)
    if not regions:
        print("No regions to scan", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    http_client = HttpClient(session, request_delay=args.request_delay)
    resolver = SystemNameResolver(http_client)
    tracker = ArbitrageTracker(ship_lookup)

    region_max_pages: Dict[int, int] = {}
    page = 1
    total_regions = len(regions)

    while True:
        pages_fetched = 0
        for region in regions:
            max_pages_for_region = region_max_pages.get(region.region_id)
            if args.max_pages and page > args.max_pages:
                continue
            if max_pages_for_region is not None and page > max_pages_for_region:
                continue
            try:
                orders, max_pages = fetch_orders_for_region(http_client, region, page)
            except RuntimeError as exc:
                print(f"Warning: failed to fetch region {region.name} page {page}: {exc}", file=sys.stderr)
                continue
            region_max_pages[region.region_id] = max(max_pages, region_max_pages.get(region.region_id, 0))
            for order_payload in orders:
                tracker.ingest(order_payload, region)
            pages_fetched += 1
        if pages_fetched == 0:
            break
        opportunities = tracker.opportunities(
            min_margin=args.min_margin,
            min_profit=args.min_profit,
            max_investment=args.max_investment,
        )
        report = render_opportunity_report(
            opportunities,
            resolver,
            top_n=args.top,
            page=page,
            pages_processed=pages_fetched,
        )
        print(report)
        page += 1
        if page == 2:
            print("Completed first sweep across all regions. Moving to page 2.")

    print("Scanning finished. Final opportunity snapshot:")
    opportunities = tracker.opportunities(
        min_margin=args.min_margin,
        min_profit=args.min_profit,
        max_investment=args.max_investment,
    )
    final_report = render_opportunity_report(
        opportunities,
        resolver,
        top_n=args.top,
        page=page - 1,
        pages_processed=total_regions,
    )
    print(final_report)
    try:
        args.final_output.parent.mkdir(parents=True, exist_ok=True)
        args.final_output.write_text(final_report.rstrip() + "\n", encoding="utf-8")
        print(f"Final snapshot saved to {args.final_output}")
    except OSError as exc:
        print(f"Warning: failed to write final snapshot to {args.final_output}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
