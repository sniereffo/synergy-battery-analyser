import pytest

from synergy_analyser.models import (
    EnergyPlan,
    RATE_SCHEDULES,
    display_name,
    plan_for_tariff,
)


def test_ev2_prefill_schedule_exists():
    schedule = RATE_SCHEDULES["EV2 TARIFF"]
    assert schedule["rates"]["PEAK"] == 53.8446
    assert "OVERNIGHT" in schedule["rates"]


def test_display_name_maps_api_columns():
    assert display_name("SUPER_OFFPEAK") == "Super Off Peak"
    assert display_name("OFF_PEAK(AM)") == "Off Peak (AM)"
    # Unknown columns get a sensible title-cased fallback.
    assert display_name("SOMETHING_NEW") == "Something New"


def test_plan_for_tariff_uses_prefill_rates():
    plan = plan_for_tariff("EV2 TARIFF", ["PEAK", "OVERNIGHT", "SUPER_OFFPEAK"])
    assert isinstance(plan, EnergyPlan)
    assert plan.rates["PEAK"] == 53.8446
    assert plan.rates["OVERNIGHT"] == 19.3841
    # Only the periods present are included.
    assert set(plan.rates) == {"PEAK", "OVERNIGHT", "SUPER_OFFPEAK"}
    assert plan.supply_charge_cents == 129.2269


def test_plan_for_tariff_overrides_win():
    plan = plan_for_tariff(
        "EV2 TARIFF", ["PEAK"], supply_charge_cents=100.0, rate_overrides={"PEAK": 60.0}
    )
    assert plan.rates["PEAK"] == 60.0
    assert plan.supply_charge_cents == 100.0


def test_plan_for_unknown_tariff_defaults_to_zero_rates():
    plan = plan_for_tariff("MYSTERY PLAN", ["PEAK", "OFF_PEAK"])
    assert plan.rates == {"PEAK": 0.0, "OFF_PEAK": 0.0}
    assert plan.supply_charge_cents > 0  # falls back to a default supply charge
