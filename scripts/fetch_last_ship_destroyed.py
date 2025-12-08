"""Fetch the most recent ship kill for a solar system using the ESI kill stream."""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import requests

ESI_BASE = "https://esi.evetech.net/latest"
DATASOURCE = "tranquility"
KILL_STREAM_URL = f"{ESI_BASE}/killmails/stream/?datasource={DATASOURCE}"
USER_AGENT = "EveAdvancedLogistics/0.1 (last kill fetcher)"
REQUEST_TIMEOUT = 20
DEFAULT_SYSTEMS = Path("data/systems.json")
DEFAULT_SHIPS = Path("data/ships.json")
DEFAULT_INTERVAL = 60
DEFAULT_LOOKBACK = 60


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })
    return session


def load_json_array(path: Path) -> List[Dict]:
    if not path.exists():
        raise FileNotFoundError(f"Missing required data file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, list):
        raise ValueError(f"Expected a JSON array in {path}")
    return data


def build_system_indexes(path: Path) -> Tuple[Dict[int, Dict], Dict[str, Dict]]:
    systems = load_json_array(path)
    by_id: Dict[int, Dict] = {}
    by_name: Dict[str, Dict] = {}
    for entry in systems:
        system_id = int(entry["id"])
        by_id[system_id] = entry
        by_name[entry["name"].lower()] = entry
    return by_id, by_name


def resolve_system(
    *,
    system_name: Optional[str],
    system_id: Optional[int],
    systems_file: Path,
) -> Tuple[int, Dict]:
    by_id, by_name = build_system_indexes(systems_file)
    if system_id:
        if system_id not in by_id:
            raise ValueError(f"System id {system_id} not found in {systems_file}")
        return system_id, by_id[system_id]
    if system_name:
        entry = by_name.get(system_name.lower())
        if not entry:
            raise ValueError(f"System '{system_name}' not found in {systems_file}")
        return int(entry["id"]), entry
    raise ValueError("Provide either --system or --system-id")


def build_ship_index(path: Path) -> Dict[int, Dict[str, str]]:
    ships = load_json_array(path)
    return {
        int(entry["id"]): {
            "name": entry.get("name", f"Ship {entry['id']}").strip(),
            "group": entry.get("group_name"),
        }
        for entry in ships
    }


def fetch_killmail(session: requests.Session, killmail_id: int, kill_hash: str) -> Dict:
    url = f"{ESI_BASE}/killmails/{killmail_id}/{kill_hash}/?datasource={DATASOURCE}"
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def resolve_names(session: requests.Session, identifiers: Iterable[int]) -> Dict[int, Dict[str, str]]:
    ids = [identifier for identifier in identifiers if identifier]
    if not ids:
        return {}
    url = f"{ESI_BASE}/universe/names/?datasource={DATASOURCE}"
    response = session.post(url, json=ids, timeout=REQUEST_TIMEOUT)
    if response.status_code == 404:
        return {}
    response.raise_for_status()
    payload = response.json()
    return {entry["id"]: entry for entry in payload}


def iter_location_endpoints(location_id: int) -> List[Tuple[str, str]]:
    if location_id >= 61000000:
        return [("stations", "station")]
    if location_id >= 50000000:
        return [("stargates", "stargate")]
    if location_id >= 40000000:
        return [
            ("moons", "moon"),
            ("planets", "planet"),
            ("asteroid_belts", "asteroid belt"),
        ]
    return []


def resolve_location(session: requests.Session, location_id: Optional[int]) -> Optional[Dict[str, str]]:
    if not location_id:
        return None
    endpoints = iter_location_endpoints(location_id)
    for endpoint, label in endpoints:
        url = f"{ESI_BASE}/universe/{endpoint}/{location_id}/?datasource={DATASOURCE}"
        response = session.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            continue
        response.raise_for_status()
        payload = response.json()
        return {
            "name": payload.get("name", f"{label.title()} {location_id}"),
            "kind": label,
        }
    if location_id >= 1020000000000:
        return {
            "name": f"Structure {location_id}",
            "kind": "structure (name requires auth)",
        }
    return {"name": f"Location {location_id}", "kind": "unresolved"}


def parse_killmail_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def prune_seen_kills(seen: Dict[int, datetime], oldest_allowed: datetime) -> None:
    stale_ids = [kill_id for kill_id, timestamp in seen.items() if timestamp < oldest_allowed]
    for kill_id in stale_ids:
        del seen[kill_id]


def iter_kill_stream(session: requests.Session) -> Iterator[Dict[str, str]]:
    headers = {
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
        "User-Agent": USER_AGENT,
    }
    backoff = 5
    while True:
        try:
            with session.get(
                KILL_STREAM_URL,
                headers=headers,
                stream=True,
                timeout=(REQUEST_TIMEOUT, REQUEST_TIMEOUT),
            ) as response:
                response.raise_for_status()
                data_lines: List[str] = []
                for raw_line in response.iter_lines(decode_unicode=True, chunk_size=1):
                    if raw_line is None:
                        continue
                    line = raw_line.strip()
                    if not line:
                        if not data_lines:
                            continue
                        payload = "\n".join(data_lines)
                        data_lines.clear()
                        try:
                            yield json.loads(payload)
                        except json.JSONDecodeError as exc:
                            now_str = datetime.now(timezone.utc).isoformat()
                            print(f"[{now_str}] Failed to decode kill stream event: {exc}: {payload}")
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].strip())
                # Connection closed cleanly; immediately reconnect.
        except requests.RequestException as exc:
            now_str = datetime.now(timezone.utc).isoformat()
            print(f"[{now_str}] Kill stream error: {exc}. Retrying in {backoff}s.")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
        else:
            backoff = 5


