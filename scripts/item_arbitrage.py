"""Scan EVE Online regional markets for generalized arbitrage opportunities."""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

from calc_jumps import (
    DEFAULT_SYSTEMS_PATH,
    VALID_FLAGS,
    JumpCache,
    load_system_indexes,
    resolve_system_identifier,
)

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
ORDERS_URL = f"{ESI_BASE}/markets/{{region_id}}/orders/"
USER_AGENT = "EveAdvancedLogistics/0.2 (item arbitrage scanner)"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4
BACKOFF_SECONDS = 1.5
DEFAULT_ITEMS = Path("data/items.json")
DEFAULT_REGIONS = Path("data/regions.json")
DEFAULT_SYSTEMS = DEFAULT_SYSTEMS_PATH
DEFAULT_FINAL_OUTPUT = Path("data/arbitrage_items_summary.txt")
THE_FORGE_REGION_ID = 10000002
FORGE_ADJACENT_REGION_IDS = {
    THE_FORGE_REGION_ID,
    10000016,  # Lonetrek
    10000033,  # The Citadel
    10000029,  # Geminate
    10000003,  # Vale of the Silent
}


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


@dataclass(frozen=True)
class ItemMetadata:
    type_id: int
    name: str
    unit_volume: float


@dataclass
class ItemOpportunity:
    type_id: int
    item_name: str
    buy_from: Order
    sell_to: Order
    unit_profit: float
    tradable_units: int
    total_profit: float
    multiplier: float
    total_investment: float
    total_volume: float


@dataclass
class RouteOpportunity:
    buy_region: str
    sell_region: str
    buy_system_id: int
    sell_system_id: int
    buy_system_name: str
    sell_system_name: str
    items: List[ItemOpportunity] = field(default_factory=list)
    total_profit: float = 0.0
    total_investment: float = 0.0
    total_volume: float = 0.0
    total_units: int = 0
    multiplier_hint: float = 0.0
    jumps_from_current: Optional[int] = None
    jumps_trade: Optional[int] = None

    @property
    def total_jumps(self) -> Optional[int]:
        if self.jumps_from_current is None or self.jumps_trade is None:
            return None
        return self.jumps_from_current + self.jumps_trade

    @property
    def isk_per_jump(self) -> Optional[float]:
        jumps = self.total_jumps
        if jumps is None:
            return None
        # Treat stationary trades as one jump so profit still yields a usable ratio.
        effective_jumps = jumps if jumps > 0 else 1
        return self.total_profit / effective_jumps


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


class SystemResolver:
    def __init__(self, lookup: Mapping[int, str]):
        self.lookup = lookup

    def resolve(self, system_id: int) -> str:
        return self.lookup.get(system_id, f"System {system_id}")



