import copy
from datetime import datetime

import pytest

from synergy_analyser.battery import simulate_battery


def _iv(hhmm: str, consumption: float = 0.0, export: float = 0.0,
        rate: float = 30.0, feed_in: float = 2.0) -> dict:
    """A minimal priced interval on 2026-08-02 at HH:MM."""
    h, m = map(int, hhmm.split(":"))
    return {
        "timestamp": datetime(2026, 8, 2, h, m),
        "consumption_kwh": consumption,
        "export_kwh": export,
        "rate_cents": rate,
        "feed_in_rate_cents": feed_in,
    }


def _total(result, key):
    return sum(e[key] for e in result)


def _net_savings(result):
    return (_total(result, "savings_cents") - _total(result, "lost_feed_in_cents")
            - _total(result, "grid_topup_cost_cents"))


# --- fixture-day properties ---

def test_battery_never_exceeds_capacity(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=15.0, grid_topup=False)
    for entry in result:
        assert 0 <= entry["battery_kwh"] <= 15.0


def test_battery_charges_from_excess_solar(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=15.0, grid_topup=False)
    # The real day has a solar surplus around midday — the battery must gain charge.
    assert max(e["battery_kwh"] for e in result) > 0
    assert _total(result, "charge_kwh") > 0


def test_battery_discharges_to_offset_import(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=15.0, grid_topup=False)
    assert _total(result, "discharge_kwh") > 0


def test_battery_savings_are_positive(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=15.0, grid_topup=False)
    assert _total(result, "savings_cents") > 0


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
    # Filled to capacity by the end of the cheap window.
    assert max(e["battery_kwh"] for e in result) == pytest.approx(15.0)


def test_zero_capacity_battery_no_savings(priced_intervals):
    result = simulate_battery(priced_intervals, capacity_kwh=0.0, grid_topup=False)
    assert _total(result, "savings_cents") == 0


def test_default_round_trip_efficiency_is_ninety_percent(priced_intervals):
    default = simulate_battery(copy.deepcopy(priced_intervals), capacity_kwh=15.0)
    explicit = simulate_battery(copy.deepcopy(priced_intervals), capacity_kwh=15.0,
                                round_trip_efficiency=0.9)
    assert [e["discharge_kwh"] for e in default] == [e["discharge_kwh"] for e in explicit]


def test_lossless_battery_discharges_at_least_as_much(priced_intervals):
    lossless = simulate_battery(copy.deepcopy(priced_intervals), 15.0, round_trip_efficiency=1.0)
    lossy = simulate_battery(copy.deepcopy(priced_intervals), 15.0, round_trip_efficiency=0.9)
    assert _total(lossless, "discharge_kwh") >= _total(lossy, "discharge_kwh")
    assert _total(lossless, "savings_cents") >= _total(lossy, "savings_cents")


@pytest.mark.parametrize("eff", [0.0, -0.5, 1.1])
def test_invalid_round_trip_efficiency_raises(priced_intervals, eff):
    with pytest.raises(ValueError):
        simulate_battery(priced_intervals, 15.0, round_trip_efficiency=eff)


# --- hand-computed economics ---

def _solar_then_evening():
    return [
        _iv("10:00", consumption=0.0, export=3.0, rate=10.0, feed_in=2.0),
        _iv("18:00", consumption=1.0, rate=50.0, feed_in=10.0),
        _iv("19:00", consumption=1.0, rate=50.0, feed_in=10.0),
    ]


