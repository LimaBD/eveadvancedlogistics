"""Identify suspiciously underpriced sell orders in a cached market snapshot."""
from __future__ import annotations

import argparse
import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

DEFAULT_ORDERS_PATH = Path("data/market_orders_snapshot.json")
DEFAULT_ITEMS_PATH = Path("data/items.json")
DEFAULT_STATIONS_PATH = Path("data/stations.json")


@dataclass
class SuspiciousOrder:
    """Container describing a single outlier sell order."""

    order_id: int
    type_id: int
    price: float
    normal_price: float
    multiplier: float
    region_name: str
    system_id: int
    location_id: int
    volume_remain: int
    min_volume: int
    investment: float
    profit_normal: float
    location_name: Optional[str] = None
    item_name: Optional[str] = None
    best_buy_price: Optional[float] = None
    buy_multiplier: Optional[float] = None
    next_sell_price: Optional[float] = None
    gap_multiplier: Optional[float] = None
    profit_best_buy: Optional[float] = None

    @property
    def total_price(self) -> float:
        return self.price * self.volume_remain

    @property
    def total_normal_value(self) -> float:
        return self.normal_price * self.volume_remain


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Flag sell orders that are dramatically cheaper than the normal price, "
            "the best live buy orders, and the next-Amarr Loyalty Token highest sell listing."
        )
    )
    parser.add_argument(
        "--orders-path",
        type=Path,
        default=DEFAULT_ORDERS_PATH,
        help="Path to the cached market orders snapshot JSON.",
    )
    parser.add_argument(
        "--items-path",
        type=Path,
        default=DEFAULT_ITEMS_PATH,
        help="Path to SDE items JSON for resolving item names.",
    )
    parser.add_argument(
        "--stations-path",
        type=Path,
        default=DEFAULT_STATIONS_PATH,
        help="Path to SDE stations JSON for structure names.",
    )
    parser.add_argument(
        "--min-multiplier",
        type=float,
        default=2.0,
        help="Minimum discount ratio (normal_price / order_price) to flag an order.",
    )
    parser.add_argument(
        "--min-buy-multiplier",
        type=float,
        help=(
            "Minimum best-buy/price ratio required to flag an order. Defaults to --min-multiplier."
        ),
    )
    parser.add_argument(
        "--min-gap-multiplier",
        type=float,
        default=2.0,
        help=(
            "Minimum ratio between the next highest sell order and the flagged order."
            " Set to 0 to disable this check."
        ),
    )
    parser.add_argument(
        "--min-investment",
        type=float,
        default=0.0,
        help="Minimum ISK required to buy the entire order (price * volume).",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=0.0,
        help=(
            "Minimum estimated profit (best buy if available, otherwise median) required for an order."
        ),
    )
    parser.add_argument(
        "--min-normal-price",
        type=float,
        default=500_000.0,
        help="Ignore items whose normal (median) price is below this threshold.",
    )
    parser.add_argument(
        "--min-normal-value",
        type=float,
        default=0.0,
        help="Minimum per-order value at normal pricing (normal_price * volume).",
    )
    parser.add_argument(
        "--min-volume",
        type=int,
        default=1,
        help="Ignore orders that are thinner than this many units remaining.",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=5,
        help="Minimum number of sell orders needed to estimate a normal price.",
    )
    parser.add_argument(
        "--trim-ratio",
        type=float,
        default=0.1,
        help=(
            "Fraction (0-0.45) of prices trimmed from each tail before computing the median."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum number of flagged orders to print (sorted by multiplier).",
    )
    parser.add_argument(
        "--sort-by",
        choices=("multiplier", "profit"),
        default="multiplier",
        help="Primary sort key for the final list (multiplier or total profit).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional file to write the final report to (same format as stdout).",
    )
    parser.add_argument(
        "--format",
        choices=("table", "json"),
        default="table",
        help="Select human-readable table output or machine-consumable JSON.",
    )
    parser.add_argument(
        "--skip-item-names",
        action="store_true",
        help="Do not load items.json (useful when only type IDs are needed).",
    )
    return parser.parse_args()


def load_orders(path: Path) -> List[MutableMapping]:
    if not path.exists():
        raise FileNotFoundError(f"Orders snapshot {path} not found")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if isinstance(payload, Mapping):
        orders = payload.get("orders")
        if orders is None:
            data_section = payload.get("data")
            if isinstance(data_section, Mapping):
                orders = data_section.get("orders")
        if orders is None:
            raise ValueError(f"Orders snapshot {path} does not contain an 'orders' array")
        return list(orders)
    if isinstance(payload, list):
        return payload
    raise ValueError(f"Unsupported snapshot format in {path}")


def collect_market_stats(
    orders: Iterable[Mapping],
) -> Tuple[Dict[int, List[float]], Dict[int, float], Dict[int, float]]:
    sell_samples: Dict[int, List[float]] = {}
    best_buy_prices: Dict[int, float] = {}
    sell_gap_prices: Dict[int, Tuple[Optional[float], Optional[float]]] = {}
    for order in orders:
        price = float(order.get("price", 0.0))
        if price <= 0:
            continue
        type_id = int(order.get("type_id"))
        if order.get("is_buy_order"):
            current_best = best_buy_prices.get(type_id)
            if current_best is None or price > current_best:
                best_buy_prices[type_id] = price
            continue
        sell_samples.setdefault(type_id, []).append(price)
        best_price, next_price = sell_gap_prices.get(type_id, (None, None))
        if best_price is None or price < best_price:
            if best_price is not None and price != best_price:
                next_price = best_price
            best_price = price
        elif price > best_price and (next_price is None or price < next_price):
            next_price = price
        sell_gap_prices[type_id] = (best_price, next_price)
    next_sell_lookup: Dict[int, float] = {
        type_id: next_price
        for type_id, (best_price, next_price) in sell_gap_prices.items()
        if next_price is not None
    }
    return sell_samples, best_buy_prices, next_sell_lookup


def trimmed_median(values: List[float], trim_ratio: float) -> float:
    if not values:
        raise ValueError("Cannot compute median of empty list")
    ordered = sorted(values)
    if trim_ratio <= 0:
        return statistics.median(ordered)
    trim_ratio = min(max(trim_ratio, 0.0), 0.45)
    trim = int(len(ordered) * trim_ratio)
    if trim == 0:
        return statistics.median(ordered)
    trimmed = ordered[trim: len(ordered) - trim]
    if not trimmed:
        trimmed = ordered
    return statistics.median(trimmed)


def compute_normal_prices(
    samples: Mapping[int, List[float]],
    *,
    min_samples: int,
    trim_ratio: float,
) -> Dict[int, float]:
    normals: Dict[int, float] = {}
    min_samples = max(1, min_samples)
    for type_id, prices in samples.items():
        if len(prices) < min_samples:
            continue
        normals[type_id] = trimmed_median(prices, trim_ratio)
    return normals


def load_station_lookup(path: Optional[Path]) -> Dict[int, str]:
    if not path:
        return {}
    path = path.expanduser()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Stations file {path} must be a list")
    lookup: Dict[int, str] = {}
    for entry in payload:
        try:
            station_id = int(entry["id"])
            name = str(entry.get("name", "")).strip()
        except (KeyError, ValueError) as exc:
            raise ValueError(f"Invalid station row: {entry}") from exc
        if name:
            lookup[station_id] = name
    return lookup


def resolve_item_names(
    path: Optional[Path],
    type_ids: Iterable[int],
) -> Dict[int, str]:
    if not path:
        return {}
    needed = {int(type_id) for type_id in type_ids}
    if not needed:
        return {}
    path = path.expanduser()
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError(f"Items file {path} must be a list")
    lookup: Dict[int, str] = {}
    for entry in payload:
        try:
            type_id = int(entry["id"])
        except (KeyError, ValueError):
            continue
        if type_id not in needed:
            continue
        name = str(entry.get("name", "")).strip() or f"Type {type_id}"
        lookup[type_id] = name
        if len(lookup) == len(needed):
            break
    return lookup


def find_suspicious_orders(
    orders: Iterable[Mapping],
    normals: Mapping[int, float],
    *,
    min_multiplier: float,
    min_normal_price: float,
    min_normal_value: float,
    min_volume: int,
    station_lookup: Mapping[int, str],
    best_buy_prices: Mapping[int, float],
    next_sell_prices: Mapping[int, float],
    min_buy_multiplier: Optional[float],
    min_gap_multiplier: Optional[float],
    min_investment: float,
    min_profit: float,
    sort_by: str,
) -> List[SuspiciousOrder]:
    suspicious: List[SuspiciousOrder] = []
    min_multiplier = max(1.0, min_multiplier)
    min_volume = max(1, min_volume)
    min_investment = max(0.0, min_investment)
    min_profit = max(0.0, min_profit)
    buy_multiplier = None
    if min_buy_multiplier is not None:
        buy_multiplier = max(1.0, min_buy_multiplier)
    gap_multiplier = None
    if min_gap_multiplier is not None:
        gap_multiplier = max(1.0, min_gap_multiplier)
    for order in orders:
        if order.get("is_buy_order"):
            continue
        try:
            type_id = int(order["type_id"])
            normal_price = normals.get(type_id)
            if normal_price is None or normal_price < min_normal_price:
                continue
            price = float(order.get("price", 0.0))
            if price <= 0:
                continue
            ratio = normal_price / price if price else float("inf")
            if ratio < min_multiplier:
                continue
            best_buy_price = best_buy_prices.get(type_id)
            buy_ratio = None
            if best_buy_price and price > 0:
                buy_ratio = best_buy_price / price
            if buy_multiplier is not None:
                if buy_ratio is None or buy_ratio < buy_multiplier:
                    continue
            next_sell_price = next_sell_prices.get(type_id)
            gap_ratio = None
            if next_sell_price and price > 0 and next_sell_price > price:
                gap_ratio = next_sell_price / price
            if gap_multiplier is not None:
                if gap_ratio is None or gap_ratio < gap_multiplier:
                    continue
            volume = int(order.get("volume_remain", 0))
            if volume < min_volume:
                continue
            investment = price * volume
            if investment < min_investment:
                continue
            normal_value = normal_price * volume
            if normal_value < min_normal_value:
                continue
            profit_normal = (normal_price - price) * volume
            profit_best_buy = None
            if best_buy_price is not None:
                profit_best_buy = (best_buy_price - price) * volume
            available_profit = profit_best_buy if profit_best_buy is not None else profit_normal
            if available_profit < min_profit:
                continue
            location_id = int(order.get("location_id", 0))
            location_name = order.get("location_name")
            if not location_name:
                location_name = station_lookup.get(location_id) or f"Structure {location_id}"
            suspicious.append(
                SuspiciousOrder(
                    order_id=int(order.get("order_id", 0)),
                    type_id=type_id,
                    price=price,
                    normal_price=normal_price,
                    multiplier=ratio,
                    region_name=str(order.get("region_name") or f"Region {order.get('region_id')}").strip(),
                    system_id=int(order.get("system_id", 0)),
                    location_id=location_id,
                    volume_remain=volume,
                    min_volume=int(order.get("min_volume", 1)),
                    investment=investment,
                    profit_normal=profit_normal,
                    location_name=location_name,
                    best_buy_price=best_buy_price,
                    buy_multiplier=buy_ratio,
                    next_sell_price=next_sell_price,
                    gap_multiplier=gap_ratio,
                    profit_best_buy=profit_best_buy,
                )
            )
        except (KeyError, ValueError, TypeError):
            continue
    def sort_key(order: SuspiciousOrder) -> float:
        if sort_by == "profit":
            profit_value = order.profit_best_buy if order.profit_best_buy is not None else order.profit_normal
            return profit_value if profit_value is not None else 0.0
        return order.multiplier

    suspicious.sort(key=sort_key, reverse=True)
    return suspicious


def format_table(rows: Sequence[SuspiciousOrder]) -> str:
    headers = ["Item", "Region", "Investment", "Profit"]
    body = []
    for row in rows:
        item_label = row.item_name or f"Type {row.type_id}"
        profit_value = row.profit_best_buy if row.profit_best_buy is not None else row.profit_normal
        profit_label = f"{profit_value:,.2f}" if profit_value is not None else "-"
        body.append(
            [
                item_label,
                row.region_name,
                f"{row.investment:,.2f}",
                profit_label,
            ]
        )
    widths = [len(header) for header in headers]
    for row in body:
        for idx, value in enumerate(row):
            widths[idx] = max(widths[idx], len(value))
    lines = [
        " ".join(value.ljust(widths[idx]) for idx, value in enumerate(headers)),
        " ".join("-" * widths[idx] for idx in range(len(headers))),
    ]
    for row in body:
        lines.append(" ".join(value.ljust(widths[idx]) for idx, value in enumerate(row)))
    return "\n".join(lines)


def to_json(rows: Sequence[SuspiciousOrder]) -> List[Dict[str, object]]:
    payload: List[Dict[str, object]] = []
    for row in rows:
        profit_value = row.profit_best_buy if row.profit_best_buy is not None else row.profit_normal
        payload.append(
            {
                "item_name": row.item_name or f"Type {row.type_id}",
                "region_name": row.region_name,
                "investment": row.investment,
                "profit": profit_value,
            }
        )
    return payload


def main() -> None:
    args = parse_args()
    orders = load_orders(args.orders_path)
    sell_samples, best_buy_prices, next_sell_prices = collect_market_stats(orders)
    normals = compute_normal_prices(
        sell_samples,
        min_samples=args.min_samples,
        trim_ratio=args.trim_ratio,
    )
    stations = load_station_lookup(args.stations_path)
    buy_multiplier = args.min_buy_multiplier
    if buy_multiplier is None:
        buy_multiplier = args.min_multiplier
    elif buy_multiplier <= 0:
        buy_multiplier = None
    gap_multiplier = args.min_gap_multiplier
    if gap_multiplier is not None and gap_multiplier <= 1.0:
        gap_multiplier = None
    suspicious = find_suspicious_orders(
        orders,
        normals,
        min_multiplier=args.min_multiplier,
        min_normal_price=args.min_normal_price,
        min_normal_value=args.min_normal_value,
        min_volume=args.min_volume,
        station_lookup=stations,
        best_buy_prices=best_buy_prices,
        next_sell_prices=next_sell_prices,
        min_buy_multiplier=buy_multiplier,
        min_gap_multiplier=gap_multiplier,
        min_investment=args.min_investment,
        min_profit=args.min_profit,
        sort_by=args.sort_by,
    )
    if not suspicious:
        print("No suspicious sell orders matched the filters.")
        return
    limited = suspicious[: args.limit] if args.limit else suspicious
    if not args.skip_item_names:
        item_lookup = resolve_item_names(args.items_path, {order.type_id for order in limited})
        for order in limited:
            order.item_name = item_lookup.get(order.type_id, order.item_name)
    output_text: str
    if args.format == "json":
        output_text = json.dumps(to_json(limited), indent=2)
    else:
        output_text = format_table(limited)
    if args.output:
        args.output.write_text(output_text + "\n", encoding="utf-8")
    print(output_text)


if __name__ == "__main__":
    main()