def build_report(
    *,
    system_entry: Dict,
    killmail_id: int,
    killmail_hash: str,
    killmail: Dict,
    ship_lookup: Dict[int, Dict[str, str]],
    name_index: Dict[int, Dict[str, str]],
    location: Optional[Dict[str, str]],
) -> Dict:
    victim = killmail.get("victim", {})
    ship_type_id = victim.get("ship_type_id")
    ship_data = ship_lookup.get(ship_type_id, {})
    character_id = victim.get("character_id")
    corporation_id = victim.get("corporation_id")
    alliance_id = victim.get("alliance_id")

    def lookup(entity_id: Optional[int]) -> Optional[str]:
        if not entity_id:
            return None
        entry = name_index.get(entity_id)
        if entry:
            return entry.get("name")
        return str(entity_id)

    return {
        "system_id": system_entry["id"],
        "system_name": system_entry["name"],
        "region_id": system_entry.get("region_id"),
        "killmail_id": killmail_id,
        "killmail_time": killmail.get("killmail_time"),
        "victim": {
            "character": lookup(character_id) or "NPC",
            "corporation": lookup(corporation_id),
            "alliance": lookup(alliance_id),
            "damage_taken": victim.get("damage_taken"),
        },
        "ship": {
            "type_id": ship_type_id,
            "name": ship_data.get("name", f"Type {ship_type_id}" if ship_type_id else "Unknown ship"),
            "group": ship_data.get("group"),
        },
        "location": location,
        "links": {
            "zkill": f"https://zkillboard.com/kill/{killmail_id}/",
            "esi": f"{ESI_BASE}/killmails/{killmail_id}/{killmail_hash}/?datasource={DATASOURCE}",
        },
    }


