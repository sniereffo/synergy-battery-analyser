# Synergy Battery Feasibility Analyser

Analyse your [Synergy](https://www.synergy.net.au/) (Western Australia) electricity usage data to determine whether a home battery is financially worthwhile.

> This project was built with AI assistance — thanks Claude! I had a personal need to review my energy usage and determine whether a home battery made financial sense. The speed at which I could go from idea to a working tool that analyses, visualises, and informs that decision was fantastic. Maybe someone else will find it useful too!

<p align="center">
  <img src="example.png" alt="Screenshot">
</p>

## Features

- **Synergy portal integration** — Logs in to the current My Synergy portal (`my.synergy.net.au`) in a real browser window (you complete the MFA yourself), then reads your half-hourly interval data automatically
- **Tariff-aware analysis** — Reads the tariff period of each half-hourly slot directly from Synergy's data (peak, off-peak, off-peak AM, overnight, super off-peak), and prefills known plan rates which you can edit to match your bill
- **Battery simulation** — Models half-hourly charge/discharge cycles, charges from solar export, discharges to offset grid import, with optional grid top-up during the cheapest tariff period
- **Feed-in credit accounting** — Calculates DEBS feed-in credits (configurable peak/off-peak rates) and tracks lost credits when the battery captures energy that would otherwise be exported
- **Offline analysis** — Saves fetched interval data locally so you can re-run analysis with different parameters without logging in again
- **Light/dark theme** — Toggleable via header button, honours system preference with flash-free page load
- **CSV export** — Download the full interval dataset for use in spreadsheets or other tools

## How it works

1. Click **Open Synergy login** — a browser window opens on the My Synergy portal. Log in as normal, including your one-time MFA code. Your password is entered only on Synergy's own site; this app never sees it.
2. Pick your service, choose a date range, set battery capacity and feed-in rates, and confirm the (prefilled) tariff rates.
3. The tool fetches your half-hourly consumption and solar-export data from the portal, one day per request, and saves it locally for future use.
4. Each interval is priced using its Synergy-supplied tariff period and a battery charge/discharge simulation is run.
5. Results are presented as summary cards and six interactive charts.

> **Note on the browser step:** Synergy's portal is protected by multi-factor authentication and reCAPTCHA, which are designed to require a human. The app therefore drives a real browser for login rather than logging in headlessly — you'll complete the MFA prompt yourself each session. Fetching a long date range takes a little while, since the portal only returns one day of half-hourly data per request.

## Charts

- **Daily Usage** — Stacked area chart of daily consumption broken down by tariff period (peak, off-peak, super off-peak, overnight) with solar export overlay. Includes a zoom slider for navigating the full date range.
- **Cost Breakdown** — Daily cost in dollars split by tariff period, showing the financial impact of each rate tier over time.
- **Import vs Export** — Grid import and grid export as overlaid line charts across the full analysis period, useful for spotting seasonal generation and consumption patterns.
- **Battery Simulation** — Per-day view with a date picker to jump to any day. Shows battery state of charge (left axis) alongside half-hourly consumption, solar export, and battery discharge bars (right axis).
- **ROI** — Cumulative savings curve over the analysis period. If a battery cost is provided, shows the breakeven point and estimated payback period.
- **Heatmap** — Hour-of-day vs date grid showing consumption intensity. Highlights usage patterns across time of day and season.

## Development

Requires Python 3.14+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install the browser used for the login/fetch step (one-off)
uv run playwright install chromium

# Run the dev server
uv run uvicorn synergy_analyser.main:app --host 127.0.0.1 --port 8000

# Run tests
uv run pytest -v

# Live end-to-end check (opens a browser; you log in) — fetches one day
uv run python scripts/smoke_fetch.py 2026-08-02
```

## Data notes

- Usage comes from the portal's `MyAccount_ChartData` procedure. Each half-hourly slot arrives already labelled with its tariff period; consumption and solar export are in **kWh** directly (no kW→kWh conversion needed).
- `VAL_SOLAR` is grid **export** (what the meter sends back), which is what feed-in credits are earned on.
- Feed-in rates default to DEBS: 10 c/kWh peak (3pm–9pm), 2 c/kWh off-peak.

## License

This project is licensed under the [Elastic License 2.0](LICENSE).
