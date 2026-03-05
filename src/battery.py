def simulate_battery(
    usage_data: list[dict],
    capacity_kwh: float,
    grid_topup: bool = False,
    inverter_capacity_kw: float = 5.0,
) -> list[dict]:
    """Simulate battery charge/discharge across half-hourly usage data.

    Battery charges from solar export (energy that would otherwise go to grid).
    Charging from export forfeits the feed-in credit for that energy.
    Optionally tops up from grid during the cheapest tariff period.
    Discharges when consumption exceeds export (net grid draw).
    Charge and discharge rates are capped by the inverter capacity.
    """
    battery_level = 0.0
    result = []
    max_per_interval = inverter_capacity_kw * 0.5  # kWh per 30-min interval

    # Find cheapest tariff rate for grid top-up
    all_rates = {e["rate_cents"] for e in usage_data}
    cheapest_rate = min(all_rates) if all_rates else 0.0

    for entry in usage_data:
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
            charge_kwh = min(net, capacity_kwh - battery_level, max_per_interval)
            battery_level += charge_kwh
        elif net < 0:
            # Deficit — discharge battery to cover it
            needed = abs(net)
            discharge_kwh = min(needed, battery_level, max_per_interval)
            battery_level -= discharge_kwh
            savings_cents = discharge_kwh * rate

        # Grid top-up: charge from grid during cheapest tariff if battery isn't full
        if grid_topup and rate == cheapest_rate and battery_level < capacity_kwh:
            remaining_charge = max_per_interval - charge_kwh  # respect rate limit
            topup = min(capacity_kwh - battery_level, remaining_charge)
            grid_topup_kwh = topup
            battery_level += topup

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
