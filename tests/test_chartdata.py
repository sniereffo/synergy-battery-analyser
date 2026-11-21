import json
from pathlib import Path
from datetime import datetime

import pytest

from synergy_analyser.chartdata import (
    apply_rates,
    parse_halfhourly_response,
    unpriced_consumption,
)

FIX = Path(__file__).parent / "fixtures"
HALF = json.loads((FIX / "chartdata_halfhourly_day.json").read_text())


def test_parse_halfhourly_returns_48_intervals():
    intervals = parse_halfhourly_response(HALF)
    assert len(intervals) == 48


def test_first_interval_shape():
    first = parse_halfhourly_response(HALF)[0]
    assert first["timestamp"] == datetime(2026, 8, 2, 0, 0)
    assert first["tariff_name"] == "OVERNIGHT"
    assert first["consumption_kwh"] == 0.254
    assert first["export_kwh"] == 0.0


def test_intervals_are_time_ordered_every_30_min():
    intervals = parse_halfhourly_response(HALF)
    assert intervals[1]["timestamp"] == datetime(2026, 8, 2, 0, 30)
    assert intervals[-1]["timestamp"] == datetime(2026, 8, 2, 23, 30)


def test_halfhourly_totals_match_daily_bucket():
    # These per-period daily totals come from the portal's own weekly/monthly
    # bucketed view for 2026-08-02 — the half-hourly rows must sum to them.
    intervals = parse_halfhourly_response(HALF)

    def total(period):
        return round(sum(i["consumption_kwh"] for i in intervals if i["tariff_name"] == period), 3)

    assert total("PEAK") == 3.209
    assert total("OFF_PEAK") == 1.596
    assert total("OFF_PEAK(AM)") == 0.986
    assert total("OVERNIGHT") == 3.195
    assert total("SUPER_OFFPEAK") == 8.311
    assert round(sum(i["export_kwh"] for i in intervals), 3) == 15.811


def test_accepts_full_returnvalue_wrapper():
    # parse should also accept the outer {"IPResult": {...}} shape.
    wrapped = {"IPResult": HALF}
    assert len(parse_halfhourly_response(wrapped)) == 48


def test_apply_rates_sets_cost_and_feed_in():
    intervals = parse_halfhourly_response(HALF)
    rates = {
        "PEAK": 53.8446,
        "OFF_PEAK": 23.6916,
        "OFF_PEAK(AM)": 23.6916,
        "OVERNIGHT": 19.3841,
        "SUPER_OFFPEAK": 8.6151,
    }
    priced = apply_rates(intervals, rates, feed_in_peak_cents=10.0, feed_in_off_peak_cents=2.0)
    first = priced[0]  # 00:00 OVERNIGHT, 0.254 kWh, no solar
    assert first["rate_cents"] == 19.3841
    assert round(first["cost_cents"], 4) == round(0.254 * 19.3841, 4)
    assert first["feed_in_rate_cents"] == 2.0  # 00:00 is off-peak feed-in
    assert first["export_credit_cents"] == 0.0

    # A midday exporting slot earns the off-peak feed-in rate (before 3pm).
    midday = next(i for i in priced if i["timestamp"] == datetime(2026, 8, 2, 12, 0))
    assert midday["feed_in_rate_cents"] == 2.0
    assert round(midday["export_credit_cents"], 4) == round(midday["export_kwh"] * 2.0, 4)


def test_apply_rates_reports_unknown_periods():
    intervals = parse_halfhourly_response(HALF)
    priced = apply_rates(intervals, {"PEAK": 50.0}, feed_in_peak_cents=10.0, feed_in_off_peak_cents=2.0)
    # Unknown periods default to 0.0 rate rather than crashing.
    overnight = priced[0]
    assert overnight["rate_cents"] == 0.0
    assert overnight["cost_cents"] == 0.0


@pytest.mark.parametrize("payload", [
    {},
    {"ChartData": {}},
    {"ChartData": {"Devices": ["0"]}},
    {"IPResult": {}},
    {"IPResult": {"ChartData": {"Response": None}}},
    {"IPResult": "no data", "ChartData": None},
])
def test_payload_without_response_is_zero_intervals(payload):
    # e.g. a range before the move-in date: no data, not an error.
    assert parse_halfhourly_response(payload) == []


@pytest.mark.parametrize("payload", [
    None,
    "oops",
    ["rows"],
    {"ChartData": {"Response": "not a list"}},
])
def test_malformed_payload_raises(payload):
    with pytest.raises(ValueError):
        parse_halfhourly_response(payload)


def test_unpriced_consumption_reports_zero_and_missing_rates():
    intervals = parse_halfhourly_response(HALF)
    rates = {"PEAK": 50.0, "OFF_PEAK": 0.0, "OFF_PEAK(AM)": 20.0, "SUPER_OFFPEAK": 8.0}
    priced = apply_rates(intervals, rates)
    unpriced = unpriced_consumption(priced, rates)
    assert set(unpriced) == {"OFF_PEAK", "OVERNIGHT"}
    assert round(unpriced["OFF_PEAK"], 3) == 1.596
    assert round(unpriced["OVERNIGHT"], 3) == 3.195


def test_unpriced_consumption_ignores_periods_without_usage():
    intervals = [
        {"timestamp": datetime(2026, 8, 2, 0, 0), "consumption_kwh": 0.0,
         "export_kwh": 1.0, "tariff_name": "OVERNIGHT"},
    ]
    assert unpriced_consumption(intervals, {}) == {}
    assert unpriced_consumption(parse_halfhourly_response(HALF), {
        "PEAK": 1.0, "OFF_PEAK": 1.0, "OFF_PEAK(AM)": 1.0, "OVERNIGHT": 1.0, "SUPER_OFFPEAK": 1.0,
    }) == {}
