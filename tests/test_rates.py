from pathlib import Path

from synergy_analyser.rates import (
    map_label_to_periods,
    parse_rates_html,
    plan_slug_for_tariff,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_map_label_to_periods():
    assert map_label_to_periods("Super Off Peak") == ["SUPER_OFFPEAK"]
    assert map_label_to_periods("Overnight") == ["OVERNIGHT"]
    # EV off-peak covers both the AM and PM off-peak columns at one rate.
    assert set(map_label_to_periods("Everyday EV off peak")) == {"OFF_PEAK", "OFF_PEAK(AM)"}
    assert map_label_to_periods("Standard home") == ["PEAK"]
    # Leading noise from tag-stripping must not break keyword matching.
    assert map_label_to_periods("unit Overnight") == ["OVERNIGHT"]


def test_parse_rates_html_extracts_ev_rates():
    html = (FIXTURES / "synergy_ev_plan.html").read_text()
    parsed = parse_rates_html(html)
    assert parsed["supply_charge_cents"] == 132.7806
    r = parsed["rates"]
    assert r["PEAK"] == 55.3253
    assert r["SUPER_OFFPEAK"] == 8.8520
    assert r["OVERNIGHT"] == 19.9172
    assert r["OFF_PEAK"] == 24.3431
    assert r["OFF_PEAK(AM)"] == 24.3431


def test_parse_flat_plan_spreads_single_rate():
    html = "<td>Supply charge 119.2419 cents per day</td><td>Electricity charge 33.2621 cents per unit</td>"
    parsed = parse_rates_html(html)
    assert parsed["supply_charge_cents"] == 119.2419
    # A single anytime rate applies to every period column.
    assert parsed["rates"] == {
        "PEAK": 33.2621, "OFF_PEAK": 33.2621, "OFF_PEAK(AM)": 33.2621,
        "OVERNIGHT": 33.2621, "SUPER_OFFPEAK": 33.2621,
    }


def test_plan_slug_for_tariff():
    assert plan_slug_for_tariff("EV2 TARIFF") == "Electric-Vehicle-Add-On"
    assert plan_slug_for_tariff("midday saver") == "Midday-Saver"
    assert plan_slug_for_tariff("Unknown Plan XYZ") is None
