"""Fetch all published EVE Online item types and persist them to JSON."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
TYPES_PAGE_URL = f"{ESI_BASE}/universe/types/?datasource={DATASOURCE}&page={{page}}"
TYPE_DETAIL_URL = f"{ESI_BASE}/universe/types/{{type_id}}/?datasource={DATASOURCE}"
GROUP_DETAIL_URL = f"{ESI_BASE}/universe/groups/{{group_id}}/?datasource={DATASOURCE}"
CATEGORY_DETAIL_URL = f"{ESI_BASE}/universe/categories/{{category_id}}/?datasource={DATASOURCE}"
USER_AGENT = "EveAdvancedLogistics/0.1 (items fetcher)"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
BACKOFF_SECONDS = 1.5
DEFAULT_OUTPUT = Path("data/items.json")


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


def fetch_types_page(session: requests.Session, page: int) -> List[int]:
    url = TYPES_PAGE_URL.format(page=page)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                if page == 1:
                    raise RuntimeError("Types endpoint returned 404 on first page")
                return []
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError(f"Unexpected payload for types page {page}")
            return [int(type_id) for type_id in payload]
        except requests.RequestException as exc:  # pragma: no cover - network heavy
            if attempt == MAX_RETRIES:
                raise RuntimeError(f"Failed to fetch {url}") from exc
            time.sleep(BACKOFF_SECONDS * attempt)
    raise RuntimeError(f"Failed to fetch {url}")


def fetch_all_type_ids(session: requests.Session) -> List[int]:
    type_ids: List[int] = []
    page = 1
    while True:
        page_ids = fetch_types_page(session, page)
        if not page_ids:
            break
        type_ids.extend(page_ids)
        page += 1
    if not type_ids:
        raise RuntimeError("Failed to discover any type IDs")
    return type_ids


def fetch_type_detail(session: requests.Session, type_id: int) -> Dict:
    url = TYPE_DETAIL_URL.format(type_id=type_id)
    payload = fetch_with_retries(session, url)
    name = payload.get("name")
    group_id = payload.get("group_id")
    if name is None or group_id is None:
        raise ValueError(f"Type {type_id} missing required fields")
    entry: Dict = {
        "id": int(type_id),
        "name": str(name),
        "group_id": int(group_id),
        "published": bool(payload.get("published", True)),
    }
    if payload.get("market_group_id") is not None:
        entry["market_group_id"] = int(payload["market_group_id"])
    for numeric_key in ("volume", "packaged_volume", "mass", "portion_size"):
        if payload.get(numeric_key) is not None:
            entry[numeric_key] = float(payload[numeric_key])
    return entry


def fanout_type_fetch(
    session: requests.Session, type_ids: Sequence[int], max_workers: int
) -> List[Dict]:
    worker = partial(fetch_type_detail, session)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        return list(executor.map(worker, type_ids))


def fetch_group_detail(session: requests.Session, group_id: int) -> Dict:
    url = GROUP_DETAIL_URL.format(group_id=group_id)
    payload = fetch_with_retries(session, url)
    name = payload.get("name")
    category_id = payload.get("category_id")
    if name is None or category_id is None:
        raise ValueError(f"Group {group_id} missing required fields")
    return {
        "id": int(group_id),
        "name": str(name),
        "category_id": int(category_id),
    }


def fanout_group_fetch(
    session: requests.Session, group_ids: Iterable[int], max_workers: int
) -> Dict[int, Dict]:
    unique_ids = list(dict.fromkeys(int(group_id) for group_id in group_ids))
    worker = partial(fetch_group_detail, session)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        group_entries = list(executor.map(worker, unique_ids))
    return {entry["id"]: entry for entry in group_entries}


def fetch_category_detail(session: requests.Session, category_id: int) -> Dict:
    url = CATEGORY_DETAIL_URL.format(category_id=category_id)
    payload = fetch_with_retries(session, url)
    name = payload.get("name")
    if name is None:
        raise ValueError(f"Category {category_id} missing name field")
    return {
        "id": int(category_id),
        "name": str(name),
    }


def fanout_category_fetch(
    session: requests.Session, category_ids: Iterable[int], max_workers: int
) -> Dict[int, Dict]:
    unique_ids = list(dict.fromkeys(int(category_id) for category_id in category_ids))
    worker = partial(fetch_category_detail, session)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        category_entries = list(executor.map(worker, unique_ids))
    return {entry["id"]: entry for entry in category_entries}


def build_item_records(
    types: Sequence[Dict], groups: Dict[int, Dict], categories: Dict[int, Dict]
) -> List[Dict]:
    items: List[Dict] = []
    for type_entry in types:
        group = groups.get(type_entry["group_id"], {})
        category = categories.get(group.get("category_id")) if group else None
        record: Dict = {
            "id": type_entry["id"],
            "name": type_entry["name"],
            "group_id": type_entry["group_id"],
            "group_name": group.get("name"),
            "category_id": group.get("category_id") if group else None,
            "category_name": category.get("name") if category else None,
            "published": type_entry["published"],
        }
        optional_keys = (
            "market_group_id",
            "volume",
            "packaged_volume",
            "mass",
            "portion_size",
        )
        for key in optional_keys:
            if key in type_entry:
                record[key] = type_entry[key]
        items.append(record)
    return sorted(items, key=lambda entry: entry["name"])


def write_items_file(items: List[Dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(items, indent=2, sort_keys=False), encoding="utf-8"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch the global EVE item/type catalog into data/items.json."
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=16,
        help="Parallel workers for type/group/category lookups.",
    )
    parser.add_argument(
        "--max-types",
        type=int,
        default=None,
        help="Fetch only the first N type IDs (useful for local testing).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="Destination JSON path (default: data/items.json).",
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

    type_ids = fetch_all_type_ids(session)
    if args.max_types:
        type_ids = type_ids[: max(1, args.max_types)]

    types = fanout_type_fetch(session, type_ids, max_workers=max_workers)
    group_ids = {entry["group_id"] for entry in types}
    groups = fanout_group_fetch(session, group_ids, max_workers=max_workers)
    category_ids = {entry["category_id"] for entry in groups.values()}
    categories = fanout_category_fetch(session, category_ids, max_workers=max_workers)

    items = build_item_records(types, groups, categories)
    write_items_file(items, args.output)
    print(f"Wrote {len(items)} items to {args.output}")


if __name__ == "__main__":
    main()
