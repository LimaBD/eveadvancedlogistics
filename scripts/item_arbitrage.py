"""Process cached market orders and rank arbitrage routes."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from calc_jumps_local import (
    DEFAULT_GRAPH_PATH,
    DEFAULT_SYSTEMS_PATH,
    LocalJumpCache,
    VALID_FLAGS,
    load_system_indexes,
    resolve_system_identifier,
)


DEFAULT_ITEMS = Path("data/items.json")
DEFAULT_REGIONS = Path("data/regions.json")
DEFAULT_SYSTEMS = DEFAULT_SYSTEMS_PATH
DEFAULT_SYSTEM_GRAPH = DEFAULT_GRAPH_PATH
DEFAULT_FINAL_OUTPUT = Path("data/arbitrage_items_summary.txt")
DEFAULT_ORDERS_SNAPSHOT = Path("data/market_orders_snapshot.json")
PLAYER_STRUCTURE_MIN_LOCATION_ID = 1_000_000_000_000
THE_FORGE_REGION_ID = 10000002
FORGE_ADJACENT_REGION_IDS = {
    THE_FORGE_REGION_ID,
    10000016,  # Lonetrek
    10000033,  # The Citadel
    10000029,  # Geminate
    10000003,  # Vale of the Silent
}


def is_player_structure(location_id: int) -> bool:
    """Player-owned structures use large location IDs (>= 1e12)."""
    return location_id >= PLAYER_STRUCTURE_MIN_LOCATION_ID


def load_system_security(path: Path) -> Dict[int, float]:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Systems file {path} missing; cannot evaluate security status")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"Systems file {path} must contain a JSON array")
    lookup: Dict[int, float] = {}
    for entry in payload:
        if not isinstance(entry, Mapping):
            continue
        system_id = entry.get("id")
        security = entry.get("security_status")
        if system_id is None or security is None:
            continue
        try:
            lookup[int(system_id)] = float(security)
        except (TypeError, ValueError):
            continue
    return lookup


@dataclass
class ProfileConfig:
    profile_name: Optional[str] = None
    profile_path: Optional[Path] = None
    items: Path = DEFAULT_ITEMS
    regions: Path = DEFAULT_REGIONS
    systems: Path = DEFAULT_SYSTEMS
    system_graph: Path = DEFAULT_SYSTEM_GRAPH
    current_system: Optional[str] = None
    min_multiplier: float = 1.2
    min_profit_per_jump: Optional[float] = None
    max_total_jumps: Optional[int] = None
    max_investment: float = 20_000_000.0
    max_volume: float = 140.0
    max_units: Optional[int] = None
    max_items: Optional[int] = None
    max_item_volume: Optional[float] = None
    top: int = 15
    item_limit: Optional[int] = None
    route_flag: str = "shortest"
    forge_adjacent_only: bool = False
    min_security_status: Optional[float] = None
    final_output: Path = DEFAULT_FINAL_OUTPUT
    partial_top: int = 5
    partial_interval: int = 50


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


def _expand_path(value: object | None, default: Path, *, base_dir: Path) -> Path:
    if value is None:
        return default
    candidate = value if isinstance(value, Path) else Path(str(value))
    candidate = candidate.expanduser()
    if not candidate.is_absolute():
        candidate = (base_dir / candidate).resolve()
    return candidate


def _coerce_optional_float(value: object | None) -> Optional[float]:
    if value is None:
        return None
    return float(value)


def _coerce_optional_int(value: object | None) -> Optional[int]:
    if value is None:
        return None
    return int(value)


def _coerce_bool(value: object | None, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y", "on"}:
            return True
        if lowered in {"false", "0", "no", "n", "off"}:
            return False
    raise ValueError(f"Expected boolean value, received {value!r}")


def load_profile_config(profile_path: Path) -> ProfileConfig:
    profile_path = profile_path.expanduser().resolve()
    if not profile_path.exists():
        raise FileNotFoundError(f"Profile file {profile_path} does not exist")
    payload = json.loads(profile_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Profile {profile_path} must contain a JSON object")
    config = ProfileConfig()
    config.profile_path = profile_path
    config.profile_name = str(payload.get("name") or profile_path.stem)
    base_dir = profile_path.parent
    if "items" in payload:
        config.items = _expand_path(payload["items"], config.items, base_dir=base_dir)
    if "regions" in payload:
        config.regions = _expand_path(payload["regions"], config.regions, base_dir=base_dir)
    if "systems" in payload:
        config.systems = _expand_path(payload["systems"], config.systems, base_dir=base_dir)
    graph_override = (
        payload.get("system_graph")
        or payload.get("graph")
        or payload.get("graph_file")
    )
    if graph_override is not None:
        config.system_graph = _expand_path(graph_override, config.system_graph, base_dir=base_dir)
    if "final_output" in payload:
        config.final_output = _expand_path(payload["final_output"], config.final_output, base_dir=base_dir)
    if "current_system" in payload:
        raw_system = payload["current_system"]
        config.current_system = None if raw_system is None else str(raw_system)
    if "min_multiplier" in payload:
        config.min_multiplier = float(payload["min_multiplier"])
    config.min_profit_per_jump = _coerce_optional_float(payload.get("min_profit_per_jump"))
    config.max_total_jumps = _coerce_optional_int(payload.get("max_total_jumps"))
    if "max_investment" in payload:
        config.max_investment = float(payload["max_investment"])
    if "max_volume" in payload:
        config.max_volume = float(payload["max_volume"])
    config.max_units = _coerce_optional_int(payload.get("max_units"))
    config.max_items = _coerce_optional_int(payload.get("max_items"))
    config.max_item_volume = _coerce_optional_float(payload.get("max_item_volume"))
    if "top" in payload:
        config.top = int(payload["top"])
    config.item_limit = _coerce_optional_int(payload.get("item_limit"))
    if "route_flag" in payload:
        route_flag = str(payload["route_flag"])
        if route_flag not in VALID_FLAGS:
            raise ValueError(
                f"Invalid route_flag '{route_flag}' in {profile_path}. Choose from {sorted(VALID_FLAGS)}"
            )
        config.route_flag = route_flag
    config.forge_adjacent_only = _coerce_bool(payload.get("forge_adjacent_only"), config.forge_adjacent_only)
    config.min_security_status = _coerce_optional_float(payload.get("min_security_status"))
    if "partial_top" in payload:
        config.partial_top = max(0, int(payload["partial_top"]))
    if "partial_interval" in payload:
        config.partial_interval = max(1, int(payload["partial_interval"]))
    return config


def load_json_list(path: Path) -> List[Dict]:
    path = path.expanduser().resolve()
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
    transit_to_buy: Optional[ItemOpportunity] = None
    transit_to_sell: Optional[ItemOpportunity] = None

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
        effective_jumps = jumps if jumps > 0 else 1
        return self.total_profit / effective_jumps


class SystemResolver:
    def __init__(self, lookup: Mapping[int, str]):
        self.lookup = lookup

    def resolve(self, system_id: int) -> str:
        return self.lookup.get(system_id, f"System {system_id}")


@dataclass
class ItemOrderBook:
    sells: Dict[int, Order] = field(default_factory=dict)
    buys: Dict[int, Order] = field(default_factory=dict)

    def ingest(self, order: Order) -> None:
        bucket = self.buys if order.is_buy_order else self.sells
        existing = bucket.get(order.system_id)
        is_better = False
        if existing is None:
            is_better = True
        elif order.is_buy_order:
            is_better = order.price > existing.price
        else:
            is_better = order.price < existing.price
        if is_better:
            bucket[order.system_id] = order


class ArbitrageTracker:
    def __init__(self, item_lookup: Mapping[int, ItemMetadata]):
        self.item_lookup = item_lookup
        self.books: Dict[int, ItemOrderBook] = {}

    def ingest(self, order: Order) -> None:
        if order.type_id not in self.item_lookup:
            return
        if order.volume_remain <= 0:
            return
        book = self.books.setdefault(order.type_id, ItemOrderBook())
        book.ingest(order)

    def opportunities(
        self,
        *,
        min_multiplier: float,
        max_investment: Optional[float],
        max_volume: Optional[float],
        max_item_volume: Optional[float],
        max_units_cap: Optional[int],
        max_items_cap: Optional[int],
        progress_callback: Optional[Callable[[int, ItemOpportunity], None]] = None,
    ) -> List[ItemOpportunity]:
        opps: List[ItemOpportunity] = []
        for type_id, book in self.books.items():
            if not book.sells or not book.buys:
                continue
            item_meta = self.item_lookup[type_id]
            unit_volume = max(item_meta.unit_volume, 0.0)
            if max_item_volume is not None and unit_volume > max_item_volume:
                continue
            for sell_order in book.sells.values():
                if sell_order.min_volume > 1:
                    continue
                for buy_order in book.buys.values():
                    if buy_order.price <= sell_order.price:
                        continue
                    if buy_order.min_volume > 1:
                        continue
                    tradable = min(sell_order.volume_remain, buy_order.volume_remain)
                    if tradable <= 0:
                        continue
                    if max_units_cap is not None:
                        if max_units_cap <= 0:
                            continue
                        tradable = min(tradable, max_units_cap)
                    if max_investment is not None:
                        if sell_order.price > max_investment:
                            continue
                        invest_cap = int(max_investment // sell_order.price)
                        tradable = min(tradable, invest_cap)
                    if tradable <= 0:
                        continue
                    if max_volume is not None and unit_volume > 0:
                        volume_cap = int(max_volume // unit_volume)
                        tradable = min(tradable, volume_cap)
                    if tradable <= 0:
                        continue
                    unit_profit = buy_order.price - sell_order.price
                    multiplier = buy_order.price / sell_order.price if sell_order.price else 0.0
                    if multiplier < min_multiplier:
                        continue
                    total_profit = unit_profit * tradable
                    total_investment = sell_order.price * tradable
                    total_volume = unit_volume * tradable
                    if max_volume is not None and total_volume > max_volume + 1e-6:
                        continue
                    opps.append(
                        ItemOpportunity(
                            type_id=type_id,
                            item_name=item_meta.name,
                            buy_from=sell_order,
                            sell_to=buy_order,
                            unit_profit=unit_profit,
                            tradable_units=tradable,
                            total_profit=total_profit,
                            multiplier=multiplier,
                            total_investment=total_investment,
                            total_volume=total_volume,
                        )
                    )
                    if progress_callback is not None:
                        progress_callback(len(opps), opps[-1])
        ordered = sorted(opps, key=lambda entry: (entry.total_profit, entry.multiplier), reverse=True)
        if max_items_cap is not None:
            if max_items_cap <= 0:
                return []
            return ordered[:max_items_cap]
        return ordered


def load_orders_snapshot(path: Path) -> Tuple[List[Order], Mapping[str, object]]:
    snapshot_path = path.expanduser().resolve()
    if not snapshot_path.exists():
        raise FileNotFoundError(f"Orders snapshot {snapshot_path} does not exist")
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    orders_payload = payload.get("orders")
    if not isinstance(orders_payload, list):
        raise ValueError(f"Snapshot {snapshot_path} does not contain an 'orders' array")
    orders: List[Order] = []
    for entry in orders_payload:
        if not isinstance(entry, Mapping):
            continue
        try:
            order = Order(
                order_id=int(entry.get("order_id") or 0),
                type_id=int(entry.get("type_id") or 0),
                price=float(entry.get("price") or 0.0),
                volume_remain=int(entry.get("volume_remain") or 0),
                min_volume=int(entry.get("min_volume") or 1),
                is_buy_order=bool(entry.get("is_buy_order", False)),
                system_id=int(entry.get("system_id") or 0),
                location_id=int(entry.get("location_id") or 0),
                region_id=int(entry.get("region_id") or 0),
                region_name=str(entry.get("region_name") or f"Region {entry.get('region_id')}")
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid order entry in snapshot: {entry}") from exc
        if order.volume_remain <= 0:
            continue
        orders.append(order)
    if not orders:
        raise ValueError(f"Snapshot {snapshot_path} does not contain usable orders")
    return orders, payload


def group_opportunities(
    opportunities: Sequence[ItemOpportunity],
    *,
    resolver: SystemResolver,
    jump_cache: LocalJumpCache,
    current_system_id: int,
    max_total_volume: Optional[float],
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
            group.jumps_from_current = jump_cache.jumps(current_system_id, buy_sys)
            group.jumps_trade = jump_cache.jumps(buy_sys, sell_sys)
        candidate = opportunity
        if max_total_volume is not None:
            remaining_volume = max_total_volume - group.total_volume
            if remaining_volume <= 1e-6:
                continue
            if candidate.total_volume - remaining_volume > 1e-6:
                unit_volume = 0.0
                if candidate.tradable_units > 0:
                    unit_volume = candidate.total_volume / candidate.tradable_units
                if unit_volume > 0:
                    max_units_by_volume = int(math.floor((remaining_volume + 1e-9) / unit_volume))
                    if max_units_by_volume <= 0:
                        continue
                    allowed_units = min(candidate.tradable_units, max_units_by_volume)
                    if allowed_units < candidate.tradable_units:
                        candidate = ItemOpportunity(
                            type_id=candidate.type_id,
                            item_name=candidate.item_name,
                            buy_from=candidate.buy_from,
                            sell_to=candidate.sell_to,
                            unit_profit=candidate.unit_profit,
                            tradable_units=allowed_units,
                            total_profit=candidate.unit_profit * allowed_units,
                            multiplier=candidate.multiplier,
                            total_investment=candidate.buy_from.price * allowed_units,
                            total_volume=unit_volume * allowed_units,
                        )
        group.items.append(candidate)
        group.total_profit += candidate.total_profit
        group.total_investment += candidate.total_investment
        group.total_volume += candidate.total_volume
        group.total_units += candidate.tradable_units
        group.multiplier_hint = max(group.multiplier_hint, candidate.multiplier)
    ordered = sorted(
        grouped.values(),
        key=lambda entry: (entry.isk_per_jump or -math.inf, entry.total_profit),
        reverse=True,
    )
    return ordered


def _best_item_from_route(route: Optional[RouteOpportunity]) -> Optional[ItemOpportunity]:
    if route is None or not route.items:
        return None
    return max(route.items, key=lambda item: (item.total_profit, item.multiplier))


def annotate_transit_opportunities(
    routes: Sequence[RouteOpportunity],
    *,
    route_lookup: Mapping[Tuple[int, int], RouteOpportunity],
    current_system_id: int,
) -> None:
    for route in routes:
        transit_buy_route = route_lookup.get((current_system_id, route.buy_system_id))
        transit_sell_route = route_lookup.get((current_system_id, route.sell_system_id))
        route.transit_to_buy = _best_item_from_route(transit_buy_route)
        route.transit_to_sell = _best_item_from_route(transit_sell_route)


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


def apply_route_filters(
    routes: Sequence[RouteOpportunity],
    *,
    min_profit_per_jump: Optional[float],
    max_total_jumps: Optional[int],
) -> Tuple[List[RouteOpportunity], List[str]]:
    """Apply filters and return (filtered_routes, blocking_reasons)."""
    filtered = filter_routes_by_profit_per_jump(
        routes,
        min_profit_per_jump=min_profit_per_jump,
        max_total_jumps=max_total_jumps,
    )
    reasons: List[str] = []
    if routes and not filtered:
        reasons = describe_active_route_filters(
            min_profit_per_jump=min_profit_per_jump,
            max_total_jumps=max_total_jumps,
        )
    return filtered, reasons


def _format_item_details(item: ItemOpportunity) -> str:
    return (
        f"{item.item_name} x{item.tradable_units} @ {item.buy_from.price:,.2f} -> {item.sell_to.price:,.2f}"
        f" | profit {item.total_profit:,.2f} ( {item.multiplier:.2f}x )"
    )


def _append_leg_section(
    lines: List[str],
    *,
    label: str,
    opportunities: Sequence[ItemOpportunity],
    empty_message: str,
) -> None:
    lines.append(f"    {label}")
    if not opportunities:
        lines.append(f"        {empty_message}")
        return
    for item in opportunities:
        lines.append("        " + _format_item_details(item))


def _format_system_label(system_name: str, region_name: Optional[str]) -> str:
    if region_name:
        return f"{system_name} ({region_name})"
    return system_name


def render_route_report(
    routes: Sequence[RouteOpportunity],
    *,
    top_n: int,
    current_system_name: str,
    current_system_region: Optional[str],
) -> str:
    lines: List[str] = []
    if not routes:
        return "No qualifying arbitrage opportunities yet."
    current_label = _format_system_label(current_system_name, current_system_region)
    for route in routes[:top_n]:
        jumps_total = route.total_jumps
        jumps_display = "n/a" if jumps_total is None else str(jumps_total)
        isk_per_jump = route.isk_per_jump
        isk_per_jump_display = None if isk_per_jump is None else f"{isk_per_jump:,.2f}"
        profit_display = f"{route.total_profit:,.2f}"
        investment_display = f"{route.total_investment:,.2f}"
        volume_display = f"{route.total_volume:,.2f}"
        buy_label = _format_system_label(route.buy_system_name, route.buy_region)
        sell_label = _format_system_label(route.sell_system_name, route.sell_region)
        path_label = f"{current_label} -> {buy_label} -> {sell_label}"
        jump_component = f"jumps {jumps_display}"
        isk_per_jump_component = (
            f"i/j $ {isk_per_jump_display}" if isk_per_jump_display is not None else "i/j n/a"
        )
        lines.append(
            f"{path_label} | in. $ {investment_display} | p. $ {profit_display} | "
            f"vol. {volume_display} m3 | {jump_component} | {isk_per_jump_component}"
        )
        transit_to_buy = [route.transit_to_buy] if route.transit_to_buy else []
        if transit_to_buy and current_label != buy_label:
            _append_leg_section(
                lines,
                label=f"{current_label} -> {buy_label}",
                opportunities=transit_to_buy,
                empty_message="no transit opportunity",
            )
        transit_to_sell = [route.transit_to_sell] if route.transit_to_sell else []
        if transit_to_sell and current_label != sell_label and buy_label != current_label:
            _append_leg_section(
                lines,
                label=f"{current_label} -> {sell_label}",
                opportunities=transit_to_sell,
                empty_message="no transit opportunity",
            )
        _append_leg_section(
            lines,
            label=f"{buy_label} -> {sell_label}",
            opportunities=route.items,
            empty_message="no trade opportunities",
        )
        lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enumerate arbitrage opportunities using cached market orders and a JSON profile.",
    )
    parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Path to the JSON profile describing scan parameters.",
    )
    parser.add_argument(
        "--orders-file",
        type=Path,
        default=DEFAULT_ORDERS_SNAPSHOT,
        help="Path to a cached orders snapshot produced by fetch_market_orders.py.",
    )
    return parser.parse_args()


def main() -> None:
    cli_args = parse_args()
    try:
        profile = load_profile_config(cli_args.profile)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load profile {cli_args.profile}: {exc}", file=sys.stderr)
        sys.exit(1)

    if not profile.current_system:
        print(
            "Profile must include a 'current_system' value so jump distances can be computed.",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        orders, snapshot_meta = load_orders_snapshot(cli_args.orders_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load orders snapshot {cli_args.orders_file}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        items_payload = load_json_list(profile.items)
        regions_payload = load_json_list(profile.regions)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Failed to load prerequisite data: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        name_to_id, id_to_name, system_regions = load_system_indexes(Path(profile.systems))
    except (OSError, ValueError) as exc:
        print(f"Failed to load system metadata: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        current_system_id = resolve_system_identifier(profile.current_system, name_to_id, id_to_name)
    except ValueError as exc:
        print(f"Invalid --current-system: {exc}", file=sys.stderr)
        sys.exit(1)
    current_system_name = id_to_name[current_system_id]

    item_lookup = build_item_lookup(items_payload, limit=profile.item_limit)
    if not item_lookup:
        print("No published items available after applying filters", file=sys.stderr)
        sys.exit(1)

    regions = build_regions(regions_payload)
    region_name_lookup = {region.region_id: region.name for region in regions}
    current_system_region_name = None
    current_region_id = system_regions.get(current_system_id)
    if current_region_id is not None:
        current_system_region_name = region_name_lookup.get(current_region_id)

    resolver = SystemResolver(id_to_name)
    jump_cache = LocalJumpCache(flag=profile.route_flag, graph_path=profile.system_graph)
    system_security_lookup: Dict[int, float] = {}
    if profile.min_security_status is not None:
        try:
            system_security_lookup = load_system_security(Path(profile.systems))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"Failed to load system security data: {exc}", file=sys.stderr)
            sys.exit(1)

    tracker = ArbitrageTracker(item_lookup)
    skipped_player_orders = 0
    skipped_regions = 0
    skipped_security_orders = 0
    applicable_orders = orders
    if profile.forge_adjacent_only:
        applicable_orders = [order for order in orders if order.region_id in FORGE_ADJACENT_REGION_IDS]
        skipped_regions = len(orders) - len(applicable_orders)
        if not applicable_orders:
            print(
                "Forge adjacency filter removed all orders; rerun fetch_market_orders with matching regions",
                file=sys.stderr,
            )
            sys.exit(1)
    for order in applicable_orders:
        if profile.min_security_status is not None:
            security = system_security_lookup.get(order.system_id)
            if security is None or security < profile.min_security_status:
                skipped_security_orders += 1
                continue
        if is_player_structure(order.location_id):
            skipped_player_orders += 1
            continue
        tracker.ingest(order)

    print(
        f"Loaded {len(applicable_orders):,} orders from {cli_args.orders_file} (snapshot: {snapshot_meta.get('fetched_at', 'unknown')}),"
    )
    if profile.forge_adjacent_only:
        print(
            f"Forge adjacency filter active: {len(applicable_orders):,} kept, {skipped_regions:,} discarded."
        )
    if skipped_player_orders:
        print(f"Skipped {skipped_player_orders:,} player-structure orders (non-dockable).")
    if skipped_security_orders:
        print(
            f"Skipped {skipped_security_orders:,} orders below min security ({profile.min_security_status})."
        )

    partial_top = max(0, profile.partial_top)
    partial_interval_seconds = 10.0
    partial_best: List[ItemOpportunity] = []
    last_partial_emit_count = 0
    last_partial_emit_time = time.monotonic()

    def _clear_console() -> None:
        command = "cls" if os.name == "nt" else "clear"
        os.system(command)

    def _emit_partial(count: int) -> None:
        if not partial_best:
            return
        _clear_console()
        partial_routes_raw = group_opportunities(
            partial_best,
            resolver=resolver,
            jump_cache=jump_cache,
            current_system_id=current_system_id,
            max_total_volume=profile.max_volume,
        )
        if not partial_routes_raw:
            return
        route_lookup = {
            (route.buy_system_id, route.sell_system_id): route for route in partial_routes_raw
        }
        display_routes, blocking_reasons = apply_route_filters(
            partial_routes_raw,
            min_profit_per_jump=profile.min_profit_per_jump,
            max_total_jumps=profile.max_total_jumps,
        )
        if not display_routes:
            reason_label = " and ".join(blocking_reasons) if blocking_reasons else "active filters"
            print(
                f"[partial] {count:,} opportunities so far but none satisfy {reason_label} yet.",
                flush=True,
            )
            print("", flush=True)
            return
        annotate_transit_opportunities(
            display_routes,
            route_lookup=route_lookup,
            current_system_id=current_system_id,
        )
        report = render_route_report(
            display_routes,
            top_n=partial_top,
            current_system_name=current_system_name,
            current_system_region=current_system_region_name,
        )
        visible = min(partial_top, len(display_routes))
        print(
            f"[partial] {count:,} qualifying opportunities so far. Best {visible} routes:",
            flush=True,
        )
        print(report, flush=True)
        print("", flush=True)

    def _on_progress(count: int, new_item: ItemOpportunity) -> None:
        nonlocal last_partial_emit_count, last_partial_emit_time
        partial_best.append(new_item)
        partial_best.sort(key=lambda entry: (entry.total_profit, entry.multiplier), reverse=True)
        if len(partial_best) > partial_top:
            del partial_best[partial_top:]
        now = time.monotonic()
        if now - last_partial_emit_time >= partial_interval_seconds:
            _emit_partial(count)
            last_partial_emit_time = time.monotonic()
            last_partial_emit_count = count

    progress_callback: Optional[Callable[[int, ItemOpportunity], None]] = None
    if partial_top > 0:
        progress_callback = _on_progress

    opportunities = tracker.opportunities(
        min_multiplier=profile.min_multiplier,
        max_investment=profile.max_investment,
        max_volume=profile.max_volume,
        max_item_volume=profile.max_item_volume,
        max_units_cap=profile.max_units,
        max_items_cap=profile.max_items,
        progress_callback=progress_callback,
    )
    if partial_top > 0 and partial_best and last_partial_emit_count != len(opportunities):
        _emit_partial(len(opportunities))
    if not opportunities:
        print("No item-level opportunities matched the provided filters.")
        sys.exit(0)

    routes_raw = group_opportunities(
        opportunities,
        resolver=resolver,
        jump_cache=jump_cache,
        current_system_id=current_system_id,
        max_total_volume=profile.max_volume,
    )
    route_lookup = {
        (route.buy_system_id, route.sell_system_id): route for route in routes_raw
    }
    routes, blocking_reasons = apply_route_filters(
        routes_raw,
        min_profit_per_jump=profile.min_profit_per_jump,
        max_total_jumps=profile.max_total_jumps,
    )
    annotate_transit_opportunities(
        routes,
        route_lookup=route_lookup,
        current_system_id=current_system_id,
    )
    if not routes and routes_raw and blocking_reasons:
        print(
            f"All {len(routes_raw)} routes were filtered out by "
            + " and ".join(blocking_reasons)
            + ". Adjust the thresholds to inspect them."
        )
    final_report = render_route_report(
        routes,
        top_n=profile.top,
        current_system_name=current_system_name,
        current_system_region=current_system_region_name,
    )
    print(final_report)
    jump_cache.flush()
    try:
        profile.final_output.parent.mkdir(parents=True, exist_ok=True)
        profile.final_output.write_text(final_report.rstrip() + "\n", encoding="utf-8")
        print(f"Final snapshot saved to {profile.final_output}")
    except OSError as exc:
        print(f"Warning: failed to write final snapshot to {profile.final_output}: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