def test_hand_computed_charge_discharge_with_losses():
    # 2 kWh battery, 90% efficient, inverter 2.5 kWh/slot.
    r = simulate_battery(_solar_then_evening(), capacity_kwh=2.0, round_trip_efficiency=0.9)

    # 10:00: 3 kWh surplus, room for 2 -> charge 2, forfeit 2 kWh x 2c feed-in.
    assert r[0]["charge_kwh"] == pytest.approx(2.0)
    assert r[0]["battery_kwh"] == pytest.approx(2.0)
    assert r[0]["lost_feed_in_cents"] == pytest.approx(4.0)
    # 18:00: 1 kWh deficit -> deliver 1, drain 1/0.9; save 1 x 50c.
    assert r[1]["discharge_kwh"] == pytest.approx(1.0)
    assert r[1]["battery_kwh"] == pytest.approx(2.0 - 1.0 / 0.9)
    assert r[1]["savings_cents"] == pytest.approx(50.0)
    # 19:00: the remaining 0.888.. kWh delivers only 0.8 kWh.
    assert r[2]["discharge_kwh"] == pytest.approx(0.8)
    assert r[2]["battery_kwh"] == pytest.approx(0.0)
    assert r[2]["savings_cents"] == pytest.approx(40.0)

    # Overall: delivered = stored x efficiency; net = 90c - 4c.
    assert _total(r, "discharge_kwh") == pytest.approx(2.0 * 0.9)
    assert _net_savings(r) == pytest.approx(86.0)
    assert _total(r, "grid_topup_kwh") == 0


def test_hand_computed_lossless():
    r = simulate_battery(_solar_then_evening(), capacity_kwh=2.0, round_trip_efficiency=1.0)
    assert [e["discharge_kwh"] for e in r] == pytest.approx([0.0, 1.0, 1.0])
    assert _net_savings(r) == pytest.approx(100.0 - 4.0)


def test_inverter_caps_charge_and_discharge():
    # 1 kW inverter = 0.5 kWh per half-hour slot.
    r = simulate_battery(_solar_then_evening(), capacity_kwh=2.0,
                         inverter_capacity_kw=1.0, round_trip_efficiency=1.0)
    assert r[0]["charge_kwh"] == pytest.approx(0.5)
    assert [e["discharge_kwh"] for e in r[1:]] == pytest.approx([0.5, 0.0])
    assert _net_savings(r) == pytest.approx(25.0 - 1.0)


def test_hand_computed_grid_topup_just_in_time():
    # No solar. Cheap 10c run 00:00-01:00, evening peak at 50c.
    usage = [
        _iv("00:00", rate=10.0),
        _iv("00:30", rate=10.0),
        _iv("01:00", consumption=0.2, rate=10.0),
        _iv("18:00", consumption=3.0, rate=50.0),
        _iv("18:30", consumption=3.0, rate=50.0),
    ]
    r = simulate_battery(usage, capacity_kwh=4.0, grid_topup=True, round_trip_efficiency=1.0)

    # 4 kWh to buy at 2.5 kWh/slot: sized to land at the end of the window.
    assert [e["grid_topup_kwh"] for e in r] == pytest.approx([0.0, 1.5, 2.5, 0.0, 0.0])
    assert r[2]["battery_kwh"] == pytest.approx(4.0)
    # The 01:00 slot tops up, so it does not also discharge for its 0.2 kWh.
    assert r[2]["discharge_kwh"] == 0
    # Evening: 2.5 (inverter cap) then the remaining 1.5.
    assert [e["discharge_kwh"] for e in r[3:]] == pytest.approx([2.5, 1.5])
    assert _total(r, "grid_topup_cost_cents") == pytest.approx(40.0)
    assert _total(r, "savings_cents") == pytest.approx(200.0)
    assert _net_savings(r) == pytest.approx(160.0)


def test_topup_never_buys_at_zero_rate():
    # 00:00-01:00 has no rate (0c) — that must not be treated as free energy.
    usage = [
        _iv("00:00", rate=0.0),
        _iv("00:30", rate=0.0),
        _iv("02:00", rate=10.0),
        _iv("02:30", rate=10.0),
        _iv("18:00", consumption=3.0, rate=50.0),
    ]
    r = simulate_battery(usage, capacity_kwh=4.0, grid_topup=True)
    assert r[0]["grid_topup_kwh"] == 0 and r[1]["grid_topup_kwh"] == 0
    assert _total(r, "grid_topup_kwh") == pytest.approx(4.0)
    assert all(e["rate_cents"] == 10.0 for e in r if e["grid_topup_kwh"] > 0)


