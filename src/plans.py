"""Plan definitions for tariff projection ("what if I switched plans?").

The portal labels each half-hourly slot with the tariff period of the user's
*current* plan. To project a different plan we re-bin each slot into that plan's
time-of-use windows (using the slot's timestamp) and price it with that plan's
rates. Windows below are taken from Synergy's published plan pages and together
cover a full 24 hours with no overlap.

Each period's `key` indexes into a rates dict keyed by the API period-column
names (as produced by rates.parse_rates_html), so scraped/override rates apply
directly.
"""

from datetime import time


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


# name: human label; key: rate-dict key; windows: [(start, end)] end-exclusive,
# end < start wraps midnight, ("00:00","00:00") means all day. Public plan-page
# slugs live in rates.py (the single source of truth for scraping).
PLANS: dict[str, dict] = {
    "Electric Vehicle Add On": {
        "periods": [
            {"name": "Peak", "key": "PEAK", "windows": [("15:00", "21:00")]},
            {"name": "Off Peak (AM)", "key": "OFF_PEAK(AM)", "windows": [("06:00", "09:00")]},
            {"name": "Off Peak", "key": "OFF_PEAK", "windows": [("21:00", "23:00")]},
            {"name": "Super Off Peak", "key": "SUPER_OFFPEAK", "windows": [("09:00", "15:00")]},
            {"name": "Overnight", "key": "OVERNIGHT", "windows": [("23:00", "06:00")]},
        ],
    },
    "Midday Saver": {
        "periods": [
            {"name": "Peak", "key": "PEAK", "windows": [("15:00", "21:00")]},
            {"name": "Super Off Peak", "key": "SUPER_OFFPEAK", "windows": [("09:00", "15:00")]},
            {"name": "Off Peak", "key": "OFF_PEAK", "windows": [("21:00", "09:00")]},
        ],
    },
    "Home Plan A1": {
        "periods": [
            {"name": "Anytime", "key": "PEAK", "windows": [("00:00", "00:00")]},
        ],
    },
}

# Map the tariff label from getLinkedServices to the plan a customer is on.
PLAN_BY_TARIFF = {
    "EV2 TARIFF": "Electric Vehicle Add On",
    "ELECTRIC VEHICLE ADD ON": "Electric Vehicle Add On",
    "MIDDAY SAVER": "Midday Saver",
    "HOME PLAN A1": "Home Plan A1",
    "HOME PLAN (A1)": "Home Plan A1",
    "A1": "Home Plan A1",
}


def available_plans() -> list[str]:
    return list(PLANS)


def default_plan_for_tariff(tariff_label: str | None) -> str | None:
    """Plan id for a Synergy tariff label or a plan name (case-insensitive)."""
    key = (tariff_label or "").strip().upper()
    if not key:
        return None
    if key in PLAN_BY_TARIFF:
        return PLAN_BY_TARIFF[key]
    return next((pid for pid in PLANS if pid.upper() == key), None)


def plan_periods(plan_id: str) -> list[dict]:
    """The plan's periods as [{name, key}] (key = API period column)."""
    return [{"name": p["name"], "key": p["key"]} for p in PLANS[plan_id]["periods"]]


def plan_details() -> dict[str, dict]:
    """Per-plan period list and whether it is a flat (single-rate) plan."""
    return {
        pid: {"periods": plan_periods(pid), "flat": len(PLANS[pid]["periods"]) == 1}
        for pid in PLANS
    }


def _prepared_periods(plan_id: str) -> list[dict]:
    periods = []
    for p in PLANS[plan_id]["periods"]:
        periods.append({
            **p,
            "windows_t": [(_t(s), _t(e)) for s, e in p["windows"]],
        })
    return periods


def _in_window(t: time, start: time, end: time) -> bool:
    if start == end:
        return True  # all day
    if end < start:
        return t >= start or t < end  # wraps midnight
    return start <= t < end


def period_for_time(t: time, periods: list[dict]) -> dict | None:
    for p in periods:
        if any(_in_window(t, s, e) for s, e in p["windows_t"]):
            return p
    return None


def assign_periods(intervals: list[dict], plan_id: str) -> list[dict]:
    """Set each interval's tariff_name to its period under the given plan."""
    periods = _prepared_periods(plan_id)
    for iv in intervals:
        p = period_for_time(iv["timestamp"].time(), periods)
        iv["tariff_name"] = p["name"] if p else "UNKNOWN"
    return intervals


def rate_map_for_plan(plan_id: str, rates_by_key: dict[str, float]) -> dict[str, float]:
    """Translate rates keyed by API period column into rates keyed by the plan's
    period display names (what assign_periods writes)."""
    return {
        p["name"]: rates_by_key.get(p["key"], 0.0)
        for p in PLANS[plan_id]["periods"]
    }
