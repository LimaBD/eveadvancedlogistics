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

## Item-wide arbitrage: split workflow

Live scanning has been split into two scripts so you can fetch once and iterate
on strategies offline.

### 1. Fetch market orders

```
python -m scripts.fetch_market_orders --regions data/regions.json --output data/new_eden_orders.json --max-pages 10
```

- Profile is optional; use it only when you want to reuse an existing region/request-delay configuration.
- Without a profile the script simply walks every region in `--regions` (default `data/regions.json`) and dumps **all** active orders.
- Outputs a JSON blob with `orders[]`, per-region stats, request delay, and provenance info so downstream processing can reuse it.
- Use `--near-forge` to only fetch The Forge and its neighboring regions (same behavior as the `forge_adjacent_only` profile flag); add `--all-regions` to override a profile that enables it.

### 2. Process cached orders

```
python -m scripts.item_arbitrage --profile scripts/presets/fast_rat.json --orders-file data/new_eden_orders.json
```

- Rebuilds order books from the snapshot and applies the selected profile.
- Swap profiles between runs to compare hauling strategies without hitting ESI.
- Writes the usual grouped route report to `final_output` from the profile.

Profiles still carry every runtime knob:

- `current_system` (required), `min_profit_per_jump`, `max_total_jumps`, `min_multiplier`.
- Investment/volume controls: `max_investment`, `max_volume`, `max_item_volume`, `max_units`, `max_items`.
- Data/IO overrides: `items`, `regions`, `systems`, `final_output`.
- Advanced tuning: `route_flag`, `request_delay`, `max_pages`, `item_limit`, `forge_adjacent_only`.

Relative paths in a profile resolve next to the JSON file, so copy an existing
profile under `scripts/presets/` to get started quickly.

## Route jump calculator

The legacy ESI-backed `calc_jumps.py` script has been retired. Routing is now
handled entirely offline using a cached graph, which avoids API churn and makes
repeat queries instant.

First, build or refresh the reusable stargate network file:

```
python scripts/build_system_graph.py --systems-file data/systems.json --output data/system_graph.json
```

Then run the local calculator, which performs a breadth-first search over that
graph:

```
python scripts/calc_jumps_local.py Jita Amarr --graph-file data/system_graph.json --show-route
```

Key options:

- Positional `origin`/`destination` accept either system names or numeric IDs.
- `--graph-file` points at the generated graph (defaults to
	`data/system_graph.json`).
- `--systems-file` provides the metadata needed to resolve names.
- `--show-route` prints the full path.

The local solver always returns the shortest hop count and never calls ESI once
the graph has been generated.
