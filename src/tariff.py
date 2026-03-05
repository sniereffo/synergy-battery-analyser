from datetime import datetime, time, timedelta
from .models import EnergyPlan, TariffPeriod


# Synergy DEBS (Distributed Energy Buyback Scheme) feed-in rates
FEED_IN_PEAK_DEFAULT = 10.0       # c/kWh, 3pm-9pm
FEED_IN_OFF_PEAK_DEFAULT = 2.0    # c/kWh, all other times
FEED_IN_PEAK_START = time(15, 0)
FEED_IN_PEAK_END = time(21, 0)


def get_feed_in_rate(dt: datetime, peak_rate: float, off_peak_rate: float) -> float:
    """Return the feed-in rate (c/kWh) applicable at the given datetime."""
    t = dt.time()
    if FEED_IN_PEAK_START <= t < FEED_IN_PEAK_END:
        return peak_rate
    return off_peak_rate


def categorise_interval(dt: datetime, plan: EnergyPlan) -> TariffPeriod:
    """Return the tariff period that applies at the given datetime."""
    t = dt.time()

    for period in plan.tariff_periods:
        if _time_in_period(t, period):
            return period

    raise ValueError(f"No tariff period covers {t} in plan {plan.id}")


def _time_in_period(t, period: TariffPeriod) -> bool:
    """Check if a time falls within a tariff period, handling midnight spans."""
    start = period.start_time
    end = period.end_time

    if start == end:
        return True  # Covers all 24 hours (e.g. Home Plan A1 "Anytime")
    elif end < start:
        return t >= start or t < end  # Spans midnight
    else:
        return start <= t < end


def categorise_usage_data(
    consumption_values: list[float | None],
    start_date: datetime,
    plan: EnergyPlan,
    generation_values: list[float | None] | None = None,
    feed_in_peak_cents: float = FEED_IN_PEAK_DEFAULT,
    feed_in_off_peak_cents: float = FEED_IN_OFF_PEAK_DEFAULT,
) -> list[dict]:
    """Categorise each half-hourly value with its tariff period and cost.

    API field names reveal the units:
      kwHalfHourlyValues — kW power readings, convert: kWh = kW × 0.5
      kwhHalfHourlyValuesGeneration — already kWh, no conversion needed
    """
    result = []
    timestamp = start_date

    for i, consumption in enumerate(consumption_values):
        kwh = (consumption if consumption is not None else 0.0) * 0.5
        gen = 0.0
        if generation_values and i < len(generation_values):
            gen = generation_values[i] if generation_values[i] is not None else 0.0

        period = categorise_interval(timestamp, plan)
        cost = kwh * period.rate_cents
        fi_rate = get_feed_in_rate(timestamp, feed_in_peak_cents, feed_in_off_peak_cents)
        export_credit = gen * fi_rate

        result.append({
            "timestamp": timestamp,
            "consumption_kwh": kwh,
            "export_kwh": gen,
            "tariff_name": period.name,
            "rate_cents": period.rate_cents,
            "cost_cents": cost,
            "feed_in_rate_cents": fi_rate,
            "export_credit_cents": export_credit,
        })

        timestamp += timedelta(minutes=30)

    return result
