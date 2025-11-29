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
	because it touches every type record; use `--max-types 100` while testing to
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

## Ship market arbitrage scanner

Use `scripts/find_arbitrage.py` to walk every regional market page in lockstep
and surface profitable ship flips (buying from sell orders, selling to buy
orders). The tool reuses the locally cached ship/region datasets so it does not
have to refetch static metadata each run.

```
python scripts/find_arbitrage.py --min-margin 0.05 --min-profit 5000000 --top 10
```

Key options:

- `--min-margin`: Smallest acceptable margin (defaults to 2%).
- `--min-profit`: Minimum total ISK profit for an opportunity (defaults to 10M).
- `--max-investment`: Maximum ISK you are willing to spend on the hull (defaults to 85M).
- `--top`: Number of opportunities printed after each page sweep.
- `--max-pages`: Cap the number of pages per region while testing.

The script prints an updated table after completing page *N* for every region,
so you immediately see fresh opportunities as the crawl deepens.

## Item-wide arbitrage scanner grouped by route

Use `scripts/item_arbitrage.py` when you want to consider *any* published item
and weigh each trade against your hauling constraints. All runtime parameters
now live in a JSON profile; the CLI only accepts `--profile PATH`.

```
python scripts/item_arbitrage.py --profile scripts/presets/fast_rat.json
```

Profiles accept keys that mirror the previous CLI options, including:

- `current_system` (required): Name or ID of your present location.
- `max_investment`, `max_volume`, `max_item_volume`, `min_multiplier`,
  `min_profit_per_jump`, `max_total_jumps`, `max_units`, `max_items`, `top`.
- `items`, `regions`, `systems`, `final_output`: Override data file paths.
- `forge_adjacent_only`, `route_flag`, `request_delay`, `max_pages`,
  `item_limit` for advanced tuning/testing.

Ship with one of the bundled presets in `scripts/presets/*.json` (e.g.
`fast_rat.json`, `fat_bastard.json`, `cheap_ships.json`) or duplicate a profile
and tweak the key/value pairs. Relative paths inside a profile are resolved
relative to the profile file itself.

After each full page sweep the script prints a table summarizing each buy/sell
route (grouping all items that share the same origin/destination pair) along
with item-level details and ISK per jump. The final snapshot is written to
`data/arbitrage_items_summary.txt` by default unless overridden in the profile.

## Route jump calculator

Use `scripts/calc_jumps.py` to query the ESI routing API and quickly determine
how many stargate jumps separate two systems. The script also exposes the
`calculate_jump_count` function for reuse in other automation.

```
python scripts/calc_jumps.py Jita Amarr --flag secure --show-route
```

Arguments:

- Positional `origin`/`destination` accept either system names or numeric IDs.
- `--flag` controls ESI routing mode (`shortest`, `secure`, `insecure`).
- `--systems-file` overrides the cached systems JSON (defaults to
	`data/systems.json`).
- `--show-route` prints the full, name-resolved waypoint list instead of just
	the jump count.
