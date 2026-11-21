"""Parse the new Synergy portal's `MyAccount_ChartData` interval responses.

The Vlocity integration procedure returns, for a single day requested with
IntervalType=DAILY / Interval=X / ChartType=INTERVAL_DATA, 48 half-hourly rows.
Each row carries the slot's grid-import kWh under exactly one tariff-period
column (Synergy pre-categorises the period for us), its solar export under
VAL_SOLAR, and the slot start time under VAL_TIME ("HHMM"). Values are already
in kWh — unlike the old API there is no kW→kWh conversion.
"""

from datetime import datetime, time

# The tariff-period columns Synergy may attach to an interval row. Normally
# exactly one is present per row; its value is that slot's grid-import kWh.
TARIFF_COLUMNS = ["PEAK", "OFF_PEAK", "OFF_PEAK(AM)", "OVERNIGHT", "SUPER_OFFPEAK"]

# Row keys that are never grid-import usage. Any *other* numeric column on a
# row with no recognised tariff column is treated as usage in a tariff period
# we don't know about (so it isn't silently dropped from the bill).
_NON_USAGE_COLUMNS = {"VAL_TIME", "VAL_DAY", "VAL_SOLAR", "BillingStatus"}
_NON_USAGE_MARKERS = ("COST", "METER", "STATUS", "TEMP", "DATE")

UNKNOWN_TARIFF = "UNKNOWN"

# Success markers Synergy puts on ChartData (errorCode / error).
_OK_ERROR_CODES = (None, "", "INVOKE-200")
_OK_ERRORS = (None, "", "OK")
_MAX_ERROR_CHARS = 300

# DEBS feed-in defaults (peak window 3pm–9pm).
FEED_IN_PEAK_DEFAULT = 10.0
FEED_IN_OFF_PEAK_DEFAULT = 2.0
_FEED_IN_PEAK_START = time(15, 0)
_FEED_IN_PEAK_END = time(21, 0)


def _extract_rows(response: dict) -> list[dict]:
    """Accept either the inner IPResult, a {"IPResult": ...} wrapper, or the
    ChartData block itself, and return the list of interval rows.

    A payload with no "Response" array (e.g. a range before the move-in date,
    or simply no data yet) yields zero rows rather than an error — unless it
    also carries a non-success errorCode/error, which raises ValueError.
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
        code, err = node.get("errorCode"), node.get("error")
        if code not in _OK_ERROR_CODES or err not in _OK_ERRORS:
            detail = " ".join(str(x) for x in (code, err) if x not in (None, ""))
            if len(detail) > _MAX_ERROR_CHARS:
                detail = detail[:_MAX_ERROR_CHARS] + "…"
            raise ValueError(f"Synergy returned an error instead of interval data: {detail}")
        return []
    if not isinstance(rows, list):
        raise ValueError("ChartData 'Response' is not a list of interval rows")
    return rows


def _is_usage_column(key: str) -> bool:
    if key in _NON_USAGE_COLUMNS or key in TARIFF_COLUMNS:
        return False
    upper = key.upper()
    return not any(m in upper for m in _NON_USAGE_MARKERS)


def _unrecognised_usage(row: dict) -> float:
    """Sum of numeric values in columns that aren't a known tariff period or
    known metadata (0.0 if there are none)."""
    total = 0.0
    for key, value in row.items():
        if not _is_usage_column(str(key)) or value in (None, "") or isinstance(value, bool):
            continue
        try:
            total += float(value)
        except (TypeError, ValueError):
            continue  # non-numeric metadata
    return total


def parse_halfhourly_response(response: dict) -> list[dict]:
    """Parse an INTERVAL_DATA response into ordered interval dicts.

    Each returned dict has: timestamp (datetime), consumption_kwh (float),
    export_kwh (float), tariff_name (str).

    consumption_kwh is the sum of every recognised tariff column on the row;
    if several are present the row is labelled by the largest (ties go to the
    first in TARIFF_COLUMNS order). A row with no recognised tariff column has
    any other numeric usage columns summed into consumption_kwh and is labelled
    "UNKNOWN" — see count_unknown()/unknown_kwh() to report them.
    """
    intervals = []
    for row in _extract_rows(response):
        val_time = str(row["VAL_TIME"]).zfill(4)
        timestamp = datetime.strptime(f"{row['VAL_DAY']}{val_time}", "%Y-%m-%d%H%M")

        values = {
            c: float(row[c]) for c in TARIFF_COLUMNS if c in row and row[c] not in (None, "")
        }
        if values:
            consumption = sum(values.values())
            # max() keeps the first of equal values, i.e. TARIFF_COLUMNS order.
            tariff_name = max(values, key=values.get)
        else:
            consumption = _unrecognised_usage(row)
            tariff_name = UNKNOWN_TARIFF
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


def count_unknown(intervals: list[dict]) -> int:
    """Number of intervals with usage under an unrecognised tariff column
    (labelled "UNKNOWN" by parse_halfhourly_response)."""
    return sum(
        1 for i in intervals
        if i["tariff_name"] == UNKNOWN_TARIFF and i["consumption_kwh"] > 0
    )


def unknown_kwh(intervals: list[dict]) -> float:
    """Total kWh of usage under an unrecognised tariff column."""
    return sum(
        i["consumption_kwh"] for i in intervals
        if i["tariff_name"] == UNKNOWN_TARIFF and i["consumption_kwh"] > 0
    )
