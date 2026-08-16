from pydantic import BaseModel


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


# Prefill defaults keyed by the tariff label Synergy returns in getLinkedServices
# ("Tarrif"). These are best-known starting values shown in the UI and are fully
# editable; the rates module refines them from Synergy's published pricing when
# available. Rates are keyed by the API period column names.
RATE_SCHEDULES: dict[str, dict] = {
    "EV2 TARIFF": {
        "supply_charge_cents": 129.2269,
        "rates": {
            "PEAK": 53.8446,
            "OFF_PEAK": 23.6916,
            "OFF_PEAK(AM)": 23.6916,
            "OVERNIGHT": 19.3841,
            "SUPER_OFFPEAK": 8.6151,
        },
    },
    "MIDDAY SAVER": {
        "supply_charge_cents": 129.2269,
        "rates": {
            "PEAK": 53.8446,
            "OFF_PEAK": 23.6916,
            "SUPER_OFFPEAK": 8.6151,
        },
    },
    "HOME PLAN A1": {
        "supply_charge_cents": 116.0505,
        "rates": {
            # A single anytime rate; the portal reports it under whichever
            # period column the account uses.
            "PEAK": 32.3719,
            "OFF_PEAK": 32.3719,
            "OFF_PEAK(AM)": 32.3719,
            "OVERNIGHT": 32.3719,
            "SUPER_OFFPEAK": 32.3719,
        },
    },
}

DEFAULT_SUPPLY_CHARGE_CENTS = 116.0505


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
) -> EnergyPlan:
    """Build a plan for the periods that actually appear in the data.

    Rates come from (in priority order) explicit overrides, the prefill schedule
    matching the tariff label, then 0.0 (surfaced in the UI for the user to fill).
    """
    label = (tariff_label or "").strip().upper()
    schedule = RATE_SCHEDULES.get(label, {})
    base_rates = schedule.get("rates", {})
    overrides = rate_overrides or {}

    rates = {}
    for period in periods_present:
        if period in overrides:
            rates[period] = overrides[period]
        else:
            rates[period] = base_rates.get(period, 0.0)

    supply = (
        supply_charge_cents
        if supply_charge_cents is not None
        else schedule.get("supply_charge_cents", DEFAULT_SUPPLY_CHARGE_CENTS)
    )
    return EnergyPlan(
        id=label or "custom",
        name=tariff_label or "Custom",
        supply_charge_cents=supply,
        rates=rates,
    )