class ArbitrageTracker:
    def __init__(self, item_lookup: Mapping[int, ItemMetadata]):
        self.item_lookup = item_lookup
        self.best_buy_orders: Dict[int, Order] = {}
        self.best_sell_orders: Dict[int, Order] = {}

    def ingest(self, order_payload: Mapping, region: Region) -> None:
        type_id = int(order_payload.get("type_id", 0))
        if type_id not in self.item_lookup:
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
        if order.volume_remain <= 0:
            return
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
        min_multiplier: float,
        max_investment: Optional[float],
        max_volume: Optional[float],
        max_item_volume: Optional[float],
    ) -> List[ItemOpportunity]:
        opps: List[ItemOpportunity] = []
        shared_type_ids = self.best_buy_orders.keys() & self.best_sell_orders.keys()
        for type_id in shared_type_ids:
            item_meta = self.item_lookup[type_id]
            seller = self.best_sell_orders[type_id]
            buyer = self.best_buy_orders[type_id]
            if buyer.price <= seller.price:
                continue
            if seller.min_volume > 1 or buyer.min_volume > 1:
                continue
            if seller.volume_remain <= 0 or buyer.volume_remain <= 0:
                continue
            if seller.price <= 0:
                continue
            unit_volume = max(item_meta.unit_volume, 0.0)
            if max_item_volume is not None and unit_volume > max_item_volume:
                continue
            max_units = min(seller.volume_remain, buyer.volume_remain)
            if max_units <= 0:
                continue
            if max_investment is not None:
                if seller.price > max_investment:
                    continue
                invest_cap = int(max_investment // seller.price)
                max_units = min(max_units, invest_cap)
            if max_volume is not None and unit_volume > 0:
                volume_cap = int(max_volume // unit_volume)
                max_units = min(max_units, volume_cap)
            if max_units <= 0:
                continue
            unit_profit = buyer.price - seller.price
            total_profit = unit_profit * max_units
            multiplier = buyer.price / seller.price if seller.price else 0.0
            if multiplier < min_multiplier:
                continue
            total_investment = seller.price * max_units
            total_volume = unit_volume * max_units
            if max_volume is not None and total_volume > max_volume + 1e-6:
                continue
            opps.append(
                ItemOpportunity(
                    type_id=type_id,
                    item_name=item_meta.name,
                    buy_from=seller,
                    sell_to=buyer,
                    unit_profit=unit_profit,
                    tradable_units=max_units,
                    total_profit=total_profit,
                    multiplier=multiplier,
                    total_investment=total_investment,
                    total_volume=total_volume,
                )
            )
        return sorted(
            opps,
            key=lambda entry: (entry.total_profit, entry.multiplier),
            reverse=True,
        )


def load_json_list(path: Path) -> List[Dict]:
    if not path.exists():
        raise FileNotFoundError(f"Missing required file: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Expected a list in {path}")
    return payload


def build_item_lookup(items_payload: Iterable[Mapping], *, limit: Optional[int]) -> Dict[int, ItemMetadata]:
    lookup: Dict[int, ItemMetadata] = {}
    remaining = limit if limit is None else max(1, limit)
    for entry in items_payload:
        if not entry.get("published", True):
            continue
        try:
            type_id = int(entry["id"])
            name = str(entry["name"]).strip()
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid item entry: {entry}") from exc
        if not name:
            continue
        packaged_vol = float(entry.get("packaged_volume") or 0.0)
        regular_vol = float(entry.get("volume") or 0.0)
        unit_volume = packaged_vol if packaged_vol > 0 else regular_vol
        lookup[type_id] = ItemMetadata(type_id=type_id, name=name, unit_volume=unit_volume)
        if remaining is not None:
            remaining -= 1
            if remaining == 0:
                break
    return lookup


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


def group_opportunities(
    opportunities: Sequence[ItemOpportunity],
    *,
    resolver: SystemResolver,
    jump_cache: JumpCache,
    current_system_id: int,
) -> List[RouteOpportunity]:
    grouped: Dict[Tuple[int, int], RouteOpportunity] = {}
    for opportunity in opportunities:
        buy_sys = opportunity.buy_from.system_id
        sell_sys = opportunity.sell_to.system_id
        key = (buy_sys, sell_sys)
        group = grouped.get(key)
        if group is None:
            group = RouteOpportunity(
                buy_region=opportunity.buy_from.region_name,
                sell_region=opportunity.sell_to.region_name,
                buy_system_id=buy_sys,
                sell_system_id=sell_sys,
                buy_system_name=resolver.resolve(buy_sys),
                sell_system_name=resolver.resolve(sell_sys),
            )
            grouped[key] = group
            jumps_to_buy = jump_cache.jumps(current_system_id, buy_sys)
            jumps_trade = jump_cache.jumps(buy_sys, sell_sys)
            group.jumps_from_current = jumps_to_buy
            group.jumps_trade = jumps_trade
        group.items.append(opportunity)
        group.total_profit += opportunity.total_profit
        group.total_investment += opportunity.total_investment
        group.total_volume += opportunity.total_volume
        group.total_units += opportunity.tradable_units
        group.multiplier_hint = max(group.multiplier_hint, opportunity.multiplier)
    ordered = sorted(
        grouped.values(),
        key=lambda entry: (entry.isk_per_jump or -math.inf, entry.total_profit),
        reverse=True,
    )
    return ordered


def filter_routes_by_profit_per_jump(
    routes: Sequence[RouteOpportunity],
    *,
    min_profit_per_jump: Optional[float],
    max_total_jumps: Optional[int],
) -> List[RouteOpportunity]:
    no_profit_filter = min_profit_per_jump is None or min_profit_per_jump <= 0
    no_jump_filter = max_total_jumps is None or max_total_jumps < 0
    if no_profit_filter and no_jump_filter:
        return list(routes)
    filtered: List[RouteOpportunity] = []
    for route in routes:
        if not no_jump_filter:
            assert max_total_jumps is not None
            total_jumps = route.total_jumps
            if total_jumps is None or total_jumps > max_total_jumps:
                continue
        if no_profit_filter:
            filtered.append(route)
            continue
        isk_per_jump = route.isk_per_jump
        if isk_per_jump is None:
            continue
        assert min_profit_per_jump is not None
        if isk_per_jump >= min_profit_per_jump:
            filtered.append(route)
    return filtered


def describe_active_route_filters(
    *,
    min_profit_per_jump: Optional[float],
    max_total_jumps: Optional[int],
) -> List[str]:
    reasons: List[str] = []
    if min_profit_per_jump is not None and min_profit_per_jump > 0:
        reasons.append(f"--min-profit-per-jump={min_profit_per_jump:,.0f}")
    if max_total_jumps is not None and max_total_jumps >= 0:
        reasons.append(f"--max-total-jumps={max_total_jumps}")
    return reasons


def render_route_report(
    routes: Sequence[RouteOpportunity],
    *,
    top_n: int,
    current_system_name: str,
) -> str:
    lines: List[str] = []
    if not routes:
        return "No qualifying arbitrage opportunities yet."
    header = (
        f"{'Buy -> Sell':<55} | {'Items':>5} | {'Units':>7} | {'Profit (ISK)':>18} | "
        f"{'Investment':>18} | {'Volume (m3)':>14} | {'Jumps':>10} | {'ISK/jump':>12}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for route in routes[:top_n]:
        jumps_total = route.total_jumps
        jumps_display = "n/a" if jumps_total is None else str(jumps_total)
        isk_per_jump = route.isk_per_jump
        isk_per_jump_display = "n/a" if isk_per_jump is None else f"{isk_per_jump:,.2f}"
        profit_display = f"{route.total_profit:,.2f}"
        investment_display = f"{route.total_investment:,.2f}"
        volume_display = f"{route.total_volume:,.2f}"
        buy_sell = f"{route.buy_region} / {route.buy_system_name} -> {route.sell_region} / {route.sell_system_name}"
        lines.append(
            f"{buy_sell:<55} | {len(route.items):>5} | {route.total_units:>7} | {profit_display:>18} | "
            f"{investment_display:>18} | {volume_display:>14} | {jumps_display:>10} | {isk_per_jump_display:>12}"
        )
        for item in route.items:
            lines.append(
                "    "
                + f"{item.item_name} x{item.tradable_units} @ {item.buy_from.price:,.2f} -> {item.sell_to.price:,.2f}"
                + f" | profit {item.total_profit:,.2f} | mult {item.multiplier:.2f}x"
            )
        if route.jumps_from_current is not None and route.jumps_trade is not None:
            lines.append(
                "    "
                + f"Route: {current_system_name} -> {route.buy_system_name} ({route.jumps_from_current} jumps), "
                + f"then {route.buy_system_name} -> {route.sell_system_name} ({route.jumps_trade} jumps)"
            )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enumerate arbitrage opportunities across all published items, grouped by travel route.",
    )
    parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS, help="Path to data/items.json.")
    parser.add_argument("--regions", type=Path, default=DEFAULT_REGIONS, help="Path to data/regions.json.")
    parser.add_argument("--systems", type=Path, default=DEFAULT_SYSTEMS, help="Path to data/systems.json.")
    parser.add_argument(
        "--current-system",
        required=True,
        help="Name or ID of the system you are currently in (used to compute total jumps).",
    )
    parser.add_argument(
        "--min-multiplier",
        type=float,
        default=2,
        help="Minimum price multiplier (buyer/seller) to accept (default: 2).",
    )
    parser.add_argument(
        "--min-profit-per-jump",
        type=float,
        default=None,
        help="Minimum ISK earned per jump required for a route (disabled by default).",
    )
    parser.add_argument(
        "--max-total-jumps",
        type=int,
        default=None,
        help=(
            "Skip grouped opportunities whose total travel (current -> buy -> sell) exceeds this many jumps."
        ),
    )
    parser.add_argument(
        "--max-investment",
        type=float,
        default=20_000_000.0,
        help="Maximum ISK to commit per item opportunity.",
    )
    parser.add_argument(
        "--max-volume",
        type=float,
        default=140.0,
        help="Maximum cubic meters per item opportunity (default: 200m3).",
    )
    parser.add_argument(
        "--max-item-volume",
        type=float,
        default=None,
        help="Maximum cubic meters per individual item (skip larger items if set).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="Display only the top N grouped routes per refresh (default: 25).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=None,
        help="Limit scanning to this many pages per region (testing helper).",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.1,
        help="Seconds to sleep after each ESI call to avoid throttling (default: 0.1).",
    )
    parser.add_argument(
        "--item-limit",
        type=int,
        default=None,
        help="Consider only the first N published items (testing helper).",
    )
    parser.add_argument(
        "--route-flag",
        choices=sorted(VALID_FLAGS),
        default="shortest",
        help="Routing preference passed to the ESI route API.",
    )
    parser.add_argument(
        "--forge-adjacent-only",
        action="store_true",
        help="Limit scanning to The Forge and its directly adjacent regions",
    )
    parser.add_argument(
        "--final-output",
        type=Path,
        default=DEFAULT_FINAL_OUTPUT,
        help="Write the final grouped snapshot to this path.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    try:
        items_payload = load_json_list(args.items)
        regions_payload = load_json_list(args.regions)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Failed to load prerequisite data: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        name_to_id, id_to_name = load_system_indexes(Path(args.systems))
    except (OSError, ValueError) as exc:
        print(f"Failed to load system metadata: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        current_system_id = resolve_system_identifier(args.current_system, name_to_id, id_to_name)
    except ValueError as exc:
        print(f"Invalid --current-system: {exc}", file=sys.stderr)
        sys.exit(1)
    current_system_name = id_to_name[current_system_id]

    item_lookup = build_item_lookup(items_payload, limit=args.item_limit)
    if not item_lookup:
        print("No published items available after applying filters", file=sys.stderr)
        sys.exit(1)
    regions = build_regions(regions_payload)
    if args.forge_adjacent_only:
        before = len(regions)
        regions = [region for region in regions if region.region_id in FORGE_ADJACENT_REGION_IDS]
        if not regions:
            print(
                "No regions remain after applying --forge-adjacent-only.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"Forge adjacency filter active: scanning {len(regions)} of {before} regions.",
            file=sys.stderr,
        )
    if not regions:
        print("No regions to scan", file=sys.stderr)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    http_client = HttpClient(session, request_delay=args.request_delay)
    tracker = ArbitrageTracker(item_lookup)
    resolver = SystemResolver(id_to_name)
    jump_cache = JumpCache(flag=args.route_flag)

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
            min_multiplier=args.min_multiplier,
            max_investment=args.max_investment,
            max_volume=args.max_volume,
            max_item_volume=args.max_item_volume,
        )
        routes_raw = group_opportunities(
            opportunities,
            resolver=resolver,
            jump_cache=jump_cache,
            current_system_id=current_system_id,
        )
        routes = filter_routes_by_profit_per_jump(
            routes_raw,
            min_profit_per_jump=args.min_profit_per_jump,
            max_total_jumps=args.max_total_jumps,
        )
        if not routes and routes_raw:
            reasons = describe_active_route_filters(
                min_profit_per_jump=args.min_profit_per_jump,
                max_total_jumps=args.max_total_jumps,
            )
            if reasons:
                print(
                    f"All {len(routes_raw)} routes were filtered out by "
                    + " and ".join(reasons)
                    + ". Adjust the thresholds to inspect them."
                )
        report = render_route_report(
            routes,
            top_n=args.top,
            current_system_name=current_system_name,
        )
        print(f"\n=== After completing page {page} across {pages_fetched} regions ===")
        print(report)
        page += 1
        if page == 2:
            print("Completed first sweep across all regions. Moving to page 2.")

    print("Scanning finished. Final grouped opportunity snapshot:")
    opportunities = tracker.opportunities(
        min_multiplier=args.min_multiplier,
        max_investment=args.max_investment,
        max_volume=args.max_volume,
        max_item_volume=args.max_item_volume,
    )
    routes_raw = group_opportunities(
        opportunities,
        resolver=resolver,
        jump_cache=jump_cache,
        current_system_id=current_system_id,
    )
    routes = filter_routes_by_profit_per_jump(
        routes_raw,
        min_profit_per_jump=args.min_profit_per_jump,
        max_total_jumps=args.max_total_jumps,
    )
    if not routes and routes_raw:
        reasons = describe_active_route_filters(
            min_profit_per_jump=args.min_profit_per_jump,
            max_total_jumps=args.max_total_jumps,
        )
        if reasons:
            print(
                f"All {len(routes_raw)} routes were filtered out by "
                + " and ".join(reasons)
                + ". Adjust the thresholds to inspect them."
            )
    final_report = render_route_report(
        routes,
        top_n=args.top,
        current_system_name=current_system_name,
    )
    print(final_report)
    jump_cache.flush()
    try:
        args.final_output.parent.mkdir(parents=True, exist_ok=True)
        args.final_output.write_text(final_report.rstrip() + "\n", encoding="utf-8")
        print(f"Final snapshot saved to {args.final_output}")
    except OSError as exc:
        print(f"Warning: failed to write final snapshot to {args.final_output}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
