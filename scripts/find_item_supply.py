"""CLI helper to surface nearby sell orders for a specific EVE item."""

from __future__ import annotations

import argparse
import json
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence


@dataclass
class OrderView:
	location_id: int
	station_name: str
	system_name: str
	jumps: int
	price: float
	volume: int
	order_count: int = 1


def load_json(path: Path):
	with path.open("r", encoding="utf-8") as handle:
		return json.load(handle)


def resolve_item(items: Sequence[Mapping], name: str) -> Mapping:
	target = name.strip().casefold()
	exact = next(
		(item for item in items if item["name"].strip().casefold() == target),
		None,
	)
	if exact:
		return exact

	candidates = [
		item for item in items if target in item["name"].strip().casefold()
	]
	if not candidates:
		raise SystemExit(f"Item '{name}' not found in items.json")
	if len(candidates) > 1:
		sample = ", ".join(item["name"].strip() for item in candidates[:5])
		extra = "" if len(candidates) <= 5 else " ..."
		raise SystemExit(
			"Item name is ambiguous. Matching entries: " + sample + extra
		)
	return candidates[0]


def build_station_lookup(stations: Sequence[Mapping]) -> Dict[int, Mapping]:
	return {int(station["id"]): station for station in stations}


def build_system_lookups(
	systems: Sequence[Mapping],
) -> tuple[Dict[int, Mapping], Dict[str, int]]:
	by_id = {int(system["id"]): system for system in systems}
	by_name = {system["name"].casefold(): int(system["id"]) for system in systems}
	return by_id, by_name


def resolve_system_id(identifier: str, by_name: Mapping[str, int]) -> int:
	identifier = identifier.strip()
	if identifier.isdigit():
		return int(identifier)
	lowered = identifier.casefold()
	if lowered in by_name:
		return by_name[lowered]
	raise SystemExit(f"System '{identifier}' not found in systems.json")


def build_graph(graph_data: Mapping[str, Iterable[int]]) -> Dict[int, List[int]]:
	return {int(system_id): [int(n) for n in neighbors]
			for system_id, neighbors in graph_data.items()}


def compute_jump_distances(graph: Mapping[int, Iterable[int]], origin: int) -> Dict[int, int]:
	distances: Dict[int, int] = {origin: 0}
	queue: deque[int] = deque([origin])
	while queue:
		current = queue.popleft()
		for neighbor in graph.get(current, ()):  # neighbors missing only on dead-end systems
			if neighbor in distances:
				continue
			distances[neighbor] = distances[current] + 1
			queue.append(neighbor)
	return distances


def orders_for_item(orders: Sequence[Mapping], type_id: int) -> List[Mapping]:
	return [order for order in orders if order["type_id"] == type_id and not order["is_buy_order"]]


def summarize_orders(
	orders: Sequence[Mapping],
	stations_by_id: Mapping[int, Mapping],
	systems_by_id: Mapping[int, Mapping],
	jump_distances: Mapping[int, int],
) -> List[OrderView]:
	views: List[OrderView] = []
	for order in orders:
		system_id = int(order["system_id"])
		jumps = jump_distances.get(system_id)
		if jumps is None:
			continue  # unreachable system

		location_id = int(order["location_id"])
		station = stations_by_id.get(location_id)
		station_name = (
			station["name"].strip()
			if station
			else f"Structure {location_id}"
		)

		system = systems_by_id.get(system_id)
		system_name = system["name"] if system else str(system_id)

		views.append(
			OrderView(
				location_id=location_id,
				station_name=station_name,
				system_name=system_name,
				jumps=jumps,
				price=float(order["price"]),
				volume=int(order["volume_remain"]),
			)
		)
	return views


def format_table(rows: Sequence[OrderView]) -> str:
	headers = ["Jumps", "Price (ISK)", "Volume", "Orders", "System", "Station"]
	table_data = [
		[
			str(row.jumps),
			f"{row.price:,.2f}",
			str(row.volume),
			str(row.order_count),
			row.system_name,
			row.station_name,
		]
		for row in rows
	]
	widths = [
		max(len(header), *(len(row[idx]) for row in table_data))
		for idx, header in enumerate(headers)
	]
	lines = [
		" ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers)),
		" ".join("-" * widths[idx] for idx in range(len(headers))),
	]
	for row in table_data:
		lines.append(
			" ".join(row[idx].ljust(widths[idx]) for idx in range(len(headers)))
		)
	return "\n".join(lines)


