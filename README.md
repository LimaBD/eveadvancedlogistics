# Eve Advanced Logistics

## Region data export

Use the helper script in `scripts/fetch_regions.py` to download the latest region
names and IDs from the EVE ESI API:

1. Install dependencies with `pip install -r requirements.txt`.
2. Run `python scripts/fetch_regions.py` to write the consolidated list (always
	saved to `data/regions.json`).

## Item data export

The companion script `scripts/fetch_items.py` enumerates the entire universe type
list and enriches each entry with group and category metadata so downstream
automation can reason about item classes.

1. Install dependencies with `pip install -r requirements.txt` (same as above).
2. Execute `python scripts/fetch_items.py` to create the JSON export (always
	saved to `data/items.json`). The run may take a long time
	because it touches every type record; use `--max-items 100` while testing to
	fetch only the first 100 IDs.

## Ship data export

`scripts/fetch_ships.py` focuses on the Ship category (category ID 6) and pulls
every published ship type so tooling can quickly reference the canonical list of
ship names, IDs, and their parent group metadata.

1. Install dependencies with `pip install -r requirements.txt` (same as above).
2. Run `python scripts/fetch_ships.py` to produce `data/ships.json`. Use
	`--max-types 50` while iterating locally to limit the volume of type detail
	requests and keep the feedback loop quick.

Both scripts automatically create the `data/` directory when needed, so manual
setup is minimal.
