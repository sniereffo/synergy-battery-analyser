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
    ChartData block itself, and return the list of interval rows.

    A payload with no "Response" array (e.g. a range before the move-in date,
    or simply no data yet) yields zero rows rather than an error.
    """
    node = response
    if not isinstance(node, dict):
        raise ValueError(f"Unexpected ChartData payload type: {type(node).__name__}")
    if isinstance(node.get("IPResult"), dict):
        node = node["IPResult"]
    if isinstance(node.get("ChartData"), dict):
        node = node["ChartData"]
    rows = node.get("Response")
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise ValueError("ChartData 'Response' is not a list of interval rows")
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
    table degrades gracefully; use `unpriced_consumption()` to report them.
    Returns the same dicts, enriched in place.
    """
    for interval in intervals:
        rate = rates.get(interval["tariff_name"], 0.0)
        fi_rate = _feed_in_rate(interval["timestamp"], feed_in_peak_cents, feed_in_off_peak_cents)
        interval["rate_cents"] = rate
        interval["cost_cents"] = interval["consumption_kwh"] * rate
        interval["feed_in_rate_cents"] = fi_rate
        interval["export_credit_cents"] = interval["export_kwh"] * fi_rate
    return intervals


def unpriced_consumption(intervals: list[dict], rates: dict[str, float]) -> dict[str, float]:
    """Return {tariff_name: kWh} for periods with consumption but no (or a 0)
    rate, so the caller can warn that this usage was priced at 0c."""
    unpriced: dict[str, float] = {}
    for i in intervals:
        name = i["tariff_name"]
        if i["consumption_kwh"] > 0 and not rates.get(name):
            unpriced[name] = unpriced.get(name, 0.0) + i["consumption_kwh"]
    return unpriced