def print_text_report(report: Dict) -> None:
    print(f"Latest kill in {report['system_name']} ({report['system_id']}):")
    print(f"  Time (UTC): {report['killmail_time']}")
    victim = report["victim"]
    victim_line = victim["character"]
    corporation = victim.get("corporation")
    if corporation:
        victim_line += f" | {corporation}"
    alliance = victim.get("alliance")
    if alliance:
        victim_line += f" [{alliance}]"
    print(f"  Victim: {victim_line}")
    ship = report["ship"]
    ship_line = ship.get("name")
    if ship.get("group"):
        ship_line += f" ({ship['group']})"
    print(f"  Ship: {ship_line}")
    if report.get("location"):
        loc = report["location"]
        print(f"  Location: {loc['name']} ({loc['kind']})")
    damage_taken = victim.get("damage_taken")
    if damage_taken is not None:
        print(f"  Damage taken: {damage_taken:,}")
    links = report.get("links", {})
    link = links.get("zkill") or links.get("esi")
    if link:
        print(f"  Killmail: {link}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch the latest ship kill for an EVE Online solar system.",
    )
    parser.add_argument("--system", help="Solar system name, e.g. 'Jita'.")
    parser.add_argument("--system-id", type=int, help="Solar system id, e.g. 30000142.")
    parser.add_argument(
        "--systems-file",
        type=Path,
        default=DEFAULT_SYSTEMS,
        help="Path to systems.json (default: data/systems.json).",
    )
    parser.add_argument(
        "--ships-file",
        type=Path,
        default=DEFAULT_SHIPS,
        help="Path to ships.json (default: data/ships.json).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help="Seconds between heartbeat messages while waiting for a match (default: 60).",
    )
    parser.add_argument(
        "--lookback",
        type=int,
        default=DEFAULT_LOOKBACK,
        help="Only show kills newer than this many seconds (default: 60).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    session = build_session()
    system_id, system_entry = resolve_system(
        system_name=args.system,
        system_id=args.system_id,
        systems_file=args.systems_file,
    )
    ship_lookup = build_ship_index(args.ships_file)

    seen_kills: Dict[int, datetime] = {}
    last_match: Optional[datetime] = None
    heartbeat_interval = max(5, args.interval)
    next_heartbeat = time.monotonic() + heartbeat_interval
    lookback_delta = timedelta(seconds=max(1, args.lookback))

    try:
        for event in iter_kill_stream(session):
            try:
                now = datetime.now(timezone.utc)
                prune_seen_kills(seen_kills, now - lookback_delta)

                if time.monotonic() >= next_heartbeat:
                    if last_match:
                        last_str = last_match.isoformat()
                        print(f"[{now.isoformat()}] Watching {system_entry['name']} (last match {last_str}).")
                    else:
                        print(f"[{now.isoformat()}] Watching {system_entry['name']} (no matches yet).")
                    next_heartbeat = time.monotonic() + heartbeat_interval

                killmail_id = event.get("killmail_id")
                kill_hash = event.get("killmail_hash")
                if not killmail_id or not kill_hash:
                    continue
                if killmail_id in seen_kills:
                    continue

                try:
                    killmail = fetch_killmail(session, killmail_id, kill_hash)
                except requests.RequestException as exc:
                    now_str = datetime.now(timezone.utc).isoformat()
                    print(f"[{now_str}] Failed to fetch killmail {killmail_id}: {exc}")
                    continue

                kill_time_str = killmail.get("killmail_time")
                if not kill_time_str:
                    continue
                kill_time = parse_killmail_time(kill_time_str)
                if kill_time < now - lookback_delta:
                    continue
                if killmail.get("solar_system_id") != system_id:
                    continue

                location_id = (
                    killmail.get("victim", {}).get("structure_id")
                    or killmail.get("moon_id")
                )
                location = resolve_location(session, location_id)

                ids_to_resolve = {
                    killmail.get("victim", {}).get("character_id"),
                    killmail.get("victim", {}).get("corporation_id"),
                    killmail.get("victim", {}).get("alliance_id"),
                }
                name_index = resolve_names(session, ids_to_resolve)
                report = build_report(
                    system_entry=system_entry,
                    killmail_id=killmail_id,
                    killmail_hash=kill_hash,
                    killmail=killmail,
                    ship_lookup=ship_lookup,
                    name_index=name_index,
                    location=location,
                )

                if args.format == "json":
                    print(json.dumps(report, indent=2))
                else:
                    print_text_report(report)
                    print("-")

                seen_kills[killmail_id] = kill_time
                last_match = kill_time
            except Exception as exc:  # noqa: BLE001
                now_str = datetime.now(timezone.utc).isoformat()
                print(f"[{now_str}] Error while processing kill stream: {exc}")
                continue
    except KeyboardInterrupt:
        print("Stopping kill monitor.")


if __name__ == "__main__":
    main()
