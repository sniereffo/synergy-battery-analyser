from synergy_analyser.models import (
    DEFAULT_SUPPLY_CHARGE_CENTS,
    EnergyPlan,
    RATE_SCHEDULES,
    display_name,
    plan_for_tariff,
    rate_schedule_for,
    rate_schedules_with_aliases,
)
from synergy_analyser.plans import PLANS

EV = "Electric Vehicle Add On"


def test_rate_schedules_keyed_by_plan_id():
    assert set(RATE_SCHEDULES) == set(PLANS)
    schedule = RATE_SCHEDULES[EV]
    assert schedule["rates"]["PEAK"] == 55.3253
    assert schedule["rates"]["OVERNIGHT"] == 19.9172
    assert schedule["supply_charge_cents"] == 132.7806


def test_every_plan_schedule_covers_its_periods_with_positive_rates():
    for plan_id, plan in PLANS.items():
        rates = RATE_SCHEDULES[plan_id]["rates"]
        for period in plan["periods"]:
            assert rates[period["key"]] > 0, (plan_id, period["key"])
        assert RATE_SCHEDULES[plan_id]["supply_charge_cents"] > 0


def test_rate_schedule_for_accepts_plan_id_or_tariff_label():
    assert rate_schedule_for("EV2 TARIFF") is RATE_SCHEDULES[EV]
    assert rate_schedule_for(EV) is RATE_SCHEDULES[EV]
    assert rate_schedule_for("midday saver") is RATE_SCHEDULES["Midday Saver"]
    assert rate_schedule_for("MYSTERY") == {}
    assert rate_schedule_for(None) == {}


def test_rate_schedules_with_aliases():
    sched = rate_schedules_with_aliases()
    assert sched["EV2 TARIFF"] == RATE_SCHEDULES[EV]
    assert sched["HOME PLAN A1"] == RATE_SCHEDULES["Home Plan A1"]
    for plan_id in PLANS:
        assert sched[plan_id] == RATE_SCHEDULES[plan_id]


def test_display_name_maps_api_columns():
    assert display_name("SUPER_OFFPEAK") == "Super Off Peak"
    assert display_name("OFF_PEAK(AM)") == "Off Peak (AM)"
    # Unknown columns get a sensible title-cased fallback.
    assert display_name("SOMETHING_NEW") == "Something New"
    # Already-display names (from plan projection) are unchanged.
    assert display_name("Off Peak (AM)") == "Off Peak (AM)"


def test_plan_for_tariff_uses_prefill_rates():
    plan = plan_for_tariff("EV2 TARIFF", ["PEAK", "OVERNIGHT", "SUPER_OFFPEAK"])
    assert isinstance(plan, EnergyPlan)
    assert plan.rates["PEAK"] == 55.3253
    assert plan.rates["OVERNIGHT"] == 19.9172
    # Only the periods present are included.
    assert set(plan.rates) == {"PEAK", "OVERNIGHT", "SUPER_OFFPEAK"}
    assert plan.supply_charge_cents == 132.7806


def test_plan_for_tariff_overrides_win():
    plan = plan_for_tariff(
        "EV2 TARIFF", ["PEAK", "OVERNIGHT"], supply_charge_cents=100.0,
        rate_overrides={"PEAK": 60.0},
    )
    assert plan.rates == {"PEAK": 60.0, "OVERNIGHT": 19.9172}
    assert plan.supply_charge_cents == 100.0


def test_plan_for_tariff_base_rates_replace_prefill():
    # e.g. rates scraped from Synergy's site for a tariff we can't re-bin.
    plan = plan_for_tariff(
        "SOME NEW TOU PLAN", ["PEAK", "OFF_PEAK"],
        rate_overrides={"OFF_PEAK": 20.0},
        base_rates={"PEAK": 40.0, "OFF_PEAK": 25.0},
        base_supply_cents=150.0,
    )
    assert plan.rates == {"PEAK": 40.0, "OFF_PEAK": 20.0}
    assert plan.supply_charge_cents == 150.0


def test_plan_for_unknown_tariff_defaults_to_zero_rates():
    plan = plan_for_tariff("MYSTERY PLAN", ["PEAK", "OFF_PEAK"])
    assert plan.rates == {"PEAK": 0.0, "OFF_PEAK": 0.0}
    assert plan.supply_charge_cents == DEFAULT_SUPPLY_CHARGE_CENTS == 119.2419
