from pydantic import BaseModel
from datetime import time


class TariffPeriod(BaseModel):
    name: str
    rate_cents: float
    start_time: time
    end_time: time  # if end < start, period spans midnight


class EnergyPlan(BaseModel):
    id: str
    name: str
    supply_charge_cents: float  # cents per day
    tariff_periods: list[TariffPeriod]


# Tariff rates per product code.
# The API provides time windows but NOT prices — we define rates here.
# Keys are normalised period names that map to API period names.
RATE_SCHEDULES: dict[str, dict] = {
    "Home Plan (A1)": {
        "supply_charge_cents": 116.0505,
        "rates": {
            "Anytime": 32.3719,
        },
    },
    "Midday Saver": {
        "supply_charge_cents": 129.2269,
        "rates": {
            "Super Off Peak": 8.6151,
            "Peak": 53.8446,
            "Off Peak": 23.6916,
        },
    },
    "EV Add on": {
        "supply_charge_cents": 129.2269,
        "rates": {
            "Super Off Peak": 8.6151,
            "Peak": 53.8446,
            "Off Peak": 23.6916,
            "Overnight": 19.3841,
        },
    },
}

# Map API period names to our normalised rate names
_PERIOD_NAME_MAP = {
    "OFF PEAK(AM)": "Off Peak",
    "SUPER OFF PEAK": "Super Off Peak",
    "PEAK": "Peak",
    "OFF-PEAK": "Off Peak",
    "OVERNIGHT": "Overnight",
}


def get_rate_schedule(product_code: str) -> dict[str, float]:
    return RATE_SCHEDULES[product_code]["rates"]


def _parse_hours(hours_str: str) -> tuple[time, time]:
    """Parse '0900-1500' into (time(9,0), time(15,0))."""
    start_str, end_str = hours_str.split("-")
    start = time(int(start_str[:2]), int(start_str[2:]))
    end = time(int(end_str[:2]), int(end_str[2:]))
    return start, end


def build_plan_from_api(api_details: dict) -> EnergyPlan:
    """Build an EnergyPlan from the API's retrieveIntervalDetails entry."""
    product_code = api_details["productCode"]
    schedule = RATE_SCHEDULES[product_code]
    rates = schedule["rates"]

    periods: list[TariffPeriod] = []

    # Parse numbered periods (period2 through period5+)
    for key in sorted(api_details.keys()):
        if key.startswith("period") and key.endswith("Hours"):
            period_num = key.replace("Hours", "")  # e.g. "period2"
            api_name = api_details[period_num]  # e.g. "SUPER OFF PEAK"
            normalised = _PERIOD_NAME_MAP.get(api_name, api_name)
            rate = rates.get(normalised, 0.0)
            start, end = _parse_hours(api_details[key])
            periods.append(TariffPeriod(
                name=normalised, rate_cents=rate, start_time=start, end_time=end,
            ))

    # Handle basePeriod — it's the gap not covered by numbered periods.
    # For EV Add On: basePeriod is "OFF PEAK(AM)" covering 0600-0900
    base_name = api_details.get("basePeriod", "")
    if base_name:
        normalised_base = _PERIOD_NAME_MAP.get(base_name, base_name)
        rate = rates.get(normalised_base, 0.0)
        # Determine base period hours from the gaps in other periods
        base_start, base_end = _infer_base_period_hours(periods)
        periods.append(TariffPeriod(
            name=normalised_base, rate_cents=rate, start_time=base_start, end_time=base_end,
        ))

    return EnergyPlan(
        id=product_code,
        name=product_code,
        supply_charge_cents=schedule["supply_charge_cents"],
        tariff_periods=periods,
    )


def _infer_base_period_hours(periods: list[TariffPeriod]) -> tuple[time, time]:
    """Find the gap in 24 hours not covered by the given periods."""
    covered = set()
    for p in periods:
        start_h = p.start_time.hour
        end_h = p.end_time.hour
        if end_h <= start_h:
            hours = list(range(start_h, 24)) + list(range(0, end_h))
        else:
            hours = list(range(start_h, end_h))
        covered.update(hours)

    uncovered = sorted(set(range(24)) - covered)
    if not uncovered:
        return time(0, 0), time(0, 0)

    # Find contiguous range
    start_h = uncovered[0]
    end_h = uncovered[-1] + 1
    if end_h == 24:
        end_h = 0
    return time(start_h, 0), time(end_h, 0)
