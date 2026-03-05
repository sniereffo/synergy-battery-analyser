from synergy_analyser.models import (
    EnergyPlan, TariffPeriod, RATE_SCHEDULES,
    get_rate_schedule, build_plan_from_api,
)
from datetime import time


def test_ev_addon_rate_schedule_exists():
    schedule = get_rate_schedule("EV Add on")
    assert "Peak" in schedule
    assert "Overnight" in schedule
    assert schedule["Peak"] == 53.8446


def test_midday_saver_rate_schedule_exists():
    schedule = get_rate_schedule("Midday Saver")
    assert "Peak" in schedule
    assert "Super Off Peak" in schedule


def test_home_plan_a1_rate_schedule_exists():
    schedule = get_rate_schedule("Home Plan (A1)")
    assert "Anytime" in schedule


def test_get_rate_schedule_unknown_raises():
    import pytest
    with pytest.raises(KeyError):
        get_rate_schedule("nonexistent_plan")


def test_build_plan_from_api_response():
    """Build an EnergyPlan from the retrieveIntervalDetails structure."""
    api_details = {
        "productCode": "EV Add on",
        "basePeriod": "OFF PEAK(AM)",
        "period2": "SUPER OFF PEAK",
        "period2Hours": "0900-1500",
        "period2Days": "XXXXXXX",
        "period3": "PEAK",
        "period3Hours": "1500-2100",
        "period3Days": "XXXXXXX",
        "period4": "OFF-PEAK",
        "period4Hours": "2100-2300",
        "period4Days": "XXXXXXX",
        "period5": "OVERNIGHT",
        "period5Hours": "2300-0600",
        "period5Days": "XXXXXXX",
    }
    plan = build_plan_from_api(api_details)
    assert plan.name == "EV Add on"
    assert len(plan.tariff_periods) == 5  # Off Peak AM, Super Off Peak, Peak, Off-Peak PM, Overnight


def test_build_plan_from_api_has_correct_rates():
    api_details = {
        "productCode": "EV Add on",
        "basePeriod": "OFF PEAK(AM)",
        "period2": "SUPER OFF PEAK",
        "period2Hours": "0900-1500",
        "period2Days": "XXXXXXX",
        "period3": "PEAK",
        "period3Hours": "1500-2100",
        "period3Days": "XXXXXXX",
        "period4": "OFF-PEAK",
        "period4Hours": "2100-2300",
        "period4Days": "XXXXXXX",
        "period5": "OVERNIGHT",
        "period5Hours": "2300-0600",
        "period5Days": "XXXXXXX",
    }
    plan = build_plan_from_api(api_details)
    peak = next(p for p in plan.tariff_periods if p.name == "Peak")
    assert peak.rate_cents == 53.8446
    assert peak.start_time == time(15, 0)
    assert peak.end_time == time(21, 0)


def test_all_plan_periods_cover_24_hours():
    """Verify a built plan covers all 48 half-hour slots."""
    api_details = {
        "productCode": "EV Add on",
        "basePeriod": "OFF PEAK(AM)",
        "period2": "SUPER OFF PEAK",
        "period2Hours": "0900-1500",
        "period2Days": "XXXXXXX",
        "period3": "PEAK",
        "period3Hours": "1500-2100",
        "period3Days": "XXXXXXX",
        "period4": "OFF-PEAK",
        "period4Hours": "2100-2300",
        "period4Days": "XXXXXXX",
        "period5": "OVERNIGHT",
        "period5Hours": "2300-0600",
        "period5Days": "XXXXXXX",
    }
    plan = build_plan_from_api(api_details)
    covered_slots = set()
    for period in plan.tariff_periods:
        start_h = period.start_time.hour
        end_h = period.end_time.hour
        if end_h <= start_h:  # spans midnight
            hours = list(range(start_h, 24)) + list(range(0, end_h))
        else:
            hours = list(range(start_h, end_h))
        for h in hours:
            covered_slots.add(h * 2)
            covered_slots.add(h * 2 + 1)
    assert covered_slots == set(range(48))
