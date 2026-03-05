from synergy_analyser.battery import simulate_battery
from synergy_analyser.tariff import categorise_usage_data
from synergy_analyser.models import build_plan_from_api
from datetime import datetime


def _build_usage(sample_api_response, sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    consumption = sample_api_response["kwHalfHourlyValues"]
    generation = sample_api_response["kwhHalfHourlyValuesGeneration"]
    start = datetime(2025, 4, 1)
    return categorise_usage_data(consumption, start, plan, generation)


def test_battery_never_exceeds_capacity(sample_api_response, sample_ev_plan_details):
    usage = _build_usage(sample_api_response, sample_ev_plan_details)
    result = simulate_battery(usage, capacity_kwh=15.0, grid_topup=False)
    for entry in result:
        assert 0 <= entry["battery_kwh"] <= 15.0


def test_battery_charges_from_excess_solar(sample_api_response, sample_ev_plan_details):
    usage = _build_usage(sample_api_response, sample_ev_plan_details)
    result = simulate_battery(usage, capacity_kwh=15.0, grid_topup=False)
    # Battery should have charge after solar hours (by 3pm, slot 30)
    assert result[30]["battery_kwh"] > 0


def test_battery_discharges_during_peak(sample_api_response, sample_ev_plan_details):
    usage = _build_usage(sample_api_response, sample_ev_plan_details)
    result = simulate_battery(usage, capacity_kwh=15.0, grid_topup=False)
    # Battery at 9pm (slot 42) should be less than at 3pm (slot 30)
    assert result[42]["battery_kwh"] < result[30]["battery_kwh"]


def test_battery_savings_are_positive(sample_api_response, sample_ev_plan_details):
    usage = _build_usage(sample_api_response, sample_ev_plan_details)
    result = simulate_battery(usage, capacity_kwh=15.0, grid_topup=False)
    total_savings = sum(e["savings_cents"] for e in result)
    assert total_savings > 0


def test_battery_grid_topup_uses_cheapest_tariff(sample_api_response, sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    # Reduce export so battery doesn't fill from solar alone
    low_gen = [v * 0.05 if v else 0.0 for v in sample_api_response["kwhHalfHourlyValuesGeneration"]]
    consumption = sample_api_response["kwHalfHourlyValues"]
    start = datetime(2025, 4, 1)
    usage = categorise_usage_data(consumption, start, plan, low_gen)

    result = simulate_battery(usage, capacity_kwh=15.0, grid_topup=True)
    cheapest_rate = min(p.rate_cents for p in plan.tariff_periods)
    for entry in result:
        if entry["grid_topup_kwh"] > 0:
            assert entry["rate_cents"] == cheapest_rate


def test_zero_capacity_battery_no_savings(sample_api_response, sample_ev_plan_details):
    usage = _build_usage(sample_api_response, sample_ev_plan_details)
    result = simulate_battery(usage, capacity_kwh=0.0, grid_topup=False)
    total_savings = sum(e["savings_cents"] for e in result)
    assert total_savings == 0
