from itertools import groupby


def _topup_windows(
    usage_data: list[dict], cheapest_rate: float, max_per_interval: float
) -> dict[int, tuple[float, float]]:
    """Find the grid top-up slots and the look-ahead needed to size them.

    For each day, the top-up window is the LAST contiguous run of slots priced
    at `cheapest_rate`. Returns {index: (future_solar_kwh, future_topup_cap_kwh)}
    for every slot in those windows, where both values are summed over the
    slots *after* that index within the same window:
      - future_solar_kwh: solar surplus the battery could still absorb
        (capped per slot by the inverter);
      - future_topup_cap_kwh: inverter headroom left for grid top-up in those
        slots once that solar charging is accounted for.
    """
    windows: dict[int, tuple[float, float]] = {}
    indexed = list(enumerate(usage_data))
    for _, day_iter in groupby(indexed, key=lambda ie: ie[1]["timestamp"].date()):
        day = list(day_iter)
        # Walk back from the end of the day to the last cheap-rate run.
        end = len(day) - 1
        while end >= 0 and day[end][1]["rate_cents"] != cheapest_rate:
            end -= 1
        if end < 0:
            continue  # no cheap-rate slots on this day
        start = end
        while start > 0 and day[start - 1][1]["rate_cents"] == cheapest_rate:
            start -= 1
        future_solar = 0.0
        future_cap = 0.0
        for pos in range(end, start - 1, -1):
            idx, entry = day[pos]
            windows[idx] = (future_solar, future_cap)
            solar = min(max(entry["export_kwh"] - entry["consumption_kwh"], 0.0), max_per_interval)
            future_solar += solar
            future_cap += max_per_interval - solar
    return windows


def simulate_battery(
    usage_data: list[dict],
    capacity_kwh: float,
    grid_topup: bool = False,
    inverter_capacity_kw: float = 5.0,
    round_trip_efficiency: float = 0.9,
) -> list[dict]:
    """Simulate battery charge/discharge across half-hourly usage data.

    `usage_data` must be sorted by timestamp.

    - Charging: the battery charges from solar surplus (export that would
      otherwise go to the grid), forfeiting that energy's feed-in credit.
    - Discharging: when consumption exceeds export, the battery covers the
      deficit. Round-trip losses are applied on discharge: delivering D kWh to
      the house drains D / round_trip_efficiency kWh from the battery.
      `discharge_kwh` is the energy delivered to the house.
    - Charge and delivered-discharge energy are capped by the inverter
      capacity (kW * 0.5 h per slot).
    - Grid top-up (optional) buys energy at the cheapest *positive* rate:
        * skipped entirely on flat plans (fewer than two distinct positive
          rates) — there is nothing to arbitrage; 0/missing rates never count
          as free energy;
        * only happens in each day's LAST contiguous run of cheapest-rate
          slots, so the battery is full heading into the dearer periods;
        * is sized "just in time": a slot only tops up the shortfall that the
          remaining slots of that window can't cover, after deducting the
          solar surplus still to come in the window. Within the window, grid
          energy therefore never displaces solar charging, and top-up lands at
          the end of the window. (Deficits later in the window are not
          forecast; if the battery covers them it simply ends the window
          slightly below full.)
        * solar surplus *after* the window is not counted, so the battery can
          be full when afternoon solar arrives and that surplus is exported
          instead. That is deliberate: it is usually feed-in at the DEBS peak
          rate (10c), worth more than the cheap grid energy that replaced it.
          Top-up also fills to capacity regardless of that evening's demand;
          any unused energy carries over to the next day.
        * a slot that tops up does not also discharge — the house draws from
          the grid at the cheap rate while the battery charges.
    """
    if not 0 < round_trip_efficiency <= 1:
        raise ValueError("round_trip_efficiency must be in (0, 1]")

    battery_level = 0.0
    result = []
    max_per_interval = inverter_capacity_kw * 0.5  # kWh per 30-min interval

    # Cheapest positive rate for grid top-up; none on flat/unpriced plans.
    positive_rates = {e["rate_cents"] for e in usage_data if e["rate_cents"] > 0}
    topup_windows: dict[int, tuple[float, float]] = {}
    if grid_topup and len(positive_rates) >= 2:
        topup_windows = _topup_windows(usage_data, min(positive_rates), max_per_interval)

    for idx, entry in enumerate(usage_data):
        consumption = entry["consumption_kwh"]
        export = entry["export_kwh"]
        rate = entry["rate_cents"]
        feed_in_rate = entry["feed_in_rate_cents"]
        net = export - consumption  # positive = surplus available to charge

        charge_kwh = 0.0
        grid_topup_kwh = 0.0
        discharge_kwh = 0.0
        savings_cents = 0.0

        if net > 0:
            # Surplus export — charge the battery (forfeits feed-in credit)
            charge_kwh = max(min(net, capacity_kwh - battery_level, max_per_interval), 0.0)
            battery_level += charge_kwh

        # Grid top-up: only the shortfall later slots in the window can't fill.
        if idx in topup_windows:
            future_solar, future_cap = topup_windows[idx]
            shortfall = capacity_kwh - battery_level - future_solar
            grid_topup_kwh = max(min(
                shortfall - future_cap,
                max_per_interval - charge_kwh,
                capacity_kwh - battery_level,
            ), 0.0)
            battery_level += grid_topup_kwh

        if net < 0 and grid_topup_kwh == 0:
            # Deficit — discharge battery to cover it (losses on discharge)
            discharge_kwh = min(-net, battery_level * round_trip_efficiency, max_per_interval)
            battery_level = max(battery_level - discharge_kwh / round_trip_efficiency, 0.0)
            savings_cents = discharge_kwh * rate

        # Lost feed-in credit = energy captured by battery that would have earned credits
        lost_feed_in_cents = charge_kwh * feed_in_rate

        result.append({
            **entry,
            "battery_kwh": battery_level,
            "charge_kwh": charge_kwh,
            "discharge_kwh": discharge_kwh,
            "grid_topup_kwh": grid_topup_kwh,
            "savings_cents": savings_cents,
            "lost_feed_in_cents": lost_feed_in_cents,
            "grid_topup_cost_cents": grid_topup_kwh * rate,
        })

    return result
