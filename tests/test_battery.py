import copy

from synergy_analyser.battery import simulate_battery


def test_battery_never_exceeds_capacity(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=15.0, grid_topup=False)
    for entry in result:
        assert 0 <= entry["battery_kwh"] <= 15.0


def test_battery_charges_from_excess_solar(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=15.0, grid_topup=False)
    # The real day has a solar surplus around midday — the battery must gain charge.
    assert max(e["battery_kwh"] for e in result) > 0
    assert sum(e["charge_kwh"] for e in result) > 0


def test_battery_discharges_to_offset_import(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=15.0, grid_topup=False)
    assert sum(e["discharge_kwh"] for e in result) > 0


def test_battery_savings_are_positive(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=15.0, grid_topup=False)
    assert sum(e["savings_cents"] for e in result) > 0


def test_battery_grid_topup_uses_cheapest_tariff(priced_intervals):
    # Remove solar so the battery can only fill via grid top-up.
    no_solar = copy.deepcopy(priced_intervals)
    for e in no_solar:
        e["export_kwh"] = 0.0
        e["export_credit_cents"] = 0.0
    result = simulate_battery(no_solar, capacity_kwh=15.0, grid_topup=True)
    cheapest_rate = min(e["rate_cents"] for e in priced_intervals)
    topups = [e for e in result if e["grid_topup_kwh"] > 0]
    assert topups  # some top-up happened
    for entry in topups:
        assert entry["rate_cents"] == cheapest_rate


def test_zero_capacity_battery_no_savings(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=0.0, grid_topup=False)
    assert sum(e["savings_cents"] for e in result) == 0
