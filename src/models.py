from pydantic import BaseModel

from .plans import PLAN_BY_TARIFF, default_plan_for_tariff


# The new Synergy portal labels each half-hourly slot with its tariff period
# (one of these API column names). We no longer categorise by time-of-day; we
# only need a c/kWh rate for each period the data actually contains.
PERIOD_DISPLAY_NAMES = {
    "PEAK": "Peak",
    "OFF_PEAK": "Off Peak",
    "OFF_PEAK(AM)": "Off Peak (AM)",
    "OVERNIGHT": "Overnight",
    "SUPER_OFFPEAK": "Super Off Peak",
}


class EnergyPlan(BaseModel):
    id: str
    name: str
    supply_charge_cents: float           # cents per day
    rates: dict[str, float]              # API period name -> c/kWh


# Built-in prefill defaults keyed by plan id (the names in plans.PLANS). Rates
# are keyed by the API period column names. Values are Synergy's published
# residential prices effective 1 July 2026; they are refreshed live from
# synergy.net.au (see rates.py) when reachable, and are fully editable in the UI.
# Look schedules up via `rate_schedule_for()`, which also accepts the tariff
# label Synergy returns in getLinkedServices ("Tarrif"), e.g. "EV2 TARIFF".
RATE_SCHEDULES: dict[str, dict] = {
    "Electric Vehicle Add On": {
        "supply_charge_cents": 132.7806,
        "rates": {
            "PEAK": 55.3253,
            "OFF_PEAK": 24.3431,
            "OFF_PEAK(AM)": 24.3431,
            "OVERNIGHT": 19.9172,
            "SUPER_OFFPEAK": 8.8520,
        },
    },
    "Midday Saver": {
        "supply_charge_cents": 132.7806,
        "rates": {
            "PEAK": 55.3253,
            "OFF_PEAK": 24.3431,
            "SUPER_OFFPEAK": 8.8520,
        },
    },
    "Home Plan A1": {
        "supply_charge_cents": 119.2419,
        "rates": {
            # A single anytime rate; the portal reports it under whichever
            # period column the account uses.
            "PEAK": 33.2621,
            "OFF_PEAK": 33.2621,
            "OFF_PEAK(AM)": 33.2621,
            "OVERNIGHT": 33.2621,
            "SUPER_OFFPEAK": 33.2621,
        },
    },
}

# Supply charge used when the plan is unknown: the standard Home Plan A1
# daily charge (effective 1 July 2026).
DEFAULT_SUPPLY_CHARGE_CENTS = 119.2419


def rate_schedule_for(name: str | None) -> dict:
    """Prefill schedule for a plan id or a Synergy tariff label ({} if unknown)."""
    plan_id = default_plan_for_tariff(name)
    return RATE_SCHEDULES.get(plan_id, {}) if plan_id else {}


def rate_schedules_with_aliases() -> dict[str, dict]:
    """RATE_SCHEDULES plus an entry per known tariff label (e.g. "EV2 TARIFF"),
    so clients can look a schedule up by either plan id or tariff label."""
    aliases = {
        label: RATE_SCHEDULES[plan_id]
        for label, plan_id in PLAN_BY_TARIFF.items()
        if plan_id in RATE_SCHEDULES
    }
    return {**aliases, **RATE_SCHEDULES}


def display_name(api_period: str) -> str:
    """Human-friendly label for an API tariff-period column name.

    Idempotent: names already in display form (mixed case, e.g. "Off Peak (AM)"
    from plan projection) are returned unchanged.
    """
    if api_period in PERIOD_DISPLAY_NAMES:
        return PERIOD_DISPLAY_NAMES[api_period]
    if api_period.isupper() or "_" in api_period:
        return api_period.replace("_", " ").title()
    return api_period


def plan_for_tariff(
    tariff_label: str | None,
    periods_present: list[str],
    supply_charge_cents: float | None = None,
    rate_overrides: dict[str, float] | None = None,
    base_rates: dict[str, float] | None = None,
    base_supply_cents: float | None = None,
) -> EnergyPlan:
    """Build a plan for the periods that actually appear in the data.

    Rates come from (in priority order) explicit overrides, `base_rates` (e.g.
    scraped from Synergy's site) or else the prefill schedule matching the tariff
    label, then 0.0 (surfaced in the UI for the user to fill). The supply charge
    likewise prefers the explicit value, then `base_supply_cents`, the prefill,
    and finally DEFAULT_SUPPLY_CHARGE_CENTS.
    """
    label = (tariff_label or "").strip().upper()
    schedule = rate_schedule_for(tariff_label)
    if not base_rates:
        base_rates = schedule.get("rates", {})
    overrides = rate_overrides or {}

    rates = {}
    for period in periods_present:
        if period in overrides:
            rates[period] = overrides[period]
        else:
            rates[period] = base_rates.get(period, 0.0)

    if supply_charge_cents is not None:
        supply = supply_charge_cents
    elif base_supply_cents is not None:
        supply = base_supply_cents
    else:
        supply = schedule.get("supply_charge_cents", DEFAULT_SUPPLY_CHARGE_CENTS)
    return EnergyPlan(
        id=label or "custom",
        name=tariff_label or "Custom",
        supply_charge_cents=supply,
        rates=rates,
    )
