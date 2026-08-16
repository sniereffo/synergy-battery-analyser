"""Parse the new Synergy portal's `MyAccount_ChartData` interval responses.

The Vlocity integration procedure returns, for a single day requested with
IntervalType=DAILY / Interval=X / ChartType=INTERVAL_DATA, 48 half-hourly rows.
Each row carries the slot's grid-import kWh under exactly one tariff-period
column (Synergy pre-categorises the period for us), its solar export under
VAL_SOLAR, and the slot start time under VAL_TIME ("HHMM"). Values are already
in kWh — unlike the old API there is no kW→kWh conversion.
"""

from datetime import datetime, time

# The tariff-period columns Synergy may attach to an interval row. Exactly one
# is present per row; its value is that slot's grid-import kWh.
TARIFF_COLUMNS = ["PEAK", "OFF_PEAK", "OFF_PEAK(AM)", "OVERNIGHT", "SUPER_OFFPEAK"]

# DEBS feed-in defaults (peak window 3pm–9pm).
FEED_IN_PEAK_DEFAULT = 10.0
FEED_IN_OFF_PEAK_DEFAULT = 2.0
_FEED_IN_PEAK_START = time(15, 0)
_FEED_IN_PEAK_END = time(21, 0)


def _extract_rows(response: dict) -> list[dict]:
    """Accept either the inner IPResult, a {"IPResult": ...} wrapper, or the
    ChartData block itself, and return the list of interval rows."""
    node = response
    if "IPResult" in node:
        node = node["IPResult"]
    if "ChartData" in node:
        node = node["ChartData"]
    rows = node.get("Response")
    if not isinstance(rows, list):
        raise ValueError("No interval Response array found in ChartData payload")
    return rows


def parse_halfhourly_response(response: dict) -> list[dict]:
    """Parse a single-day INTERVAL_DATA response into ordered interval dicts.

    Each returned dict has: timestamp (datetime), consumption_kwh (float),
    export_kwh (float), tariff_name (str).
    """
    intervals = []
    for row in _extract_rows(response):
        val_time = str(row["VAL_TIME"]).zfill(4)
        timestamp = datetime.strptime(f"{row['VAL_DAY']}{val_time}", "%Y-%m-%d%H%M")

        present = [c for c in TARIFF_COLUMNS if c in row and row[c] not in (None, "")]
        consumption = sum(float(row[c]) for c in present)
        tariff_name = present[0] if present else "UNKNOWN"
        export = float(row.get("VAL_SOLAR") or 0.0)

        intervals.append({
            "timestamp": timestamp,
            "consumption_kwh": consumption,
            "export_kwh": export,
            "tariff_name": tariff_name,
        })

    intervals.sort(key=lambda i: i["timestamp"])
    return intervals


def _feed_in_rate(dt: datetime, peak_cents: float, off_peak_cents: float) -> float:
    return peak_cents if _FEED_IN_PEAK_START <= dt.time() < _FEED_IN_PEAK_END else off_peak_cents


def apply_rates(
    intervals: list[dict],
    rates: dict[str, float],
    feed_in_peak_cents: float = FEED_IN_PEAK_DEFAULT,
    feed_in_off_peak_cents: float = FEED_IN_OFF_PEAK_DEFAULT,
) -> list[dict]:
    """Attach cost and feed-in fields to parsed intervals.

    `rates` maps tariff-period name (the API column name) to c/kWh. Unknown
    periods default to 0.0 rather than raising, so a partial/misconfigured rate
    table degrades gracefully. Returns the same dicts, enriched in place, and
    records any unknown period names on each affected interval's tariff rate of
    0.0 (surfaced by the caller via the returned list).
    """
    for interval in intervals:
        rate = rates.get(interval["tariff_name"], 0.0)
        fi_rate = _feed_in_rate(interval["timestamp"], feed_in_peak_cents, feed_in_off_peak_cents)
        interval["rate_cents"] = rate
        interval["cost_cents"] = interval["consumption_kwh"] * rate
        interval["feed_in_rate_cents"] = fi_rate
        interval["export_credit_cents"] = interval["export_kwh"] * fi_rate
    return intervals


def unknown_periods(intervals: list[dict], rates: dict[str, float]) -> set[str]:
    """Return tariff-period names that appear in the data but have no rate."""
    return {
        i["tariff_name"]
        for i in intervals
        if i["tariff_name"] != "UNKNOWN" and i["tariff_name"] not in rates
    }
