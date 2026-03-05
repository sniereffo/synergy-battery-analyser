from datetime import datetime, time
from synergy_analyser.tariff import categorise_interval, categorise_usage_data
from synergy_analyser.models import build_plan_from_api, EnergyPlan, TariffPeriod


def test_categorise_peak_interval(sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    dt = datetime(2025, 4, 1, 17, 0)  # 5:00 PM
    period = categorise_interval(dt, plan)
    assert period.name == "Peak"


def test_categorise_overnight_interval(sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    dt = datetime(2025, 4, 1, 2, 0)  # 2:00 AM
    period = categorise_interval(dt, plan)
    assert period.name == "Overnight"


def test_categorise_super_off_peak_interval(sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    dt = datetime(2025, 4, 1, 12, 0)  # noon
    period = categorise_interval(dt, plan)
    assert period.name == "Super Off Peak"


def test_categorise_off_peak_morning(sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    dt = datetime(2025, 4, 1, 7, 30)  # 7:30 AM
    period = categorise_interval(dt, plan)
    assert period.name == "Off Peak"


def test_categorise_off_peak_evening(sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    dt = datetime(2025, 4, 1, 22, 0)  # 10:00 PM
    period = categorise_interval(dt, plan)
    assert period.name == "Off Peak"


def test_categorise_boundary_11pm_is_overnight(sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    dt = datetime(2025, 4, 1, 23, 0)
    period = categorise_interval(dt, plan)
    assert period.name == "Overnight"


def test_categorise_usage_data_length(sample_api_response, sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    consumption = sample_api_response["kwHalfHourlyValues"]
    generation = sample_api_response["kwhHalfHourlyValuesGeneration"]
    start = datetime(2025, 4, 1)

    result = categorise_usage_data(consumption, start, plan, generation)
    assert len(result) == 48


def test_categorise_usage_data_fields(sample_api_response, sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    consumption = sample_api_response["kwHalfHourlyValues"]
    generation = sample_api_response["kwhHalfHourlyValuesGeneration"]
    start = datetime(2025, 4, 1)

    result = categorise_usage_data(consumption, start, plan, generation)
    entry = result[0]
    assert "timestamp" in entry
    assert "consumption_kwh" in entry
    assert "export_kwh" in entry
    assert "tariff_name" in entry
    assert "rate_cents" in entry
    assert "cost_cents" in entry


def test_peak_slots_have_peak_rate(sample_api_response, sample_ev_plan_details):
    plan = build_plan_from_api(sample_ev_plan_details)
    consumption = sample_api_response["kwHalfHourlyValues"]
    generation = sample_api_response["kwhHalfHourlyValuesGeneration"]
    start = datetime(2025, 4, 1)

    result = categorise_usage_data(consumption, start, plan, generation)
    # Slot 30 = 15:00 (first Peak slot)
    assert result[30]["tariff_name"] == "Peak"
    assert result[30]["rate_cents"] == 53.8446


def test_home_plan_a1_anytime_all_hours():
    """Home Plan A1 has a single 'Anytime' period covering every hour of the day."""
    plan = EnergyPlan(
        id="home_plan_a1",
        name="Home Plan (A1)",
        supply_charge_cents=116.0505,
        tariff_periods=[
            TariffPeriod(
                name="Anytime",
                rate_cents=32.3719,
                start_time=time(0, 0),
                end_time=time(0, 0),
            )
        ],
    )
    for hour in range(24):
        dt = datetime(2025, 4, 1, hour, 0)
        period = categorise_interval(dt, plan)
        assert period.name == "Anytime", f"Expected 'Anytime' at {hour}:00, got '{period.name}'"
