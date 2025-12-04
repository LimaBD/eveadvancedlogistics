"""Identify the best manufacturing targets for any station in The Forge and nearby regions.

The script consumes cached market orders plus blueprint and item metadata to
evaluate every candidate install location within The Forge (and, by default,
neighboring high-sec regions). Each blueprint/run is priced as follows:

* Inputs are always sourced from sell orders (either at the install station or
    through a region-wide aggregate book).
* Outputs are immediately liquidated into existing buy orders at the most
    profitable station that has enough volume, so broker fees are never applied.
* System cost indices are fetched directly from ESI each time the script runs.

The resulting report focuses on "What to build?", "Where to build it?", and
"Where to sell it?" together with blueprint cost, input spend, and realized
profit per run/hour.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
SYSTEMS_URL = f"{ESI_BASE}/industry/systems/?datasource={DATASOURCE}"
USER_AGENT = "EveAdvancedLogistics/0.1 (manufacturing finder)"
DEFAULT_ORDERS = Path("data/market_orders_snapshot.json")
DEFAULT_BLUEPRINTS = Path("data/blueprints.json")
DEFAULT_ITEMS = Path("data/items.json")
DEFAULT_STATIONS = Path("data/stations.json")
DEFAULT_SYSTEMS = Path("data/systems.json")
THE_FORGE_REGION_ID = 10000002
DEFAULT_TOP = 100
DEFAULT_SALES_TAX = 0.08
DEFAULT_INDUSTRY_TAX = 0.10
REQUEST_TIMEOUT = 25
MATERIAL_SCOPE_CHOICES = ("station", "region")
DEFAULT_NEARBY_REGION_IDS = (10000016, 10000033)  # Lonetrek, The Citadel
MAX_BLUEPRINT_INVESTMENT = 100_000_000


def progress(message: str) -> None:
    """Emit a progress message that immediately flushes to stdout."""
    print(f"[progress] {message}", flush=True)


@dataclass
class Order:
    type_id: int
    price: float
    volume_remain: int
    is_buy_order: bool
    location_id: int


@dataclass
class Material:
    type_id: int
    quantity: int


@dataclass
class BlueprintProduct:
    blueprint_id: int
    product_type_id: int
    product_quantity: int
    base_time: int
    materials: List[Material]
    blueprint_name: Optional[str] = None


@dataclass
class MarketLevels:
    sell: List[Tuple[float, int]]
    buy: List[Tuple[float, int]]


@dataclass
class ManufacturingResult:
    product_type_id: int
    product_name: str
    blueprint_name: Optional[str]
    blueprint_cost: float
    runs_per_hour: float
    profit_per_run: float
    profit_per_hour: float
    roi: float
    sale_price: float
    sale_mode: str
    material_cost: float
    job_cost: float
    buy_price_anchor: Optional[float]
    sell_price_anchor: Optional[float]
    gross_revenue: float
    total_cost: float
    source_location_id: int
    source_location_name: Optional[str]
    source_system_id: int
    source_system_name: Optional[str]
    sale_location_id: int
    sale_location_name: Optional[str]
    cost_index: float


@dataclass
class StationInfo:
    station_id: int
    name: Optional[str]
    system_id: int


@dataclass
class SystemInfo:
    system_id: int
    name: Optional[str]
    region_id: int


@dataclass
class SourceContext:
    location_id: int
    location_name: Optional[str]
    system_id: int
    system_name: Optional[str]
    cost_index: float
    market: "LocalMarket"


@dataclass
class SaleContext:
    location_id: int
    location_name: Optional[str]
    system_id: int
    system_name: Optional[str]
    market: "LocalMarket"
    buy_liquidity: float


@dataclass
class SaleOption:
    context: SaleContext
    unit_price: float
    gross_revenue: float
    net_revenue: float
    buy_anchor: float
    sell_anchor: Optional[float]


class LocalMarket:
    def __init__(self, *, location_id: int) -> None:
        self.location_id = location_id
        self.books: Dict[int, MarketLevels] = {}
        self._buy_liquidity: Optional[float] = None

    def ingest(self, order: Order) -> None:
        book = self.books.setdefault(order.type_id, MarketLevels(sell=[], buy=[]))
        target = book.buy if order.is_buy_order else book.sell
        target.append((order.price, order.volume_remain))

    def finalize(self) -> None:
        for book in self.books.values():
            book.sell.sort(key=lambda entry: entry[0])
            book.buy.sort(key=lambda entry: entry[0], reverse=True)

    def best_sell_price(self, type_id: int) -> Optional[float]:
        book = self.books.get(type_id)
        if not book or not book.sell:
            return None
        return book.sell[0][0]

    def best_buy_price(self, type_id: int) -> Optional[float]:
        book = self.books.get(type_id)
        if not book or not book.buy:
            return None
        return book.buy[0][0]

    def total_sell_cost(self, type_id: int, quantity: int) -> Optional[float]:
        if quantity <= 0:
            return 0.0
        book = self.books.get(type_id)
        if not book or not book.sell:
            return None
        remaining = quantity
        total = 0.0
        for price, available in book.sell:
            if available <= 0:
                continue
            take = min(remaining, available)
            total += take * price
            remaining -= take
            if remaining <= 0:
                return total
        return None

    def total_buy_fill(self, type_id: int, quantity: int) -> Optional[float]:
        if quantity <= 0:
            return 0.0
        book = self.books.get(type_id)
        if not book or not book.buy:
            return None
        remaining = quantity
        total = 0.0
        for price, available in book.buy:
            if available <= 0:
                continue
            take = min(remaining, available)
            total += take * price
            remaining -= take
            if remaining <= 0:
                return total
        return None

    def buy_liquidity(self) -> float:
        if self._buy_liquidity is not None:
            return self._buy_liquidity
        total = 0.0
        for book in self.books.values():
            for price, available in book.buy:
                if price <= 0 or available <= 0:
                    continue
                total += price * available
        self._buy_liquidity = total
        return total


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find the best manufacturing targets across The Forge (and optional nearby regions) using cached market orders."
    )
    parser.add_argument("--orders", type=Path, default=DEFAULT_ORDERS, help="Path to cached orders JSON file.")
    parser.add_argument("--blueprints", type=Path, default=DEFAULT_BLUEPRINTS, help="Path to blueprint JSON derived from the CCP SDE.")
    parser.add_argument("--items", type=Path, default=DEFAULT_ITEMS, help="Path to items JSON for name resolution.")
    parser.add_argument("--stations", type=Path, default=DEFAULT_STATIONS, help="Path to stations JSON for station metadata.")
    parser.add_argument("--systems", type=Path, default=DEFAULT_SYSTEMS, help="Path to systems JSON for region lookups.")
    parser.add_argument(
        "--region-id",
        type=int,
        default=THE_FORGE_REGION_ID,
        help="Primary region ID to evaluate (default: The Forge). Use 0 to rely solely on nearby/extra regions.",
    )
    parser.add_argument(
        "--extra-region-id",
        dest="extra_region_ids",
        type=int,
        action="append",
        help="Supplementary region IDs to include when searching for install and sale stations.",
    )
    parser.add_argument(
        "--no-nearby",
        dest="include_nearby",
        action="store_false",
        help="Disable the default inclusion of Lonetrek and The Citadel when scanning for stations.",
    )
    parser.set_defaults(include_nearby=True)
    parser.add_argument(
        "--material-region-id",
        dest="material_region_ids",
        type=int,
        action="append",
        help="Region IDs allowed when sourcing materials (defaults to the search regions).",
    )
    parser.add_argument(
        "--material-location-id",
        dest="material_location_ids",
        type=int,
        action="append",
        help="Restrict input sourcing to explicit station IDs (repeat flag for multiples).",
    )
    parser.add_argument(
        "--max-source-locations",
        type=int,
        default=25,
        help="Maximum number of install stations (sorted by cost index) to evaluate.",
    )
    parser.add_argument(
        "--max-sale-locations",
        type=int,
        default=25,
        help="Maximum number of sale hubs to consider when dumping into buy orders.",
    )
    parser.add_argument(
        "--material-scope",
        choices=list(MATERIAL_SCOPE_CHOICES),
        default="region",
        help="Where inputs are purchased: 'region' aggregates all allowed sourcing stations, 'station' uses the install station only.",
    )
    parser.add_argument("--me", type=float, default=0.0, help="Material efficiency percentage (0-10 typical).")
    parser.add_argument("--te", type=float, default=0.0, help="Time efficiency percentage (0-20 typical).")
    parser.add_argument("--sales-tax", type=float, default=DEFAULT_SALES_TAX, help="Sales tax rate applied to buy-order liquidations.")
    parser.add_argument("--industry-tax", type=float, default=DEFAULT_INDUSTRY_TAX, help="Facility tax multiplier applied to job installation cost.")
    parser.add_argument("--min-profit", type=float, default=0.0, help="Minimum ISK profit per run.")
    parser.add_argument("--min-profit-hour", type=float, default=0.0, help="Minimum ISK profit per hour of job time.")
    parser.add_argument("--max-blueprints", type=int, default=None, help="Process only the first N blueprint entries (after filtering).")
    parser.add_argument("--top", type=int, default=DEFAULT_TOP, help="Number of rows to print.")
    parser.add_argument(
        "--sort-by",
        choices=["profit_per_hour", "profit_per_run", "roi"],
        default="profit_per_hour",
        help="Primary ordering for the final table.",
    )
    return parser.parse_args()


def load_orders_snapshot(path: Path, *, location_filter: Optional[Set[int]] = None) -> List[Order]:
    snapshot_path = path.expanduser()
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Orders snapshot {snapshot_path} does not exist")
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    orders_payload = payload.get("orders")
    if not isinstance(orders_payload, list):
        raise ValueError("Orders snapshot missing 'orders' array")
    orders: List[Order] = []
    for entry in orders_payload:
        try:
            loc = int(entry.get("location_id") or 0)
            if location_filter is not None and loc not in location_filter:
                continue
            volume = int(entry.get("volume_remain") or 0)
            if volume <= 0:
                continue
            order = Order(
                type_id=int(entry.get("type_id") or 0),
                price=float(entry.get("price") or 0.0),
                volume_remain=volume,
                is_buy_order=bool(entry.get("is_buy_order", False)),
                location_id=loc,
            )
            orders.append(order)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid order entry: {entry}") from exc
    if not orders:
        if location_filter:
            raise ValueError("No usable orders matched the provided location filters")
        raise ValueError("No usable orders were found in the snapshot")
    return orders


def estimate_adjusted_prices_from_orders(orders: Iterable[Order]) -> Dict[int, float]:
    aggregates: Dict[int, Tuple[float, int]] = {}
    for order in orders:
        if order.is_buy_order:
            continue
        if order.volume_remain <= 0:
            continue
        total_value, total_volume = aggregates.get(order.type_id, (0.0, 0))
        total_value += order.price * order.volume_remain
        total_volume += order.volume_remain
        aggregates[order.type_id] = (total_value, total_volume)
    estimates: Dict[int, float] = {}
    for type_id, (total_value, total_volume) in aggregates.items():
        if total_volume <= 0:
            continue
        estimates[type_id] = total_value / total_volume
    if not estimates:
        raise ValueError("No sell orders with volume were found in the snapshot")
    return estimates


def load_items(path: Path) -> Dict[int, str]:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("items.json must contain an array")
    lookup: Dict[int, str] = {}
    for entry in payload:
        try:
            lookup[int(entry["id"])] = str(entry["name"])
        except (KeyError, ValueError, TypeError):
            continue
    return lookup


def load_station_index(path: Path) -> Dict[int, StationInfo]:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("stations.json must contain an array")
    stations: Dict[int, StationInfo] = {}
    for entry in payload:
        try:
            station_id = int(entry["id"])
            system_id = int(entry["system_id"])
            name = entry.get("name")
            stations[station_id] = StationInfo(
                station_id=station_id,
                name=str(name) if name else None,
                system_id=system_id,
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not stations:
        raise ValueError("stations index did not contain any usable entries")
    return stations


def load_system_index(path: Path) -> Dict[int, SystemInfo]:
    payload = json.loads(path.expanduser().read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("systems.json must contain an array")
    systems: Dict[int, SystemInfo] = {}
    for entry in payload:
        try:
            system_id = int(entry["id"])
            region_id = int(entry["region_id"])
            name = entry.get("name")
            systems[system_id] = SystemInfo(
                system_id=system_id,
                name=str(name) if name else None,
                region_id=region_id,
            )
        except (KeyError, TypeError, ValueError):
            continue
    if not systems:
        raise ValueError("systems index did not contain any usable entries")
    return systems


def filter_systems_by_regions(
    systems: Mapping[int, SystemInfo],
    *,
    allowed_regions: Set[int],
    preserve_system_ids: Optional[Set[int]] = None,
) -> Dict[int, SystemInfo]:
    if not allowed_regions and not preserve_system_ids:
        return dict(systems)
    filtered: Dict[int, SystemInfo] = {}
    for system_id, system in systems.items():
        if system.region_id in allowed_regions:
            filtered[system_id] = system
            continue
        if preserve_system_ids and system_id in preserve_system_ids:
            filtered[system_id] = system
    return filtered


def filter_stations_by_systems(
    stations: Mapping[int, StationInfo],
    *,
    allowed_system_ids: Set[int],
    preserve_station_ids: Optional[Set[int]] = None,
) -> Dict[int, StationInfo]:
    if not allowed_system_ids and not preserve_station_ids:
        return dict(stations)
    filtered: Dict[int, StationInfo] = {}
    for station_id, station in stations.items():
        if station.system_id in allowed_system_ids:
            filtered[station_id] = station
            continue
        if preserve_station_ids and station_id in preserve_station_ids:
            filtered[station_id] = station
    return filtered


def parse_material(entry: Mapping[str, object]) -> Material:
    raw_type = entry.get("type_id") or entry.get("typeID")
    raw_qty = entry.get("quantity")
    if raw_type is None or raw_qty is None:
        raise ValueError(f"Material entry missing fields: {entry}")
    return Material(type_id=int(raw_type), quantity=int(raw_qty))


def load_blueprints(path: Path, *, max_entries: Optional[int]) -> List[BlueprintProduct]:
    blueprint_path = path.expanduser()
    if not blueprint_path.exists():
        raise FileNotFoundError(f"Blueprint file {blueprint_path} not found")
    payload = json.loads(blueprint_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Blueprint file must contain a JSON array")
    products: List[BlueprintProduct] = []
    remaining = max_entries if max_entries is None else max(1, max_entries)
    for entry in payload:
        activities = entry.get("activities")
        if not isinstance(activities, Mapping):
            continue
        manufacturing = activities.get("manufacturing")
        if not isinstance(manufacturing, Mapping):
            continue
        materials_payload = manufacturing.get("materials")
        products_payload = manufacturing.get("products")
        base_time = manufacturing.get("time")
        if not (isinstance(materials_payload, list) and isinstance(products_payload, list)):
            continue
        if base_time is None:
            continue
        material_list: List[Material] = []
        try:
            for material_entry in materials_payload:
                if isinstance(material_entry, Mapping):
                    material_list.append(parse_material(material_entry))
        except ValueError:
            continue
        if not material_list:
            continue
        blueprint_id = entry.get("blueprintTypeID") or entry.get("blueprint_id") or entry.get("type_id")
        blueprint_name = entry.get("name") or entry.get("blueprintTypeName")
        for product_entry in products_payload:
            if not isinstance(product_entry, Mapping):
                continue
            raw_prod_type = product_entry.get("type_id") or product_entry.get("typeID")
            raw_qty = product_entry.get("quantity")
            if raw_prod_type is None or raw_qty is None:
                continue
            product = BlueprintProduct(
                blueprint_id=int(blueprint_id) if blueprint_id is not None else -1,
                product_type_id=int(raw_prod_type),
                product_quantity=int(raw_qty),
                base_time=int(base_time),
                materials=list(material_list),
                blueprint_name=str(blueprint_name) if blueprint_name else None,
            )
            products.append(product)
            if remaining is not None:
                remaining -= 1
                if remaining == 0:
                    return products
    return products


def build_markets_by_location(orders: Iterable[Order]) -> Dict[int, LocalMarket]:
    markets: Dict[int, LocalMarket] = {}
    for order in orders:
        market = markets.setdefault(order.location_id, LocalMarket(location_id=order.location_id))
        market.ingest(order)
    for market in markets.values():
        market.finalize()
    return markets


def build_aggregated_market(orders: Iterable[Order], *, allowed_locations: Optional[Set[int]]) -> LocalMarket:
    aggregate = LocalMarket(location_id=-1)
    matched = False
    for order in orders:
        if allowed_locations is not None and order.location_id not in allowed_locations:
            continue
        aggregate.ingest(order)
        matched = True
    if not matched:
        raise ValueError("No orders matched the material sourcing scope")
    aggregate.finalize()
    return aggregate


def determine_search_regions(
    primary_region: Optional[int], *, include_nearby: bool, extra_regions: Optional[Sequence[int]]
) -> Set[int]:
    regions: Set[int] = set()
    if primary_region and primary_region > 0:
        regions.add(primary_region)
    if include_nearby:
        regions.update(DEFAULT_NEARBY_REGION_IDS)
    if extra_regions:
        for candidate in extra_regions:
            if candidate and candidate > 0:
                regions.add(candidate)
    if not regions:
        regions.add(THE_FORGE_REGION_ID)
    return regions


def resolve_station_ids_for_regions(
    stations: Mapping[int, StationInfo], systems: Mapping[int, SystemInfo], region_ids: Set[int]
) -> Set[int]:
    if not region_ids:
        return {station_id for station_id in stations}
    resolved: Set[int] = set()
    for station_id, station in stations.items():
        system = systems.get(station.system_id)
        if not system:
            continue
        if system.region_id in region_ids:
            resolved.add(station_id)
    return resolved


def select_candidate_locations(
    markets: Mapping[int, LocalMarket],
    *,
    stations: Mapping[int, StationInfo],
    systems: Mapping[int, SystemInfo],
    cost_indices: Mapping[int, float],
    allowed_location_ids: Optional[Set[int]],
    region_id: Optional[int],
    max_locations: Optional[int],
) -> List[SourceContext]:
    candidates: List[SourceContext] = []
    for location_id, market in markets.items():
        if allowed_location_ids is not None and location_id not in allowed_location_ids:
            continue
        station = stations.get(location_id)
        if not station:
            continue
        system = systems.get(station.system_id)
        if not system:
            continue
        if allowed_location_ids is None and region_id is not None and system.region_id != region_id:
            continue
        cost_index = cost_indices.get(system.system_id)
        if cost_index is None:
            continue
        candidates.append(
            SourceContext(
                location_id=location_id,
                location_name=station.name,
                system_id=system.system_id,
                system_name=system.name,
                cost_index=cost_index,
                market=market,
            )
        )
    candidates.sort(key=lambda ctx: (ctx.cost_index, ctx.location_name or "", ctx.location_id))
    if max_locations is not None and max_locations > 0:
        limit = max(1, max_locations)
        candidates = candidates[:limit]
    return candidates


def select_sale_locations(
    markets: Mapping[int, LocalMarket],
    *,
    stations: Mapping[int, StationInfo],
    systems: Mapping[int, SystemInfo],
    allowed_location_ids: Optional[Set[int]],
    max_locations: Optional[int],
) -> List[SaleContext]:
    candidates: List[SaleContext] = []
    for location_id, market in markets.items():
        if allowed_location_ids is not None and location_id not in allowed_location_ids:
            continue
        station = stations.get(location_id)
        if not station:
            continue
        system = systems.get(station.system_id)
        if not system:
            continue
        liquidity = market.buy_liquidity()
        if liquidity <= 0:
            continue
        candidates.append(
            SaleContext(
                location_id=location_id,
                location_name=station.name,
                system_id=system.system_id,
                system_name=system.name,
                market=market,
                buy_liquidity=liquidity,
            )
        )
    candidates.sort(key=lambda ctx: (-ctx.buy_liquidity, ctx.location_name or "", ctx.location_id))
    if max_locations is not None and max_locations > 0:
        limit = max(1, max_locations)
        candidates = candidates[:limit]
    return candidates


def fetch_manufacturing_cost_indices() -> Dict[int, float]:
    response = requests.get(SYSTEMS_URL, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected payload for cost indices")
    indices: Dict[int, float] = {}
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        system_id = entry.get("solar_system_id")
        if not isinstance(system_id, int):
            try:
                system_id = int(system_id)
            except (TypeError, ValueError):
                continue
        cost_entries = entry.get("cost_indices")
        if not isinstance(cost_entries, list):
            continue
        for idx in cost_entries:
            if not isinstance(idx, Mapping):
                continue
            if idx.get("activity") != "manufacturing":
                continue
            raw_cost = idx.get("cost_index")
            if isinstance(raw_cost, (int, float)):
                indices[system_id] = float(raw_cost)
                break
    if not indices:
        raise RuntimeError("Manufacturing cost indices were not available in the response")
    return indices


def apply_me(quantity: int, me: float) -> int:
    if me <= 0:
        return quantity
    if me >= 100:
        return max(1, math.floor(quantity * 0.01))
    multiplier = max(0.0, 1.0 - me / 100.0)
    return max(1, math.ceil(quantity * multiplier))


def apply_te(base_time: int, te: float) -> float:
    if te <= 0:
        return float(base_time)
    multiplier = max(0.0, 1.0 - te / 100.0)
    return max(1.0, base_time * multiplier)


def compute_job_cost(materials: Sequence[Material], *, adjusted_prices: Mapping[int, float], cost_index: float, facility_tax: float) -> float:
    base_cost = 0.0
    for material in materials:
        adjusted = adjusted_prices.get(material.type_id)
        if adjusted is None:
            continue
        base_cost += adjusted * material.quantity
    job_cost = base_cost * cost_index
    if facility_tax > 0:
        job_cost *= (1.0 + facility_tax)
    return job_cost


def find_best_sale_option(
    product_type_id: int,
    quantity: int,
    sale_contexts: Sequence[SaleContext],
    *,
    sales_tax: float,
) -> Optional[SaleOption]:
    if quantity <= 0:
        return None
    sales_tax = max(0.0, sales_tax)
    best_option: Optional[SaleOption] = None
    for sale_ctx in sale_contexts:
        fill_value = sale_ctx.market.total_buy_fill(product_type_id, quantity)
        if fill_value is None or fill_value <= 0:
            continue
        best_buy = sale_ctx.market.best_buy_price(product_type_id)
        if best_buy is None or best_buy <= 0:
            continue
        best_sell = sale_ctx.market.best_sell_price(product_type_id)
        unit_price = fill_value / quantity
        net_revenue = fill_value * max(0.0, 1.0 - sales_tax)
        option = SaleOption(
            context=sale_ctx,
            unit_price=unit_price,
            gross_revenue=fill_value,
            net_revenue=net_revenue,
            buy_anchor=best_buy,
            sell_anchor=best_sell,
        )
        if best_option is None or option.net_revenue > best_option.net_revenue:
            best_option = option
    return best_option


def evaluate_blueprint(
    blueprint: BlueprintProduct,
    *,
    source: SourceContext,
    material_market: Optional[LocalMarket],
    sale_contexts: Sequence[SaleContext],
    adjusted_prices: Mapping[int, float],
    facility_tax: float,
    me: float,
    te: float,
    sales_tax: float,
) -> Optional[ManufacturingResult]:
    if blueprint.blueprint_id <= 0:
        return None
    procurement_market = material_market or source.market
    blueprint_cost = procurement_market.best_sell_price(blueprint.blueprint_id)
    if blueprint_cost is None or blueprint_cost <= 0:
        return None
    if blueprint_cost > MAX_BLUEPRINT_INVESTMENT:
        return None
    effective_materials: List[Material] = []
    for material in blueprint.materials:
        effective_qty = apply_me(material.quantity, me)
        effective_materials.append(Material(type_id=material.type_id, quantity=effective_qty))
    material_cost = 0.0
    for material in effective_materials:
        spend = procurement_market.total_sell_cost(material.type_id, material.quantity)
        if spend is None:
            return None
        material_cost += spend
    job_cost = compute_job_cost(
        blueprint.materials,
        adjusted_prices=adjusted_prices,
        cost_index=source.cost_index,
        facility_tax=facility_tax,
    )
    output_units = blueprint.product_quantity
    if output_units <= 0:
        return None
    sale_option = find_best_sale_option(
        blueprint.product_type_id,
        output_units,
        sale_contexts,
        sales_tax=sales_tax,
    )
    if sale_option is None:
        return None
    net_revenue = sale_option.net_revenue
    profit_per_run = net_revenue - material_cost - job_cost
    if profit_per_run <= 0:
        return None
    adjusted_time = apply_te(blueprint.base_time, te)
    runs_per_hour = 3600.0 / adjusted_time
    profit_per_hour = profit_per_run * runs_per_hour
    total_cost = material_cost + job_cost
    roi = profit_per_run / total_cost if total_cost > 0 else float("inf")
    return ManufacturingResult(
        product_type_id=blueprint.product_type_id,
        product_name="",
        blueprint_name=blueprint.blueprint_name,
        blueprint_cost=blueprint_cost,
        runs_per_hour=runs_per_hour,
        profit_per_run=profit_per_run,
        profit_per_hour=profit_per_hour,
        roi=roi,
        sale_price=sale_option.unit_price,
        sale_mode="buy_orders",
        material_cost=material_cost,
        job_cost=job_cost,
        buy_price_anchor=sale_option.buy_anchor,
        sell_price_anchor=sale_option.sell_anchor,
        gross_revenue=sale_option.gross_revenue,
        total_cost=total_cost,
        source_location_id=source.location_id,
        source_location_name=source.location_name,
        source_system_id=source.system_id,
        source_system_name=source.system_name,
        sale_location_id=sale_option.context.location_id,
        sale_location_name=sale_option.context.location_name,
        cost_index=source.cost_index,
    )


def sort_results(results: List[ManufacturingResult], *, sort_by: str) -> List[ManufacturingResult]:
    if sort_by == "profit_per_run":
        key = lambda r: r.profit_per_run
    elif sort_by == "roi":
        key = lambda r: r.roi
    else:
        key = lambda r: r.profit_per_hour
    return sorted(results, key=key, reverse=True)


def main() -> None:
    args = parse_args()
    progress("Starting manufacturing candidate scan")
    search_region_ids = determine_search_regions(
        args.region_id if args.region_id and args.region_id > 0 else None,
        include_nearby=args.include_nearby,
        extra_regions=args.extra_region_ids,
    )
    progress(
        "Target regions: "
        + ", ".join(str(rid) for rid in sorted(search_region_ids))
        + (" (plus explicit material regions)" if args.material_region_ids else "")
    )

    try:
        progress(f"Loading station index from {args.stations} ...")
        stations = load_station_index(args.stations)
        progress(f"Loaded {len(stations)} station records")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load stations: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        progress(f"Loading system index from {args.systems} ...")
        systems = load_system_index(args.systems)
        progress(f"Loaded {len(systems)} system records")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load systems: {exc}", file=sys.stderr)
        sys.exit(1)

    material_region_ids: Set[int] = set(args.material_region_ids or [])
    if not material_region_ids:
        material_region_ids = set(search_region_ids)
    explicit_material_station_ids: Set[int] = set(args.material_location_ids or [])
    explicit_system_ids: Set[int] = set()
    explicit_region_ids: Set[int] = set()
    for station_id in explicit_material_station_ids:
        station = stations.get(station_id)
        if not station:
            continue
        explicit_system_ids.add(station.system_id)
        system = systems.get(station.system_id)
        if system:
            explicit_region_ids.add(system.region_id)

    relevant_region_ids = set(search_region_ids)
    relevant_region_ids.update(material_region_ids)
    relevant_region_ids.update(explicit_region_ids)
    progress(
        f"Filtering systems/stations to regions {sorted(relevant_region_ids)} and explicit station IDs {sorted(explicit_material_station_ids) or 'none'}"
    )

    systems = filter_systems_by_regions(
        systems,
        allowed_regions=relevant_region_ids,
        preserve_system_ids=explicit_system_ids,
    )
    stations = filter_stations_by_systems(
        stations,
        allowed_system_ids=set(systems.keys()),
        preserve_station_ids=explicit_material_station_ids,
    )

    allowed_station_ids = resolve_station_ids_for_regions(stations, systems, search_region_ids)
    if not allowed_station_ids:
        print("No stations matched the requested regions.", file=sys.stderr)
        sys.exit(1)
    material_station_ids = resolve_station_ids_for_regions(stations, systems, material_region_ids)
    if not material_station_ids:
        material_station_ids = set(allowed_station_ids)
    material_station_ids.update(explicit_material_station_ids)

    location_filter_ids = set(allowed_station_ids)
    location_filter_ids.update(material_station_ids)
    location_filter: Optional[Set[int]] = location_filter_ids if location_filter_ids else None

    try:
        progress(f"Loading orders snapshot from {args.orders} ...")
        orders = load_orders_snapshot(args.orders, location_filter=location_filter)
        progress(f"Loaded {len(orders)} orders relevant to selected stations")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load orders: {exc}", file=sys.stderr)
        sys.exit(1)

    progress("Building local market books per station ...")
    markets = build_markets_by_location(orders)
    progress(f"Constructed {len(markets)} market books")

    try:
        progress(f"Loading item metadata from {args.items} ...")
        items = load_items(args.items)
        progress(f"Loaded {len(items)} item names")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load items: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        progress(f"Loading blueprints from {args.blueprints} ...")
        blueprints = load_blueprints(args.blueprints, max_entries=args.max_blueprints)
        progress(f"Loaded {len(blueprints)} blueprint product entries")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load blueprints: {exc}", file=sys.stderr)
        sys.exit(1)

    if not blueprints:
        print("No blueprints with manufacturing activities were found.", file=sys.stderr)
        sys.exit(1)

    try:
        progress("Estimating adjusted material prices from sell orders ...")
        adjusted_prices = estimate_adjusted_prices_from_orders(orders)
        progress(f"Computed adjusted prices for {len(adjusted_prices)} item types")
    except ValueError as exc:
        print(f"Failed to derive adjusted prices from orders: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        progress("Fetching manufacturing cost indices from ESI ...")
        cost_indices = fetch_manufacturing_cost_indices()
        progress(f"Fetched cost indices for {len(cost_indices)} systems")
    except requests.RequestException as exc:
        print(f"Failed to fetch system cost indices: {exc}", file=sys.stderr)
        sys.exit(2)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)

    candidate_sources = select_candidate_locations(
        markets,
        stations=stations,
        systems=systems,
        cost_indices=cost_indices,
        allowed_location_ids=allowed_station_ids,
        region_id=None,
        max_locations=args.max_source_locations,
    )

    if not candidate_sources:
        print("No install stations satisfied the filters and had usable market data.", file=sys.stderr)
        sys.exit(1)
    progress(f"Identified {len(candidate_sources)} candidate build stations")

    sale_locations = select_sale_locations(
        markets,
        stations=stations,
        systems=systems,
        allowed_location_ids=allowed_station_ids,
        max_locations=args.max_sale_locations,
    )
    if not sale_locations:
        print("No sale hubs with market data matched the requested regions.", file=sys.stderr)
        sys.exit(1)
    progress(f"Identified {len(sale_locations)} sale hubs for liquidation")

    material_market: Optional[LocalMarket] = None
    if args.material_scope == "region":
        allowed_material_locations = material_station_ids if material_station_ids else None
        try:
            progress("Aggregating regional material sourcing market ...")
            material_market = build_aggregated_market(orders, allowed_locations=allowed_material_locations)
            progress("Regional market built successfully")
        except ValueError as exc:
            print(f"Failed to build regional material market: {exc}", file=sys.stderr)
            sys.exit(1)

    results: List[ManufacturingResult] = []
    skipped = 0
    total_sources = len(candidate_sources)
    total_blueprints = len(blueprints)
    progress(
        f"Evaluating {total_blueprints} blueprints across {total_sources} build station(s); this may take a while"
    )
    progress_interval = max(1, total_blueprints // 10)
    for source_idx, source in enumerate(candidate_sources, start=1):
        progress(
            f"Processing source {source_idx}/{total_sources}: {source.location_name or source.location_id}"
        )
        for bp_idx, blueprint in enumerate(blueprints, start=1):
            result = evaluate_blueprint(
                blueprint,
                source=source,
                material_market=material_market if args.material_scope == "region" else None,
                sale_contexts=sale_locations,
                adjusted_prices=adjusted_prices,
                facility_tax=args.industry_tax,
                me=args.me,
                te=args.te,
                sales_tax=args.sales_tax,
            )
            if result is None:
                skipped += 1
                continue
            if result.profit_per_run < args.min_profit:
                skipped += 1
                continue
            if result.profit_per_hour < args.min_profit_hour:
                skipped += 1
                continue
            result.product_name = items.get(result.product_type_id, f"Type {result.product_type_id}")
            results.append(result)
            if bp_idx % progress_interval == 0:
                progress(
                    f"Source {source_idx}/{total_sources}: evaluated {bp_idx}/{total_blueprints} blueprints"
                )
        progress(
            f"Completed source {source_idx}/{total_sources}: results so far {len(results)}, skipped {skipped}"
        )

    if not results:
        print("No profitable manufacturing targets satisfied the given thresholds.")
        print(f"Blueprints skipped: {skipped}")
        sys.exit(0)

    ordered = sort_results(results, sort_by=args.sort_by)
    top_n = max(1, args.top)
    selected = ordered[:top_n]
    region_label = ", ".join(str(rid) for rid in sorted(search_region_ids))
    print(
        f"Top {len(selected)} manufacturing candidates across regions [{region_label}] "
        f"using {len(candidate_sources)} build station(s) and {len(sale_locations)} sale hub(s)."
    )
    print(
        f"Assumptions: ME={args.me}, TE={args.te}, sales_tax={args.sales_tax:.2%}, "
        f"industry_tax={args.industry_tax:.2%}, materials={args.material_scope}"
    )
    print("")
    for idx, result in enumerate(selected, start=1):
        roi_pct = result.roi * 100.0
        source_label = result.source_location_name or f"{result.source_location_id}"
        source_system = result.source_system_name or f"{result.source_system_id}"
        sale_label = result.sale_location_name or f"{result.sale_location_id}"
        print(
            f"{idx:>2}. {result.product_name} | Build {source_label} ({source_system}) -> Sell {sale_label} | "
            f"Profit/run {result.profit_per_run:>12,.0f} ISK | Profit/hr {result.profit_per_hour:>12,.0f} ISK | ROI {roi_pct:>5.1f}%"
        )
        print(
            f"    Costs -> Blueprint {result.blueprint_cost:>12,.0f} | Inputs {result.material_cost:>12,.0f} | Job {result.job_cost:>10,.0f} | Total {result.total_cost:>12,.0f}"
        )
        print(
            f"    Sale -> Unit {result.sale_price:>12,.2f} (buy orders) | Gross {result.gross_revenue:>12,.0f} | "
            f"Anchors buy {result.buy_price_anchor or 0:,.2f} / sell {result.sell_price_anchor or 0:,.2f}"
        )
        if result.blueprint_name:
            print(f"    Blueprint: {result.blueprint_name}")
        print("")
    print(
        f"Evaluated {len(blueprints)} blueprints across {len(candidate_sources)} build station(s); skipped {skipped}."
    )


if __name__ == "__main__":
    main()
