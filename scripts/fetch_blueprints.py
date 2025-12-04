"""Download the CCP SDE blueprint YAML and convert it to JSON."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

import requests
import yaml

DEFAULT_OUTPUT = Path("data/blueprints.json")
DEFAULT_SDE_URL = "https://eve-static-data-export.s3-eu-west-1.amazonaws.com/tranquility/sde.zip"
BLUEPRINT_FILE_CANDIDATES = (
	"industryblueprints.yaml",
	"fsd/blueprints.yaml",
	"blueprints.yaml",
)
USER_AGENT = "EveAdvancedLogistics/0.1 (blueprint fetcher)"
REQUEST_TIMEOUT = 60


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Fetch the EVE SDE blueprint YAML and store manufacturing data as JSON."
	)
	parser.add_argument(
		"--output",
		type=Path,
		default=DEFAULT_OUTPUT,
		help="Destination JSON path (default: data/blueprints.json).",
	)
	parser.add_argument(
		"--input-yaml",
		type=Path,
		help="Optional existing industryBlueprints.yaml to convert (skips download).",
	)
	parser.add_argument(
		"--sde-url",
		default=DEFAULT_SDE_URL,
		help="Override the default CCP SDE ZIP URL.",
	)
	parser.add_argument(
		"--max-blueprints",
		type=int,
		default=None,
		help="Limit the number of blueprint entries (useful for testing).",
	)
	return parser.parse_args()


def download_sde_archive(session: requests.Session, url: str, destination: Path) -> None:
	with session.get(url, stream=True, timeout=REQUEST_TIMEOUT) as response:
		response.raise_for_status()
		with destination.open("wb") as handle:
			for chunk in response.iter_content(chunk_size=65536):
				if chunk:
					handle.write(chunk)


def extract_blueprints_yaml(archive_path: Path) -> bytes:
	import zipfile

	with zipfile.ZipFile(archive_path, "r") as archive:
		names = archive.namelist()
		for candidate in names:
			lower_name = candidate.lower()
			if any(lower_name.endswith(target) for target in BLUEPRINT_FILE_CANDIDATES):
				with archive.open(candidate, "r") as blueprint_file:
					return blueprint_file.read()
	raise FileNotFoundError(
		"industryBlueprints.yaml (or equivalent) not found in downloaded SDE archive"
	)


def coerce_int(value: Any) -> Optional[int]:
	if value is None:
		return None
	if isinstance(value, bool):
		return int(value)
	try:
		return int(value)
	except (TypeError, ValueError):
		return None


def coerce_float(value: Any) -> Optional[float]:
	if value is None:
		return None
	try:
		return float(value)
	except (TypeError, ValueError):
		return None


def extract_type_id(entry: Mapping[str, Any]) -> Optional[int]:
	for key in ("type_id", "typeID", "typeId", "type"):  # accommodate SDE variants
		if entry.get(key) is not None:
			return coerce_int(entry.get(key))
	return None


def sanitize_materials(raw_list: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
	materials: List[Dict[str, Any]] = []
	for item in raw_list:
		if not isinstance(item, Mapping):
			continue
		type_id = extract_type_id(item)
		quantity = coerce_int(item.get("quantity"))
		if type_id is None or quantity is None:
			continue
		entry: Dict[str, Any] = {"type_id": type_id, "quantity": quantity}
		if "consume" in item:
			entry["consume"] = bool(item["consume"])
		materials.append(entry)
	return materials


def sanitize_products(raw_list: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
	products: List[Dict[str, Any]] = []
	for item in raw_list:
		if not isinstance(item, Mapping):
			continue
		type_id = extract_type_id(item)
		quantity = coerce_int(item.get("quantity"))
		if type_id is None or quantity is None:
			continue
		entry: Dict[str, Any] = {"type_id": type_id, "quantity": quantity}
		probability = coerce_float(item.get("probability"))
		if probability is not None:
			entry["probability"] = probability
		products.append(entry)
	return products


def sanitize_skills(raw_list: Iterable[Mapping[str, Any]]) -> List[Dict[str, Any]]:
	skills: List[Dict[str, Any]] = []
	for item in raw_list:
		if not isinstance(item, Mapping):
			continue
		type_id = extract_type_id(item)
		level = coerce_int(item.get("level"))
		if type_id is None or level is None:
			continue
		skills.append({"type_id": type_id, "level": level})
	return skills


def sanitize_activity(activity: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
	output: Dict[str, Any] = {}
	time_value = coerce_int(activity.get("time"))
	if time_value is not None:
		output["time"] = time_value
	probability = coerce_float(activity.get("probability"))
	if probability is not None:
		output["probability"] = probability
	materials = activity.get("materials")
	if isinstance(materials, list):
		sanitized = sanitize_materials(materials)
		if sanitized:
			output["materials"] = sanitized
	products = activity.get("products")
	if isinstance(products, list):
		sanitized_products = sanitize_products(products)
		if sanitized_products:
			output["products"] = sanitized_products
	skills = activity.get("skills")
	if isinstance(skills, list):
		sanitized_skills = sanitize_skills(skills)
		if sanitized_skills:
			output["skills"] = sanitized_skills
	return output or None


def normalize_blueprint_entry(key: Optional[int], entry: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
	blueprint_id = entry.get("blueprintTypeID")
	if blueprint_id is None and key is not None:
		blueprint_id = key
	blueprint_id = coerce_int(blueprint_id)
	if blueprint_id is None:
		return None
	record: Dict[str, Any] = {"blueprintTypeID": blueprint_id}
	name = entry.get("name") or entry.get("blueprintTypeName")
	if isinstance(name, str) and name.strip():
		record["name"] = name.strip()
		record["blueprintTypeName"] = name.strip()
	max_limit = coerce_int(entry.get("maxProductionLimit"))
	if max_limit is not None:
		record["maxProductionLimit"] = max_limit
	activities = entry.get("activities")
	if isinstance(activities, Mapping):
		sanitized_activities: Dict[str, Any] = {}
		for activity_name, activity_payload in activities.items():
			if not isinstance(activity_payload, Mapping):
				continue
			sanitized = sanitize_activity(activity_payload)
			if sanitized:
				sanitized_activities[str(activity_name)] = sanitized
		if sanitized_activities:
			record["activities"] = sanitized_activities
	return record


def normalize_blueprints(payload: Any, *, max_entries: Optional[int]) -> List[Dict[str, Any]]:
	records: List[Dict[str, Any]] = []
	remaining = None if max_entries is None else max(1, max_entries)
	if isinstance(payload, Mapping):
		items: Iterable[tuple[Any, Any]] = payload.items()
	elif isinstance(payload, list):
		items = ((None, entry) for entry in payload)
	else:
		raise ValueError("Blueprint YAML must be a mapping or list")
	for raw_key, value in items:
		if remaining is not None and remaining == 0:
			break
		if not isinstance(value, Mapping):
			continue
		normalized = normalize_blueprint_entry(coerce_int(raw_key), value)
		if not normalized:
			continue
		records.append(normalized)
		if remaining is not None:
			remaining -= 1
	records.sort(key=lambda entry: entry["blueprintTypeID"])
	if not records:
		raise RuntimeError("No blueprint entries were parsed from the YAML")
	return records


def write_output(records: List[Dict[str, Any]], destination: Path) -> None:
	destination.parent.mkdir(parents=True, exist_ok=True)
	destination.write_text(json.dumps(records, indent=2), encoding="utf-8")


def load_from_yaml_text(text: str, *, max_entries: Optional[int]) -> List[Dict[str, Any]]:
	payload = yaml.safe_load(text)
	return normalize_blueprints(payload, max_entries=max_entries)


def main() -> None:
	args = parse_args()
	max_entries = args.max_blueprints
	session = requests.Session()
	session.headers.update({"User-Agent": USER_AGENT})

	if args.input_yaml:
		yaml_path = args.input_yaml.expanduser()
		if not yaml_path.exists():
			raise FileNotFoundError(f"Blueprint YAML {yaml_path} does not exist")
		yaml_text = yaml_path.read_text(encoding="utf-8")
		records = load_from_yaml_text(yaml_text, max_entries=max_entries)
	else:
		with tempfile.TemporaryDirectory() as tmpdir:
			tmp_path = Path(tmpdir)
			archive_path = tmp_path / "sde.zip"
			download_sde_archive(session, args.sde_url, archive_path)
			yaml_bytes = extract_blueprints_yaml(archive_path)
			records = load_from_yaml_text(yaml_bytes.decode("utf-8"), max_entries=max_entries)

	write_output(records, args.output)
	print(f"Wrote {len(records)} blueprint entries to {args.output}")


if __name__ == "__main__":
	main()