def bucket_price(value: float, step: float) -> float:
	if step <= 0:
		return value
	return math.floor(value / step) * step


def aggregate_by_price_bucket(rows: Sequence[OrderView], step: float) -> List[OrderView]:
	if step <= 0:
		return list(rows)

	grouped: Dict[tuple[int, float], OrderView] = {}
	for row in rows:
		bucket = bucket_price(row.price, step)
		key = (row.location_id, bucket)
		if key not in grouped:
			grouped[key] = OrderView(
				location_id=row.location_id,
				station_name=row.station_name,
				system_name=row.system_name,
				jumps=row.jumps,
				price=bucket,
				volume=row.volume,
				order_count=row.order_count,
			)
			continue

		existing = grouped[key]
		existing.volume += row.volume
		existing.order_count += row.order_count

	return list(grouped.values())


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description=(
			"Locate nearby sell orders for a given item, sorted by jump distance then price."
		)
	)
	parser.add_argument("--item", "-i", required=True, help="Item name to search for")
	parser.add_argument(
		"--origin",
		"-o",
		required=True,
		help="Origin system name or ID that jump counts will be computed from",
	)
	parser.add_argument(
		"--orders-path",
		default="data/market_orders_snapshot.json",
		type=Path,
		help="Path to the market orders snapshot JSON",
	)
	parser.add_argument(
		"--items-path",
		default="data/items.json",
		type=Path,
		help="Path to the SDE items JSON",
	)
	parser.add_argument(
		"--stations-path",
		default="data/stations.json",
		type=Path,
		help="Path to the SDE stations JSON",
	)
	parser.add_argument(
		"--systems-path",
		default="data/systems.json",
		type=Path,
		help="Path to the SDE systems JSON",
	)
	parser.add_argument(
		"--graph-path",
		default="data/system_graph.json",
		type=Path,
		help="Path to the pre-built system graph JSON",
	)
	parser.add_argument(
		"--limit",
		type=int,
		default=25,
		help="Maximum number of rows to display",
	)
	parser.add_argument(
		"--price-step",
		type=float,
		default=1.0,
		help=(
			"Bucket size (in ISK) for grouping sell orders. Set to 0 to disable grouping."
		),
	)
	return parser.parse_args()


def main() -> None:
	args = parse_args()

	items = load_json(args.items_path)
	item = resolve_item(items, args.item)
	type_id = int(item["id"])

	orders_snapshot = load_json(args.orders_path)
	orders = orders_snapshot.get("orders", [])
	if not orders:
		raise SystemExit("No orders present in the provided snapshot")

	stations = load_json(args.stations_path)
	systems = load_json(args.systems_path)
	graph = load_json(args.graph_path)["systems"]

	systems_by_id, systems_by_name = build_system_lookups(systems)
	origin_id = resolve_system_id(args.origin, systems_by_name)

	adjacency = build_graph(graph)
	if origin_id not in adjacency:
		raise SystemExit(f"Origin system {origin_id} is missing from the system graph")
	jump_distances = compute_jump_distances(adjacency, origin_id)

	matching_orders = orders_for_item(orders, type_id)
	if not matching_orders:
		raise SystemExit(f"No sell orders found for {item['name']}")

	station_lookup = build_station_lookup(stations)
	views = summarize_orders(
		matching_orders,
		station_lookup,
		systems_by_id,
		jump_distances,
	)

	if not views:
		raise SystemExit("No reachable sell orders for the requested item")

	views = aggregate_by_price_bucket(views, args.price_step)
	views.sort(key=lambda view: (view.price, view.jumps))
	limited = views[: args.limit]

	print(f"Item: {item['name']} (type_id {type_id})")
	origin_name = systems_by_id.get(origin_id, {}).get("name", str(origin_id))
	print(f"Origin: {origin_name} ({origin_id})")
	print(format_table(limited))


if __name__ == "__main__":
	main()
