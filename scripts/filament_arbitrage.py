"""Evaluate arbitrage routes that leverage filament travel assumptions."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from calc_jumps_local import LocalJumpCache, load_system_indexes, resolve_system_identifier
from item_arbitrage import (
    ArbitrageTracker,
    DEFAULT_ORDERS_SNAPSHOT,
    Order,
    RouteOpportunity,
    SystemResolver,
    build_item_lookup,
    build_regions,
    build_station_lookup,
    group_opportunities,
    is_player_structure,
    load_json_list,
    load_orders_snapshot,
    load_profile_config,
    load_system_security,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Plan arbitrage runs that start from a user-provided null-sec system,"
            " move to nearby sellers, and then assume filament hops back to Jita."
        )
    )
    parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Path to the existing arbitrage JSON profile for volume/investment constraints.",
    )
    parser.add_argument(
        "--orders-file",
        type=Path,
        default=DEFAULT_ORDERS_SNAPSHOT,
        help="Path to cached market orders (default: data/market_orders_snapshot.json).",
    )
    parser.add_argument(
        "--starting-system",
        required=True,
        help="Null-sec system name or ID where you filament into and begin buying items.",
    )
    parser.add_argument(
        "--jita-system",
        default="Jita",
        help="Destination system name or ID (default: Jita).",
    )
    parser.add_argument(
        "--return-jumps",
        type=int,
        default=10,
        help=(
            "Approximate jump count from the buy location back to Jita using filament chains "
            "(default: 10)."
        ),
    )
    parser.add_argument(
        "--max-buy-jumps",
        type=int,
        default=30,
        help="Maximum gate jumps you are willing to travel from the starting system to buy items.",
    )
    parser.add_argument(
        "--top-routes",
        type=int,
        default=3,
        help="How many ranked routes to display (default: 3).",
    )
    return parser.parse_args()


def format_system_label(system: str, region: Optional[str]) -> str:
    if region:
        return f"{system} ({region})"
    return system


def format_jumps(value: Optional[int]) -> str:
    if value is None:
        return "n/a"
    return f"{value}"


def compute_filament_profit_per_jump(route: RouteOpportunity, return_jumps: int) -> Optional[float]:
    if route.jumps_from_current is None:
        return None
    total_jumps = route.jumps_from_current + max(return_jumps, 0)
    if total_jumps <= 0:
        total_jumps = 1
    return route.total_profit / total_jumps


def annotate_filament_metrics(
    routes: Sequence[RouteOpportunity], *, return_jumps: int
) -> None:
    for route in routes:
        route.jumps_trade = max(0, return_jumps)
        profit_per_jump = compute_filament_profit_per_jump(route, return_jumps)
        setattr(route, "filament_profit_per_jump", profit_per_jump)


def filter_routes_for_filaments(
    routes: Sequence[RouteOpportunity],
    *,
    return_jumps: int,
    min_profit_per_jump: Optional[float],
    max_total_jumps: Optional[int],
) -> List[RouteOpportunity]:
    filtered: List[RouteOpportunity] = []
    for route in routes:
        if route.jumps_from_current is None:
            continue
        profit_per_jump = getattr(route, "filament_profit_per_jump", None)
        if min_profit_per_jump is not None:
            if profit_per_jump is None or profit_per_jump < min_profit_per_jump:
                continue
        total_jumps = route.jumps_from_current + max(0, return_jumps)
        if max_total_jumps is not None and max_total_jumps >= 0:
            if total_jumps > max_total_jumps:
                continue
        filtered.append(route)
    return filtered


def render_routes(
    routes: Sequence[RouteOpportunity],
    *,
    start_label: str,
    dest_label: str,
    return_jumps: int,
    limit: int,
) -> str:
    if not routes:
        return "No qualifying filament arbitrage routes found."

    sections: List[str] = []
    trimmed_limit = max(1, limit)
    digits = len(str(min(len(routes), trimmed_limit)))
    for index, route in enumerate(routes[:trimmed_limit], start=1):
        profit_per_jump = getattr(route, "filament_profit_per_jump", None)
        buy_label = format_system_label(route.buy_system_name, route.buy_region)
        station_segment = f" @ {route.buy_station_name}" if route.buy_station_name else ""
        lines = [
            f"Route #{index:0{digits}d}: {buy_label}{station_segment} -> {dest_label}",
            f"  Gate distance from start: {format_jumps(route.jumps_from_current)} jumps",
            f"  Assumed filament return: {return_jumps} jumps",
            (
                f"  Profit per jump: {profit_per_jump:,.2f} ISK"
                if profit_per_jump is not None
                else "  Profit per jump: n/a (no gate path to buy location)"
            ),
            (
                "  Totals: profit $ {route.total_profit:,.2f} | "
                f"investment $ {route.total_investment:,.2f} | volume {route.total_volume:,.2f} m3"
            ),
            "  Items:",
        ]
        for item in route.items:
            lines.append(
                "    - {name} x{units:,} | buy @ {buy:,.2f} | sell @ {sell:,.2f}".format(
                    name=item.item_name,
                    units=item.tradable_units,
                    buy=item.buy_from.price,
                    sell=item.sell_to.price,
                )
            )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def load_station_names(stations_path: Optional[Path]) -> Dict[int, str]:
    if stations_path is None:
        return {}
    try:
        payload = load_json_list(stations_path)
    except FileNotFoundError:
        print(
            f"Station metadata {stations_path} missing; station names will be omitted.",
            file=sys.stderr,
        )
        return {}
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Failed to load station metadata: {exc}", file=sys.stderr)
        return {}
    return build_station_lookup(payload)


def main() -> None:
    args = parse_args()
    try:
        profile = load_profile_config(args.profile)
    except (OSError, ValueError) as exc:
        print(f"Failed to load profile {args.profile}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        orders, snapshot_meta = load_orders_snapshot(args.orders_file)
    except (OSError, ValueError) as exc:
        print(f"Failed to load orders {args.orders_file}: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        items_payload = load_json_list(profile.items)
        regions_payload = load_json_list(profile.regions)
    except (OSError, ValueError) as exc:
        print(f"Failed to load prerequisite data: {exc}", file=sys.stderr)
        sys.exit(1)

    station_lookup = load_station_names(profile.stations_file)

    try:
        name_to_id, id_to_name, system_regions = load_system_indexes(Path(profile.systems))
    except (OSError, ValueError) as exc:
        print(f"Failed to load system metadata: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        start_system_id = resolve_system_identifier(args.starting_system, name_to_id, id_to_name)
    except ValueError as exc:
        print(f"Invalid starting system '{args.starting_system}': {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        jita_system_id = resolve_system_identifier(args.jita_system, name_to_id, id_to_name)
    except ValueError as exc:
        print(f"Invalid destination '{args.jita_system}': {exc}", file=sys.stderr)
        sys.exit(1)

    region_lookup = {region.region_id: region.name for region in build_regions(regions_payload)}
    start_region = region_lookup.get(system_regions.get(start_system_id))
    dest_region = region_lookup.get(system_regions.get(jita_system_id))
    start_label = format_system_label(id_to_name[start_system_id], start_region)
    dest_label = format_system_label(id_to_name[jita_system_id], dest_region)

    jump_cache = LocalJumpCache(flag=profile.route_flag, graph_path=profile.system_graph)

    system_security_lookup: Dict[int, float] = {}
    if profile.min_security_status is not None:
        try:
            system_security_lookup = load_system_security(Path(profile.systems))
        except (OSError, ValueError) as exc:
            print(f"Failed to load system security data: {exc}", file=sys.stderr)
            sys.exit(1)

    if station_lookup:
        for order in orders:
            order.location_name = station_lookup.get(order.location_id)

    item_lookup = build_item_lookup(items_payload, limit=profile.item_limit)
    if not item_lookup:
        print("No published items available after applying profile filters.", file=sys.stderr)
        sys.exit(1)

    tracker = ArbitrageTracker(item_lookup)
    filtered_orders: List[Order] = []
    skipped_player = skipped_security = skipped_distance = skipped_destination = 0
    max_buy_jumps = max(0, args.max_buy_jumps)
    for order in orders:
        if is_player_structure(order.location_id):
            skipped_player += 1
            continue
        if profile.min_security_status is not None:
            security = system_security_lookup.get(order.system_id)
            if security is None or security < profile.min_security_status:
                skipped_security += 1
                continue
        if order.is_buy_order:
            if order.system_id != jita_system_id:
                skipped_destination += 1
                continue
        else:
            jumps = jump_cache.jumps(start_system_id, order.system_id)
            if jumps is None or jumps > max_buy_jumps:
                skipped_distance += 1
                continue
        filtered_orders.append(order)
        tracker.ingest(order)

    if not filtered_orders:
        print("No orders matched the filament filters (distance, destination, or security).")
        sys.exit(0)

    print(
        f"Loaded {len(filtered_orders):,} usable orders after filtering from snapshot fetched at "
        f"{snapshot_meta.get('fetched_at', 'unknown')}."
    )
    if skipped_player:
        print(f"Skipped {skipped_player:,} player-structure orders (not dockable).")
    if skipped_security:
        print(
            f"Skipped {skipped_security:,} orders below min security "
            f"({profile.min_security_status})."
        )
    if skipped_destination:
        print(
            f"Skipped {skipped_destination:,} buy orders outside {dest_label} when searching for buyers."
        )
    if skipped_distance:
        print(
            f"Skipped {skipped_distance:,} sell orders beyond {max_buy_jumps} jumps from the starting system."
        )

    opportunities = tracker.opportunities(
        min_multiplier=profile.min_multiplier,
        max_investment=profile.max_investment,
        max_volume=profile.max_volume,
        max_item_volume=profile.max_item_volume,
        max_units_cap=profile.max_units,
        max_items_cap=profile.max_items,
        broker_fee_rate=profile.broker_fee_rate,
    )
    if not opportunities:
        print("No item-level opportunities satisfied the configured filters.")
        sys.exit(0)

    resolver = SystemResolver(id_to_name)
    routes = group_opportunities(
        opportunities,
        resolver=resolver,
        jump_cache=jump_cache,
        current_system_id=start_system_id,
        max_total_volume=profile.max_volume,
    )

    annotate_filament_metrics(routes, return_jumps=args.return_jumps)
    routes = filter_routes_for_filaments(
        routes,
        return_jumps=args.return_jumps,
        min_profit_per_jump=profile.min_profit_per_jump,
        max_total_jumps=profile.max_total_jumps,
    )
    if not routes:
        print(
            "No grouped routes passed the filament profit/jump or total jump constraints.",
            file=sys.stderr,
        )
        sys.exit(0)

    def _sort_key(route: RouteOpportunity) -> tuple[float, float]:
        metric = getattr(route, "filament_profit_per_jump", None)
        normalized = metric if metric is not None else float("-inf")
        return (normalized, route.total_profit)

    routes.sort(key=_sort_key, reverse=True)

    report = render_routes(
        routes,
        start_label=start_label,
        dest_label=dest_label,
        return_jumps=args.return_jumps,
        limit=args.top_routes,
    )
    print("".join(
        [
            f"Starting system: {start_label}\n",
            f"Destination: {dest_label}\n",
            (
                "Assuming {0} jumps via filaments from buy location back to Jita before selling.\n\n".format(
                    max(0, args.return_jumps)
                )
            ),
            report,
        ]
    ))


if __name__ == "__main__":
    main()