@pytest.mark.parametrize("rates", [[30.0, 30.0, 30.0], [0.0, 30.0, 30.0]])
def test_no_topup_on_flat_plan(rates):
    usage = [_iv(t, rate=rate) for t, rate in zip(["00:00", "02:00", "04:00"], rates)]
    usage.append(_iv("18:00", consumption=3.0, rate=30.0))
    r = simulate_battery(usage, capacity_kwh=4.0, grid_topup=True)
    assert _total(r, "grid_topup_kwh") == 0
    assert _total(r, "grid_topup_cost_cents") == 0


def test_topup_in_every_contiguous_cheap_run():
    usage = [
        _iv("01:00", rate=10.0),          # a lone cheap slot is a window too
        _iv("07:00", consumption=0.5, rate=30.0),
        _iv("12:00", rate=10.0),          # second cheap run of the day
        _iv("12:30", rate=10.0),
        _iv("18:00", consumption=3.0, rate=50.0),
    ]
    r = simulate_battery(usage, capacity_kwh=3.0, grid_topup=True, round_trip_efficiency=1.0)
    # 01:00: fills as far as one slot of inverter allows.
    assert r[0]["grid_topup_kwh"] == pytest.approx(2.5)
    assert r[1]["discharge_kwh"] == pytest.approx(0.5)
    # 12:00-12:30: only the 1 kWh shortfall, bought in the window's last slot.
    assert [e["grid_topup_kwh"] for e in r[2:4]] == pytest.approx([0.0, 1.0])
    assert r[3]["battery_kwh"] == pytest.approx(3.0)
    assert r[4]["discharge_kwh"] == pytest.approx(2.5)


def _at(day: int, hhmm: str, **kw) -> dict:
    iv = _iv(hhmm, **kw)
    iv["timestamp"] = iv["timestamp"].replace(day=day)
    return iv


def test_topup_window_wrapping_midnight_is_one_window():
    # Cheap 23:00-01:00 across midnight: one window, topped up at its end.
    usage = [
        _at(2, "23:00", rate=10.0),
        _at(2, "23:30", rate=10.0),
        _at(3, "00:00", rate=10.0),
        _at(3, "00:30", rate=10.0),
        _at(3, "18:00", consumption=3.0, rate=50.0),
    ]
    r = simulate_battery(usage, capacity_kwh=4.0, grid_topup=True, round_trip_efficiency=1.0)
    assert [e["grid_topup_kwh"] for e in r] == pytest.approx([0.0, 0.0, 1.5, 2.5, 0.0])
    assert r[3]["battery_kwh"] == pytest.approx(4.0)
    assert r[4]["discharge_kwh"] == pytest.approx(2.5)
    assert _total(r, "grid_topup_cost_cents") == pytest.approx(40.0)


def test_topup_run_breaks_at_a_gap_in_the_data():
    # Adjacent in the list but a day apart (missing day): two windows, so the
    # first one fills just-in-time at its own end and the second has no room.
    usage = [
        _at(2, "23:00", rate=10.0),
        _at(2, "23:30", rate=10.0),
        _at(4, "00:00", rate=10.0),
        _at(4, "00:30", rate=10.0),
        _at(4, "18:00", consumption=3.0, rate=50.0),
    ]
    r = simulate_battery(usage, capacity_kwh=4.0, grid_topup=True, round_trip_efficiency=1.0)
    assert [e["grid_topup_kwh"] for e in r] == pytest.approx([1.5, 2.5, 0.0, 0.0, 0.0])


def _charged_then_cheap_load():
    return [
        _iv("17:00", export=2.0, rate=30.0),               # charge 2 kWh from solar
        _iv("18:00", consumption=0.5, rate=50.0),          # discharge 0.5
        _iv("23:00", consumption=1.0, rate=10.0),          # cheapest-rate load
        _iv("23:30", rate=10.0),
    ]


def test_no_discharge_in_cheap_window_when_topup_on():
    r = simulate_battery(_charged_then_cheap_load(), capacity_kwh=4.0, grid_topup=True,
                         round_trip_efficiency=1.0)
    assert r[1]["discharge_kwh"] == pytest.approx(0.5)
    # 23:00: the grid serves the 1 kWh at 10c; the battery is left alone...
    assert r[2]["discharge_kwh"] == 0
    assert r[2]["savings_cents"] == 0
    # ...and only the 2.5 kWh shortfall is bought, at the window's end.
    assert [e["grid_topup_kwh"] for e in r[2:]] == pytest.approx([0.0, 2.5])
    assert r[3]["battery_kwh"] == pytest.approx(4.0)


