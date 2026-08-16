import json
from pathlib import Path

from synergy_analyser.chartdata import parse_halfhourly_response
from synergy_analyser.plans import (
    assign_periods,
    available_plans,
    default_plan_for_tariff,
    rate_map_for_plan,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _intervals():
    raw = json.loads((FIXTURES / "chartdata_halfhourly_day.json").read_text())
    return parse_halfhourly_response(raw)


def test_available_plans_and_default():
    assert "Electric Vehicle Add On" in available_plans()
    assert default_plan_for_tariff("EV2 TARIFF") == "Electric Vehicle Add On"
    assert default_plan_for_tariff("Mystery") is None


def test_ev_windows_reproduce_synergys_own_labels():
    # Re-binning EV2 data with the EV plan's windows must match the period
    # totals Synergy itself reported (validates the window definitions).
    intervals = _intervals()
    # capture the data's own totals first
    data_totals = {}
    for iv in intervals:
        data_totals[iv["tariff_name"]] = data_totals.get(iv["tariff_name"], 0) + iv["consumption_kwh"]

    assign_periods(intervals, "Electric Vehicle Add On")
    plan_totals = {}
    for iv in intervals:
        plan_totals[iv["tariff_name"]] = plan_totals.get(iv["tariff_name"], 0) + iv["consumption_kwh"]

    # API columns map to these display names.
    assert round(plan_totals["Peak"], 3) == round(data_totals["PEAK"], 3)
    assert round(plan_totals["Overnight"], 3) == round(data_totals["OVERNIGHT"], 3)
    assert round(plan_totals["Super Off Peak"], 3) == round(data_totals["SUPER_OFFPEAK"], 3)
    assert round(plan_totals["Off Peak (AM)"], 3) == round(data_totals["OFF_PEAK(AM)"], 3)
    assert round(plan_totals["Off Peak"], 3) == round(data_totals["OFF_PEAK"], 3)


def test_midday_saver_rebins_into_three_periods():
    intervals = _intervals()
    assign_periods(intervals, "Midday Saver")
    names = {iv["tariff_name"] for iv in intervals}
    assert names <= {"Peak", "Super Off Peak", "Off Peak"}
    # Midday Saver folds EV's overnight + morning off-peak into one Off Peak bucket.
    total = sum(iv["consumption_kwh"] for iv in intervals)
    off_peak = sum(iv["consumption_kwh"] for iv in intervals if iv["tariff_name"] == "Off Peak")
    assert off_peak > 0 and off_peak < total


def test_flat_plan_single_period():
    intervals = _intervals()
    assign_periods(intervals, "Home Plan A1")
    assert {iv["tariff_name"] for iv in intervals} == {"Anytime"}


def test_rate_map_translates_keys_to_period_names():
    rates_by_key = {"PEAK": 55.0, "OVERNIGHT": 19.0, "SUPER_OFFPEAK": 8.0,
                    "OFF_PEAK": 24.0, "OFF_PEAK(AM)": 24.0}
    rmap = rate_map_for_plan("Electric Vehicle Add On", rates_by_key)
    assert rmap["Peak"] == 55.0
    assert rmap["Off Peak (AM)"] == 24.0
    assert rmap["Overnight"] == 19.0
