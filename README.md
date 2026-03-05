# Synergy Battery Feasibility Analyser

Analyse your [Synergy](https://www.synergy.net.au/) (Western Australia) electricity usage data to determine whether a home battery is financially worthwhile.

## Features

- **Synergy API integration** - Fetches 30-minute interval data directly from Synergy's self-serve portal (email or SMS OTP authentication)
- **Tariff-aware analysis** - Auto-detects your energy plan (EV Add On, Midday Saver, Home Plan A1, or custom) with editable rates
- **Battery simulation** - Models charge/discharge cycles, including grid top-up during cheapest tariffs
- **Feed-in credit accounting** - Calculates DEBS feed-in credits (peak/off-peak) and tracks lost credits when battery captures export
- **Interactive charts** - Six ECharts visualisations: daily usage, cost breakdown, import vs export, battery simulation, ROI, and heatmap
- **Data caching** - Saves API responses locally for offline re-analysis without re-authenticating
- **Light/dark theme** - Toggleable, honours system preference
- **CSV export** - Download interval data for further analysis

## Quick start

1. Download the latest release for your platform from the [Releases](https://github.com/sniereffo/synergy-battery-analyser/releases) page
2. Run the binary — your default browser will open automatically to the app

No installation or dependencies required.

## Development

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
# Run the dev server
uv run uvicorn synergy_analyser.main:app --host 127.0.0.1 --port 8000

# Run tests
uv run pytest -v

# Build standalone binary
uv run --group dev pyinstaller synergy-analyser.spec
```

## How it works

1. Enter your premise address and verify via email or SMS OTP
2. Configure battery capacity, date range, and feed-in rates
3. The tool fetches your half-hourly consumption and export data from Synergy
4. It categorises each interval by tariff period and simulates battery behaviour
5. Results show net savings, cost comparison, and estimated payback period

## Data notes

- `kwHalfHourlyValues` from the Synergy API is in **kW** (power) - the tool converts to kWh by multiplying by 0.5
- `kwhHalfHourlyValuesGeneration` is grid **export**, not total solar generation - already in kWh
- Feed-in rates default to DEBS: 10 c/kWh peak (3pm-9pm), 2 c/kWh off-peak

## License

This project is licensed under the [Elastic License 2.0](LICENSE).