def test_cheap_window_discharge_unchanged_when_topup_off():
    r = simulate_battery(_charged_then_cheap_load(), capacity_kwh=4.0, grid_topup=False,
                         round_trip_efficiency=1.0)
    assert r[2]["discharge_kwh"] == pytest.approx(1.0)
    assert r[2]["savings_cents"] == pytest.approx(10.0)
    assert r[3]["battery_kwh"] == pytest.approx(0.5)
    assert _total(r, "grid_topup_kwh") == 0


def test_fixture_day_never_discharges_at_cheapest_rate_with_topup(priced_intervals):
    cheapest = min(e["rate_cents"] for e in priced_intervals if e["rate_cents"] > 0)
    on = simulate_battery(copy.deepcopy(priced_intervals), 10.0, grid_topup=True)
    off = simulate_battery(copy.deepcopy(priced_intervals), 10.0, grid_topup=False)
    assert all(e["discharge_kwh"] == 0 for e in on if e["rate_cents"] == cheapest)
    # Without top-up the battery does cover some cheapest-rate load that day.
    assert sum(e["discharge_kwh"] for e in off if e["rate_cents"] == cheapest) > 0


def test_topup_slot_never_discharges(priced_intervals):
    for cap in (5.0, 15.0, 30.0):
        r = simulate_battery(copy.deepcopy(priced_intervals), cap, grid_topup=True)
        for e in r:
            assert not (e["grid_topup_kwh"] > 0 and e["discharge_kwh"] > 0), e["timestamp"]
            assert e["battery_kwh"] <= cap + 1e-9


def test_topup_does_not_displace_solar_in_the_cheap_window(priced_intervals):
    """On the fixture day the cheapest (Super Off Peak, 09:00-15:00) window is
    also the solar window. Grid top-up must only buy what solar can't fill:
    every slot up to the end of the window charges exactly as much solar as
    without top-up, and solar + grid never overfill the battery."""
    cap = 15.0
    off = simulate_battery(copy.deepcopy(priced_intervals), cap, grid_topup=False)
    on = simulate_battery(copy.deepcopy(priced_intervals), cap, grid_topup=True)

    window_end = datetime(2026, 8, 2, 15, 0)
    for a, b in zip(off, on):
        if a["timestamp"] < window_end:
            assert b["charge_kwh"] == pytest.approx(a["charge_kwh"]), a["timestamp"]
    topups = [e for e in on if e["grid_topup_kwh"] > 0]
    assert topups
    assert all(e["timestamp"] < window_end for e in topups)
    # Top-up starts only after the solar run has already charged the battery.
    first_topup = topups[0]["timestamp"]
    solar_before = sum(e["charge_kwh"] for e in on if e["timestamp"] < first_topup)
    assert solar_before > 5.0
    assert max(e["battery_kwh"] for e in on) <= cap + 1e-9
    assert on[-1]["battery_kwh"] >= off[-1]["battery_kwh"]


def test_topup_buys_nothing_when_solar_already_fills(priced_intervals):
    # 10 kWh: the fixture day's solar fills it in the window, so no grid top-up.
    off = simulate_battery(copy.deepcopy(priced_intervals), 10.0, grid_topup=False)
    on = simulate_battery(copy.deepcopy(priced_intervals), 10.0, grid_topup=True)
    assert _total(on, "grid_topup_kwh") == 0
    assert _total(on, "grid_topup_cost_cents") == 0
    # Not discharging in the cheap window leaves less room for solar, never more.
    assert _total(on, "charge_kwh") <= _total(off, "charge_kwh") + 1e-9


@pytest.mark.parametrize("kwargs", [
    {"capacity_kwh": -1.0},
    {"capacity_kwh": 10.0, "inverter_capacity_kw": 0.0},
    {"capacity_kwh": 10.0, "inverter_capacity_kw": -1.0},
])
def test_invalid_capacity_or_inverter_raises(priced_intervals, kwargs):
    with pytest.raises(ValueError):
        simulate_battery(priced_intervals, **kwargs)
