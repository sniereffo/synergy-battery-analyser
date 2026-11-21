import json
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import synergy_analyser.main as main
from synergy_analyser.browser_client import NotLoggedIn, SynergyBrowserClient
from synergy_analyser.chartdata import parse_halfhourly_response
from synergy_analyser.main import app
from synergy_analyser.models import RATE_SCHEDULES
from synergy_analyser.plans import PLAN_BY_TARIFF, PLANS, plan_periods

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_DAY = date(2026, 8, 2)
EV = "Electric Vehicle Add On"
EV_SCHEDULE = RATE_SCHEDULES[EV]

# Per-period kWh Synergy reported for the fixture day (see test_chartdata).
FIXTURE_CONSUMPTION_KWH = 3.209 + 1.596 + 0.986 + 3.195 + 8.311


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


# --- helpers ---

def _day_intervals(day: date = FIXTURE_DAY) -> list[dict]:
    """The fixture day's 48 parsed intervals, moved onto `day`."""
    raw = json.loads((FIXTURES / "chartdata_halfhourly_day.json").read_text())
    shift = day - FIXTURE_DAY
    return [{**i, "timestamp": i["timestamp"] + shift} for i in parse_halfhourly_response(raw)]


def _write_cache(data_dir: Path, name: str, days: list[date], tariff_label="EV2 TARIFF",
                 intervals: list[dict] | None = None) -> str:
    """Write a cached-intervals file in the shape a live fetch produces."""
    if intervals is None:
        intervals = [iv for d in days for iv in _day_intervals(d)]
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / name).write_text(json.dumps({
        "tariff_label": tariff_label,
        "intervals": [{**i, "timestamp": i["timestamp"].isoformat()} for i in intervals],
    }))
    return name


def _days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=k) for k in range(n)]


PROJECTION_PREFIX = "Savings and payback are projected to 12 months"


def _projection_warnings(data) -> list[str]:
    return [w for w in data["warnings"] if w.startswith(PROJECTION_PREFIX)]


def _other_warnings(data) -> list[str]:
    """Warnings other than the short-range 12-month projection caveat."""
    return [w for w in data["warnings"] if not w.startswith(PROJECTION_PREFIX)]


def _poll_job(client, timeout=10.0):
    """Poll the analysis job until it finishes; return the final job state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get("/api/analyse/progress").json()
        if job["status"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise AssertionError("job did not finish in time")


def _run(client, **body) -> dict:
    body.setdefault("battery_capacity_kwh", 15.0)
    started = client.post("/api/analyse", json=body)
    assert started.status_code == 200, started.text
    return _poll_job(client)


def _analyse(client, **body) -> dict:
    job = _run(client, **body)
    assert job["status"] == "done", job.get("error")
    return job["result"]


def _expected_rates(plan_id: str, rates_by_key: dict) -> dict:
    """Plan rates keyed by period display name, as the result reports them."""
    return {p["name"]: rates_by_key[p["key"]] for p in PLANS[plan_id]["periods"]}


class FakePage:
    def __init__(self, closed=False):
        self.closed = closed

    def is_closed(self):
        return self.closed


class FakeClient:
    """Stand-in for SynergyBrowserClient (no browser)."""

    def __init__(self, logged_in=True, intervals=None, range_error=None, services=None,
                 services_error=None, window_open=False):
        self.logged_in = logged_in
        self.window_open = window_open
        self._intervals = intervals or []
        self._range_error = range_error
        self._services = services or []
        self._services_error = services_error
        self.range_calls = []
        self.services_calls = 0
        self.finalize_calls = 0
        self.closed = False

    async def get_range(self, service_id, start, end, progress=None):
        self.range_calls.append((service_id, start, end))
        if self._range_error:
            raise self._range_error
        return self._intervals

    async def get_services(self):
        self.services_calls += 1
        if self._services_error:
            raise self._services_error
        return self._services

    async def finalize_login(self):
        self.finalize_calls += 1

    async def close(self):
        self.closed = True


# --- basic routes ---

def test_health_endpoint(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "app": "synergy-analyser", "can_quit": False}


def test_quit_needs_the_launcher(client):
    # Run under uvicorn/tests there is nothing to stop it with.
    r = client.post("/api/quit", json={})
    assert r.status_code == 409


def test_quit_rejects_non_json(client, monkeypatch):
    # A cross-site form POST can't stop the app.
    calls = []
    monkeypatch.setattr(main, "quit_app", lambda: calls.append(1))
    r = client.post("/api/quit", data={"x": "1"})
    assert r.status_code == 415
    assert calls == []


def test_quit_stops_the_server(client, monkeypatch):
    import time
    calls = []
    monkeypatch.setattr(main, "quit_app", lambda: calls.append(1))
    assert client.get("/api/health").json()["can_quit"] is True
    r = client.post("/api/quit", json={})
    assert r.status_code == 200 and r.json() == {"status": "stopping"}
    # Stops just after replying (TestClient's loop runs in a background thread).
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)
    assert calls == [1]


def test_rate_schedules_endpoint(client):
    data = client.get("/api/rate-schedules").json()
    # Keyed by plan id...
    for plan_id in PLANS:
        assert data[plan_id] == RATE_SCHEDULES[plan_id]
    # ...plus the tariff-label aliases the frontend looks up.
    assert data["EV2 TARIFF"]["rates"]["PEAK"] == 55.3253
    assert data["EV2 TARIFF"] == data[EV]


def test_plans_endpoint_details(client):
    data = client.get("/api/plans", params={"tariff": "EV2 TARIFF"}).json()
    assert data["plans"] == list(PLANS)
    assert data["default"] == EV
    assert data["details"]["Home Plan A1"] == {
        "periods": [{"name": "Anytime", "key": "PEAK"}], "flat": True,
    }
    assert data["details"][EV]["flat"] is False
    assert {p["key"] for p in data["details"][EV]["periods"]} == {
        "PEAK", "OFF_PEAK", "OFF_PEAK(AM)", "OVERNIGHT", "SUPER_OFFPEAK",
    }
    assert client.get("/api/plans", params={"tariff": "MYSTERY"}).json()["default"] is None


def test_data_files_empty_when_no_data_dir(client, data_dir):
    assert not data_dir.exists()
    assert client.get("/api/data/files").json() == []


def test_data_files_lists_metadata_sorted(client, data_dir):
    # Aug 1 and Aug 3 only: start/end span the file, days counts distinct dates.
    _write_cache(data_dir, "b_2026-08-01_to_2026-08-03.json",
                 [date(2026, 8, 1), date(2026, 8, 3)], tariff_label="EV2 TARIFF")
    _write_cache(data_dir, "c_single.json", [FIXTURE_DAY], tariff_label="MIDDAY SAVER")
    (data_dir / "a_broken.json").write_text("not json")
    (data_dir / "notes.txt").write_text("ignored")

    files = client.get("/api/data/files").json()
    assert files == [
        {"name": "a_broken.json", "tariff_label": None, "start": None, "end": None, "days": 0,
         "compatible": False, "note": "Unreadable file"},
        {"name": "b_2026-08-01_to_2026-08-03.json", "tariff_label": "EV2 TARIFF",
         "start": "2026-08-01", "end": "2026-08-03", "days": 2,
         "compatible": True, "note": None},
        {"name": "c_single.json", "tariff_label": "MIDDAY SAVER",
         "start": "2026-08-02", "end": "2026-08-02", "days": 1,
         "compatible": True, "note": None},
    ]


# A cache file written by the pre-April-2026 client: raw parallel arrays.
LEGACY_PAYLOAD = {
    "chartType": "ELEC",
    "kwHalfHourlyValues": [0.3] * 48,
    "kwhHalfHourlyValuesGeneration": [0.0] * 48,
    "retrieveIntervalDetails": [{"productCode": "EV Add on"}],
}


def test_data_files_flags_legacy_cache_as_incompatible(client, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "2025-03-01_to_2026-02-28.json").write_text(json.dumps(LEGACY_PAYLOAD))
    [info] = client.get("/api/data/files").json()
    assert info["compatible"] is False
    assert "Old format" in info["note"]
    assert info["days"] == 0


def test_analysing_legacy_cache_gives_clear_error(client, data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "old.json").write_text(json.dumps(LEGACY_PAYLOAD))
    client.post("/api/analyse", json={"cached_file": "old.json", "battery_capacity_kwh": 15.0})
    job = _poll_job(client)
    assert job["status"] == "error"
    assert "pre-April-2026" in job["error"]
    assert "fetch the data again" in job["error"]


def test_analyse_validates_date_range(client):
    response = client.post("/api/analyse", json={
        "start_date": "2026-08-02", "end_date": "2026-08-01", "battery_capacity_kwh": 15.0,
    })
    assert response.status_code == 422


@pytest.mark.parametrize("eff", [0, -0.1, 1.5])
def test_analyse_validates_round_trip_efficiency(client, eff):
    response = client.post("/api/analyse", json={
        "cached_file": "x.json", "battery_capacity_kwh": 15.0, "round_trip_efficiency": eff,
    })
    assert response.status_code == 422


@pytest.mark.parametrize("dates", [
    {},
    {"start_date": "2026-08-01"},
    {"end_date": "2026-08-01"},
])
def test_live_fetch_requires_both_dates(client, dates):
    response = client.post("/api/analyse", json={
        "service_id": "001X", "battery_capacity_kwh": 15.0, **dates,
    })
    assert response.status_code == 400
    assert client.get("/api/analyse/progress").json()["status"] == "idle"


# --- cached-file analysis: plan selection + rates ---

def test_analyse_from_cached_file_uses_datas_own_plan(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    data = _analyse(client, cached_file=name)  # no plan_id, no dates, no overrides

    # EV2 tariff projects onto the EV Add On plan, priced from the built-in rates.
    plan = data["plan"]
    assert plan["id"] == plan["name"] == EV
    assert plan["periods"] == plan_periods(EV)
    assert plan["rates"] == _expected_rates(EV, EV_SCHEDULE["rates"])
    assert plan["supply_charge_cents"] == EV_SCHEDULE["supply_charge_cents"]
    assert plan["rates_source"] == "default"

    summary = data["summary"]
    assert len(data["intervals"]) == 48
    assert round(summary["total_consumption_kwh"], 2) == round(FIXTURE_CONSUMPTION_KWH, 2)
    assert summary["total_export_kwh"] == 15.81
    assert summary["num_days"] == 1
    assert summary["data_start"] == summary["data_end"] == "2026-08-02"
    assert summary["supply_cost_dollars"] == round(EV_SCHEDULE["supply_charge_cents"] / 100, 2)
    assert _other_warnings(data) == []
    assert len(_projection_warnings(data)) == 1  # one day of data
    assert data["cache_file"] == name

    # Per-interval feed-in credits sum to the summary's export credits, so the
    # Cost Breakdown chart's net total matches "Net Cost (no battery)".
    credits = sum(i["export_credit_cents"] for i in data["intervals"])
    assert round(credits / 100, 2) == summary["export_credits_dollars"]


def test_cached_files_tariff_label_wins_over_request_label(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY], tariff_label="EV2 TARIFF")
    data = _analyse(client, cached_file=name, tariff_label="HOME PLAN A1")
    assert data["plan"]["id"] == EV


@pytest.mark.parametrize("plan_id", list(PLANS))
def test_every_plan_prices_from_builtin_rates_without_overrides(client, data_dir, plan_id):
    """With no overrides and no live scrape, every plan must price from its
    built-in schedule — never silently at 0c (the old EV fallback bug)."""
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    data = _analyse(client, cached_file=name, plan_id=plan_id)

    schedule = RATE_SCHEDULES[plan_id]
    plan = data["plan"]
    assert plan["id"] == plan_id
    assert plan["rates"] == _expected_rates(plan_id, schedule["rates"])
    assert all(rate > 0 for rate in plan["rates"].values())
    assert plan["supply_charge_cents"] == schedule["supply_charge_cents"]
    assert plan["rates_source"] == "default"
    assert not [w for w in data["warnings"] if w.startswith("No rate")]
    # Every interval carries a non-zero rate from the plan.
    assert all(iv["rate_cents"] > 0 for iv in data["intervals"])


@pytest.mark.parametrize("label,plan_id", sorted(PLAN_BY_TARIFF.items()))
def test_null_plan_id_defaults_to_plan_for_data_label(client, data_dir, label, plan_id):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY], tariff_label=label)
    data = _analyse(client, cached_file=name, plan_id=None)
    assert data["plan"]["id"] == plan_id
    assert data["plan"]["rates"] == _expected_rates(plan_id, RATE_SCHEDULES[plan_id]["rates"])


def test_scraped_rates_layer_over_builtin_schedule(client, data_dir, monkeypatch):
    seen = []

    async def scraped(tariff_label):
        seen.append(tariff_label)
        # A partial scrape: only PEAK parsed, plus a supply charge.
        return {"rates": {"PEAK": 60.0}, "supply_charge_cents": 140.0}

    monkeypatch.setattr(main, "fetch_plan_rates", scraped)
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    plan = _analyse(client, cached_file=name)["plan"]

    assert seen == [EV]
    assert plan["rates"] == _expected_rates(EV, {**EV_SCHEDULE["rates"], "PEAK": 60.0})
    assert plan["supply_charge_cents"] == 140.0
    assert plan["rates_source"] == "synergy"


def test_partial_rate_override_keeps_other_rates(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    plan = _analyse(client, cached_file=name, rate_overrides={"PEAK": 70.0})["plan"]
    assert plan["rates"] == _expected_rates(EV, {**EV_SCHEDULE["rates"], "PEAK": 70.0})
    assert plan["rates_source"] == "default"


def test_full_override_skips_live_scrape(client, data_dir, monkeypatch):
    calls = []

    async def scraped(tariff_label):
        calls.append(tariff_label)
        return None

    monkeypatch.setattr(main, "fetch_plan_rates", scraped)
    overrides = {"PEAK": 50.0, "OFF_PEAK": 20.0, "OFF_PEAK(AM)": 21.0,
                 "OVERNIGHT": 15.0, "SUPER_OFFPEAK": 5.0}
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    data = _analyse(client, cached_file=name, rate_overrides=overrides, supply_charge_cents=100.0)

    assert calls == []
    assert data["plan"]["rates"] == _expected_rates(EV, overrides)
    assert data["plan"]["supply_charge_cents"] == 100.0
    assert data["plan"]["rates_source"] == "override"


def test_zero_rate_period_with_consumption_warns(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    data = _analyse(client, cached_file=name, rate_overrides={"OVERNIGHT": 0.0})
    # Overnight on the fixture day = 3.195 kWh.
    assert _other_warnings(data) == ["No rate for 'Overnight' — 3.2 kWh priced at 0c"]


def test_explicit_plan_projects_alternate_plan(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    data = _analyse(client, cached_file=name, plan_id="Midday Saver")
    plan = data["plan"]
    assert plan["name"] == "Midday Saver"
    # Midday Saver has only three periods; every slot is re-binned into them.
    assert set(plan["rates"]) == {"Peak", "Super Off Peak", "Off Peak"}
    assert {iv["tariff_name"] for iv in data["intervals"]} == set(plan["rates"])


def test_plan_id_accepts_case_insensitive_name(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    assert _analyse(client, cached_file=name, plan_id="midday saver")["plan"]["id"] == "Midday Saver"


def test_unknown_plan_id_is_job_error(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    job = _run(client, cached_file=name, plan_id="No Such Plan")
    assert job["status"] == "error"
    assert "Unknown plan" in job["error"]
    assert job["error_code"] is None


def test_unknown_tariff_falls_back_to_data_periods(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY], tariff_label="MYSTERY TARIFF")
    data = _analyse(client, cached_file=name, rate_overrides={"PEAK": 50.0})

    plan = data["plan"]
    assert plan["periods"] == []
    assert plan["name"] == "MYSTERY TARIFF"
    # Periods as Synergy labelled them; only the overridden one has a rate.
    assert plan["rates"] == {
        "Peak": 50.0, "Off Peak": 0.0, "Off Peak (AM)": 0.0,
        "Overnight": 0.0, "Super Off Peak": 0.0,
    }
    warnings = data["warnings"]
    assert any("doesn't match a known plan" in w for w in warnings)
    assert "No rate for 'Overnight' — 3.2 kWh priced at 0c" in warnings
    assert "No rate for 'Super Off Peak' — 8.3 kWh priced at 0c" in warnings
    assert not any(w.startswith("No rate for 'Peak'") for w in warnings)


# --- cached-file analysis: num_days / date filtering ---

JULY = _days(date(2026, 7, 1), 30)  # 2026-07-01 .. 2026-07-30


def _payback_from(summary, battery_cost):
    return battery_cost / (summary["period_savings_dollars"] / summary["num_days"] * 365)


def test_num_days_and_payback_follow_the_data_not_the_request(client, data_dir):
    name = _write_cache(data_dir, "july.json", JULY)
    supply = EV_SCHEDULE["supply_charge_cents"]
    common = {"cached_file": name, "battery_cost_dollars": 10000.0}

    whole = _analyse(client, **common)
    s = whole["summary"]
    assert s["num_days"] == 30
    assert (s["data_start"], s["data_end"]) == ("2026-07-01", "2026-07-30")
    assert s["supply_cost_dollars"] == round(30 * supply / 100, 2)
    assert s["payback_years"] is not None
    assert s["payback_years"] == pytest.approx(_payback_from(s, 10000.0), abs=0.06)
    assert _other_warnings(whole) == []

    # A year-long request around 30 days of data must not change anything but
    # add a warning: supply and payback are annualised from the 30 data days.
    year = _analyse(client, **common, start_date="2026-01-01", end_date="2026-12-31")
    assert year["summary"] == s
    assert len(_other_warnings(year)) == 1
    assert "only covers 2026-07-01 to 2026-07-30" in _other_warnings(year)[0]

    # A 7-day filter narrows the data itself.
    week = _analyse(client, **common, start_date="2026-07-10", end_date="2026-07-16")
    w = week["summary"]
    assert w["num_days"] == 7
    assert (w["data_start"], w["data_end"]) == ("2026-07-10", "2026-07-16")
    assert len(week["intervals"]) == 7 * 48
    assert w["supply_cost_dollars"] == round(7 * supply / 100, 2)
    assert w["payback_years"] == pytest.approx(_payback_from(w, 10000.0), abs=0.06)
    # Every day is identical usage, so a week annualises to (nearly) the same
    # payback as the month — not 30/7 or 365/7 times off.
    assert w["payback_years"] == pytest.approx(s["payback_years"], rel=0.05)
    assert w["annualised_savings_dollars"] == pytest.approx(s["annualised_savings_dollars"],
                                                           rel=0.05)
    assert _other_warnings(week) == []


def test_num_days_counts_distinct_dates_and_warns_on_gaps(client, data_dir):
    days = [date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 5)]
    name = _write_cache(data_dir, "gappy.json", days)
    data = _analyse(client, cached_file=name)
    s = data["summary"]
    assert s["num_days"] == 3
    assert (s["data_start"], s["data_end"]) == ("2026-08-01", "2026-08-05")
    assert s["supply_cost_dollars"] == round(3 * EV_SCHEDULE["supply_charge_cents"] / 100, 2)
    others = _other_warnings(data)
    assert len(others) == 1
    assert "gaps" in others[0]
    assert "3 of the 5 days" in others[0]
    assert "missing: 2026-08-03, 2026-08-04" in others[0]


def test_cached_filter_outside_data_is_error(client, data_dir):
    name = _write_cache(data_dir, "july.json", JULY)
    job = _run(client, cached_file=name, start_date="2025-01-01", end_date="2025-01-07")
    assert job["status"] == "error"
    assert "2026-07-01 to 2026-07-30" in job["error"]


def test_cached_filter_partial_overlap_warns(client, data_dir):
    name = _write_cache(data_dir, "july.json", JULY)
    data = _analyse(client, cached_file=name, start_date="2026-06-25", end_date="2026-07-03")
    assert data["summary"]["num_days"] == 3
    assert data["summary"]["data_start"] == "2026-07-01"
    assert len(_other_warnings(data)) == 1
    assert "only covers" in _other_warnings(data)[0]


def test_cached_single_bound_is_open_ended(client, data_dir):
    name = _write_cache(data_dir, "july.json", JULY)
    data = _analyse(client, cached_file=name, start_date="2026-07-28")
    assert data["summary"]["num_days"] == 3
    assert data["summary"]["data_end"] == "2026-07-30"


def test_missing_cached_file_is_job_error(client):
    job = _run(client, cached_file="nope.json")
    assert job["status"] == "error"
    assert "not found" in job["error"]


def test_malformed_cached_file_is_job_error(client, data_dir):
    data_dir.mkdir()
    (data_dir / "bad.json").write_text('{"intervals": [{"timestamp": "yesterday"}]}')
    job = _run(client, cached_file="bad.json")
    assert job["status"] == "error"
    assert "unreadable" in job["error"]


# --- live fetch + auth ---

def test_analyse_without_login_reports_not_logged_in(client):
    job = _run(client, service_id="001X", start_date="2026-08-02", end_date="2026-08-02")
    assert job["status"] == "error"
    assert "logged in" in job["error"].lower()
    assert job["error_code"] == "not_logged_in"


def test_live_fetch_session_expiry_sets_not_logged_in(client):
    fake = FakeClient(range_error=NotLoggedIn("Synergy session rejected (HTTP 401)"))
    app.state.synergy = fake
    app.state.services = [{"service_id": "001X", "tariff": "EV2 TARIFF"}]
    app.state.login_finalized = True

    job = _run(client, service_id="001X", start_date="2026-08-02", end_date="2026-08-02")
    assert job["status"] == "error"
    assert job["error_code"] == "not_logged_in"
    assert "log in again" in job["error"]
    # Per-login state is forgotten so the UI can start a fresh login.
    assert app.state.services == []
    assert app.state.login_finalized is False


def test_other_job_errors_have_no_error_code(client):
    app.state.synergy = FakeClient(range_error=RuntimeError("Aura call failed: HTTP 500"))
    job = _run(client, service_id="001X", start_date="2026-08-02", end_date="2026-08-02")
    assert job["status"] == "error"
    assert "HTTP 500" in job["error"]
    assert job["error_code"] is None


def test_live_fetch_caches_in_data_dir_and_warns_on_short_data(client, data_dir):
    sid = "../001X"  # unsafe characters are stripped from the cache file name
    fake = FakeClient(intervals=_day_intervals(FIXTURE_DAY))
    app.state.synergy = fake
    app.state.services = [{"service_id": sid, "tariff": "EV2 TARIFF"}]

    data = _analyse(client, service_id=sid, start_date="2026-08-01", end_date="2026-08-03")

    assert fake.range_calls == [(sid, date(2026, 8, 1), date(2026, 8, 3))]
    # Tariff comes from the selected service -> EV plan.
    assert data["plan"]["id"] == EV
    assert data["summary"]["num_days"] == 1
    others = _other_warnings(data)
    assert len(others) == 1
    assert "only 1 of the 3 requested days" in others[0]
    assert "missing: 2026-08-01, 2026-08-03." in others[0]

    cache = data_dir / "2026-08-01_to_2026-08-03_001X.json"
    assert [p.name for p in data_dir.iterdir()] == [cache.name]
    assert data["cache_file"] == cache.name  # frontend re-runs against this file
    payload = json.loads(cache.read_text())
    assert payload["tariff_label"] == "EV2 TARIFF"
    assert payload["service_id"] == sid
    assert len(payload["intervals"]) == 48


def test_live_fetch_with_no_data_is_error_and_not_cached(client, data_dir):
    app.state.synergy = FakeClient(intervals=[])
    job = _run(client, service_id="001X", start_date="2026-08-01", end_date="2026-08-03")
    assert job["status"] == "error"
    assert "no interval data" in job["error"]
    assert not data_dir.exists() or not any(data_dir.iterdir())


def test_auth_status_when_not_logged_in(client):
    data = client.get("/api/auth/status").json()
    assert data == {"logged_in": False, "services": [], "browser_open": False}


def test_auth_status_reports_open_login_window(client):
    app.state.synergy = FakeClient(logged_in=False, window_open=True)
    assert client.get("/api/auth/status").json()["browser_open"] is True
    app.state.synergy.window_open = False  # user closed the window
    assert client.get("/api/auth/status").json()["browser_open"] is False


def test_auth_status_logged_in_fetches_services_and_finalizes_once(client):
    services = [{"service_id": "001X", "tariff": "EV2 TARIFF"}]
    fake = FakeClient(services=services)
    app.state.synergy = fake
    for _ in range(2):
        data = client.get("/api/auth/status").json()
        assert data == {"logged_in": True, "services": services, "browser_open": False}
    assert fake.services_calls == 1
    assert fake.finalize_calls == 1


def test_auth_status_handles_expired_session(client):
    app.state.synergy = FakeClient(services_error=NotLoggedIn("expired"))
    data = client.get("/api/auth/status").json()
    assert data["logged_in"] is False
    assert data["services"] == []


def test_open_login_failure_returns_500_and_leaves_no_client(client, monkeypatch):
    async def broken_start(self):
        raise RuntimeError("Executable doesn't exist at /fake/chromium")

    monkeypatch.setattr(SynergyBrowserClient, "start", broken_start)
    response = client.post("/api/auth/open-login")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "Executable doesn't exist" in detail
    assert "Chrome or Edge" in detail and "uv run" not in detail
    assert app.state.synergy is None
    assert client.get("/api/auth/status").json()["logged_in"] is False


def test_open_login_replaces_stale_window_and_reset_clears(client, monkeypatch):
    closed = []

    async def fake_start(self):
        self._page = FakePage()

    async def fake_close(self):
        closed.append(self)
        self._page = None

    monkeypatch.setattr(SynergyBrowserClient, "start", fake_start)
    monkeypatch.setattr(SynergyBrowserClient, "close", fake_close)

    assert client.post("/api/auth/open-login").json() == {"status": "browser_opened"}
    first = app.state.synergy
    assert isinstance(first, SynergyBrowserClient)
    assert client.get("/api/auth/status").json()["browser_open"] is True

    # Not logged in yet (e.g. the user closed the window): a second click
    # discards the stale client and starts a fresh one.
    assert client.post("/api/auth/open-login").json() == {"status": "browser_opened"}
    second = app.state.synergy
    assert second is not first
    assert closed == [first]

    app.state.services = [{"service_id": "001X"}]
    app.state.login_finalized = True
    assert client.post("/api/auth/reset").json() == {"status": "reset"}
    assert app.state.synergy is None
    assert app.state.services == []
    assert app.state.login_finalized is False
    assert closed == [first, second]
    assert client.get("/api/auth/status").json() == {
        "logged_in": False, "services": [], "browser_open": False,
    }


def test_open_login_when_already_logged_in_keeps_client(client):
    fake = FakeClient(logged_in=True)
    app.state.synergy = fake
    assert client.post("/api/auth/open-login").json() == {"status": "logged_in"}
    assert app.state.synergy is fake
    assert fake.closed is False


def test_reset_without_client(client):
    assert client.post("/api/auth/reset").json() == {"status": "reset"}
    assert app.state.synergy is None


def test_auth_status_empty_service_list_is_fetched_once(client):
    fake = FakeClient(services=[])
    app.state.synergy = fake
    for _ in range(3):
        data = client.get("/api/auth/status").json()
        assert data == {"logged_in": True, "services": [], "browser_open": False}
    assert fake.services_calls == 1
    assert fake.finalize_calls == 1


def test_auth_status_unexpected_services_error_is_200_with_error(client):
    app.state.synergy = FakeClient(services_error=RuntimeError("Aura blew up"))
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()
    assert data["logged_in"] is False
    assert data["services"] == []
    assert data["browser_open"] is False
    assert "Aura blew up" in data["error"]
    assert app.state.services == []
    assert app.state.services_loaded is False
    assert app.state.login_finalized is False


def test_auth_status_unexpected_error_drops_and_closes_the_client(client):
    # A persistent failure must not loop: the broken client is discarded, so the
    # next "Open Synergy login" starts a fresh browser rather than reusing it.
    fake = FakeClient(services_error=RuntimeError("Aura blew up"))
    app.state.synergy = fake
    assert "error" in client.get("/api/auth/status").json()
    assert app.state.synergy is None
    assert fake.closed is True
    # Polling again is a clean "not logged in", not the same error.
    again = client.get("/api/auth/status").json()
    assert again == {"logged_in": False, "services": [], "browser_open": False}


def test_auth_status_error_text_is_truncated(client):
    app.state.synergy = FakeClient(services_error=KeyError("x" * 10_000))
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    assert len(response.json()["error"]) < 400


def test_auth_status_finalize_failure_is_200_with_error_and_resets(client):
    class BrokenFinalize(FakeClient):
        async def finalize_login(self):
            raise RuntimeError("window vanished")

    app.state.synergy = BrokenFinalize(services=[{"service_id": "001X", "tariff": "EV2 TARIFF"}])
    response = client.get("/api/auth/status")
    assert response.status_code == 200
    data = response.json()
    assert data["logged_in"] is False
    assert data["services"] == []
    assert "window vanished" in data["error"]
    assert app.state.services == []
    assert app.state.login_finalized is False


# --- DNS-rebinding protection ---

@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "127.0.0.1.evil.example"])
def test_untrusted_host_header_is_rejected(client, host):
    response = client.get("/api/auth/status", headers={"Host": host})
    assert response.status_code == 400


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "localhost"])
def test_local_host_headers_are_allowed(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


# --- request validation ---

@pytest.mark.parametrize("field,value", [
    ("battery_capacity_kwh", -1.0),
    ("inverter_capacity_kw", 0.0),
    ("inverter_capacity_kw", -2.0),
    ("battery_cost_dollars", -1.0),
    ("feed_in_peak_cents", -0.5),
    ("feed_in_off_peak_cents", -1.0),
    ("supply_charge_cents", -1.0),
    ("rate_overrides", {"PEAK": -1.0}),
    ("rate_overrides", {"PEAK": 30.0, "OVERNIGHT": -0.01}),
])
def test_analyse_rejects_negative_inputs(client, field, value):
    response = client.post("/api/analyse", json={
        "cached_file": "x.json", "battery_capacity_kwh": 15.0, field: value,
    })
    assert response.status_code == 422
    assert client.get("/api/analyse/progress").json()["status"] == "idle"


def test_analyse_accepts_zero_and_null_inputs(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    data = _analyse(
        client, cached_file=name, battery_capacity_kwh=0.0, battery_cost_dollars=None,
        feed_in_peak_cents=0.0, feed_in_off_peak_cents=0.0, supply_charge_cents=0.0,
        rate_overrides={"SUPER_OFFPEAK": 0.0},
    )
    s = data["summary"]
    assert s["supply_cost_dollars"] == 0
    assert s["export_credits_dollars"] == 0
    assert s["period_savings_dollars"] == 0
    assert s["payback_years"] is None


# --- result echoes the inputs it was computed with ---

def test_result_echoes_inputs(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    data = _analyse(client, cached_file=name, battery_capacity_kwh=12.5,
                    inverter_capacity_kw=3.0, battery_cost_dollars=9000.0,
                    round_trip_efficiency=0.85, grid_topup=True)
    assert data["inputs"] == {
        "battery_capacity_kwh": 12.5, "inverter_capacity_kw": 3.0,
        "battery_cost_dollars": 9000.0, "round_trip_efficiency": 0.85, "grid_topup": True,
        "batteries": [{"capacity_kwh": 12.5, "cost_dollars": 9000.0}], "primary_battery": 0,
    }
    defaults = _analyse(client, cached_file=name)["inputs"]
    assert defaults == {
        "battery_capacity_kwh": 15.0, "inverter_capacity_kw": 5.0,
        "battery_cost_dollars": None, "round_trip_efficiency": 0.9, "grid_topup": False,
        "batteries": [{"capacity_kwh": 15.0, "cost_dollars": None}], "primary_battery": 0,
    }


# --- 12-month projection of savings / payback ---

def test_short_range_is_projected_to_a_year_with_warning(client, data_dir):
    name = _write_cache(data_dir, "july.json", JULY)
    data = _analyse(client, cached_file=name, battery_cost_dollars=10000.0)
    s = data["summary"]
    assert s["is_projection"] is True
    assert s["period_savings_dollars"] > 0
    # Annualised from the unrounded savings; period_savings is rounded to cents.
    assert s["annualised_savings_dollars"] == pytest.approx(
        s["period_savings_dollars"] / 30 * 365, abs=0.15)
    assert s["payback_years"] == pytest.approx(10000.0 / s["annualised_savings_dollars"], abs=0.06)

    [warning] = _projection_warnings(data)
    assert "from 30 days of data (2026-07-01 to 2026-07-30)" in warning
    assert "estimate" in warning
    assert "full 12 months" in warning


def test_single_day_projection_warning_is_singular(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    [warning] = _projection_warnings(_analyse(client, cached_file=name))
    assert "from 1 day of data (2026-08-02 to 2026-08-02)" in warning


@pytest.mark.parametrize("num_days,is_projection,warned", [
    (329, True, True),
    (330, True, False),
    (359, True, False),
    # Synergy may not have published yesterday yet, so a default 12-month run
    # often has 363-364 days: that still counts as a full year.
    (360, False, False),
    (364, False, False),
    (365, False, False),
])
def test_projection_flag_and_warning_thresholds(client, data_dir, num_days, is_projection, warned):
    days = _days(date(2025, 8, 1), num_days)
    name = _write_cache(data_dir, "long.json", days)
    data = _analyse(client, cached_file=name, battery_cost_dollars=10000.0)
    s = data["summary"]
    assert s["num_days"] == num_days
    assert s["is_projection"] is is_projection
    assert bool(_projection_warnings(data)) is warned
    if warned:
        assert f"from {num_days} days of data (2025-08-01 to {days[-1]})" in _projection_warnings(data)[0]
    if num_days == 365:
        # A full year is measured, not projected: annual == period savings.
        assert s["annualised_savings_dollars"] == pytest.approx(s["period_savings_dollars"],
                                                               abs=0.01)


@pytest.mark.parametrize("body", [
    {"battery_capacity_kwh": 0.0, "battery_cost_dollars": 10000.0},  # no saving
    {"battery_capacity_kwh": 15.0, "battery_cost_dollars": None},     # no cost
    {"battery_capacity_kwh": 15.0, "battery_cost_dollars": 0.0},
])
def test_no_payback_without_cost_or_saving(client, data_dir, body):
    name = _write_cache(data_dir, "july.json", JULY)
    s = _analyse(client, cached_file=name, **body)["summary"]
    assert s["payback_years"] is None
    assert s["annualised_savings_dollars"] is not None
    if body["battery_capacity_kwh"] == 0:
        assert s["annualised_savings_dollars"] == 0


# --- live fetch: missing days and gaps ---

def _live(client, days: list[date], start: str, end: str) -> dict:
    app.state.synergy = FakeClient(intervals=[iv for d in days for iv in _day_intervals(d)])
    app.state.services = [{"service_id": "001X", "tariff": "EV2 TARIFF"}]
    return _analyse(client, service_id="001X", start_date=start, end_date=end)


def test_live_fetch_with_a_hole_warns_missing_and_gap(client, data_dir):
    data = _live(client, [date(2026, 8, 1), date(2026, 8, 3)], "2026-08-01", "2026-08-03")
    assert data["summary"]["num_days"] == 2
    others = _other_warnings(data)
    assert len(others) == 2
    [short] = [w for w in others if "requested days" in w]
    [gap] = [w for w in others if "gaps" in w]
    assert "only 2 of the 3 requested days; missing: 2026-08-02." in short
    assert "only 2 of the 3 days between 2026-08-01 and 2026-08-03" in gap
    assert "missing: 2026-08-02" in gap


def test_live_fetch_long_missing_list_is_abbreviated(client, data_dir):
    data = _live(client, [date(2026, 8, 1), date(2026, 8, 10)], "2026-08-01", "2026-08-10")
    listed = ("missing: 2026-08-02, 2026-08-03, 2026-08-04, 2026-08-05, 2026-08-06, "
              "… (+3 more)")
    others = _other_warnings(data)
    assert any("only 2 of the 10 requested days" in w and listed in w for w in others)
    assert any("gaps" in w and listed in w for w in others)


def test_live_fetch_without_gaps_has_no_gap_warning(client, data_dir):
    data = _live(client, [date(2026, 8, 1), date(2026, 8, 2)], "2026-08-01", "2026-08-02")
    assert _other_warnings(data) == []


# --- usage under an unrecognised tariff column ---

def _with_unknown_rows(n: int) -> tuple[list[dict], float]:
    """Fixture day with the first n usage rows relabelled as UNKNOWN."""
    intervals = _day_intervals(FIXTURE_DAY)
    relabelled = [iv for iv in intervals if iv["consumption_kwh"] > 0][:n]
    for iv in relabelled:
        iv["tariff_name"] = "UNKNOWN"
    return intervals, sum(iv["consumption_kwh"] for iv in relabelled)


def test_unknown_column_usage_is_warned_and_priced_by_time_of_day(client, data_dir):
    intervals, kwh = _with_unknown_rows(3)
    name = _write_cache(data_dir, "day.json", [], intervals=intervals)
    data = _analyse(client, cached_file=name)  # EV2 -> known plan, re-binned

    [warning] = [w for w in data["warnings"] if "unrecognised tariff column" in w]
    assert warning.startswith("3 half-hours had usage in an unrecognised tariff column")
    assert f"({kwh:.1f} kWh)" in warning
    assert "priced by time-of-day" in warning
    # Re-binned into the plan's periods, so nothing is left unpriced.
    assert all(iv["tariff_name"] in data["plan"]["rates"] for iv in data["intervals"])
    assert all(iv["rate_cents"] > 0 for iv in data["intervals"])
    assert not [w for w in data["warnings"] if w.startswith("No rate")]
    assert round(data["summary"]["total_consumption_kwh"], 2) == round(FIXTURE_CONSUMPTION_KWH, 2)


def test_unknown_column_usage_on_unknown_tariff_is_reported_unpriced(client, data_dir):
    intervals, kwh = _with_unknown_rows(1)
    name = _write_cache(data_dir, "day.json", [], tariff_label="MYSTERY TARIFF",
                        intervals=intervals)
    data = _analyse(client, cached_file=name)
    assert f"No rate for 'Unknown' — {kwh:.1f} kWh priced at 0c" in data["warnings"]
    assert not [w for w in data["warnings"] if "unrecognised tariff column" in w]


# --- savings arithmetic: the cards must add up ---

@pytest.mark.parametrize("grid_topup", [False, True])
def test_savings_and_feed_in_figures_are_consistent(client, data_dir, grid_topup):
    # The fixture day has a big solar surplus, so the battery stores solar that
    # would otherwise earn feed-in; that lost feed-in must reduce the saving.
    name = _write_cache(data_dir, "consistency.json", _days(date(2025, 7, 1), 365))
    s = _analyse(client, cached_file=name, battery_capacity_kwh=15.0,
                 battery_cost_dollars=9000.0, grid_topup=grid_topup)["summary"]
    cents = 0.011  # each figure is rounded to the cent independently

    # The battery really does give up some feed-in.
    assert s["feed_in_lost_dollars"] > 0
    assert s["export_with_battery_kwh"] < s["total_export_kwh"]

    # Saving = import avoided - feed-in lost - grid top-up cost.
    assert s["period_savings_dollars"] == pytest.approx(
        s["import_avoided_dollars"] - s["feed_in_lost_dollars"] - s["grid_topup_cost_dollars"],
        abs=cents * 3)
    # Saving = Net Cost (without) - Net Cost (with): the two cards and the savings agree.
    assert s["period_savings_dollars"] == pytest.approx(
        s["total_cost_without_battery_dollars"] - s["total_cost_with_battery_dollars"],
        abs=cents * 3)
    # Feed-in with a battery = feed-in without - feed-in lost.
    assert s["export_credits_with_battery_dollars"] == pytest.approx(
        s["export_credits_dollars"] - s["feed_in_lost_dollars"], abs=cents * 3)
    # A full year is not projected: the Annual Savings card shows the saving itself.
    assert s["is_projection"] is False
    assert s["annualised_savings_dollars"] == pytest.approx(s["period_savings_dollars"], abs=cents)
    assert s["payback_years"] == pytest.approx(9000.0 / s["annualised_savings_dollars"], abs=0.05)


# --- comparing battery sizes ---

THREE = [{"capacity_kwh": 10.0, "cost_dollars": 7000.0},
         {"capacity_kwh": 15.0, "cost_dollars": 9000.0},
         {"capacity_kwh": 20.0, "cost_dollars": 13000.0}]


def test_compares_up_to_three_batteries(client, data_dir):
    name = _write_cache(data_dir, "year.json", _days(date(2025, 7, 1), 365))
    data = _analyse(client, cached_file=name, batteries=THREE, primary_battery=1)
    opts, s = data["options"], data["summary"]
    assert [o["capacity_kwh"] for o in opts] == [10.0, 15.0, 20.0]
    cents = 0.011
    for o in opts:
        # Each option's figures add up the same way the cards do.
        assert o["period_savings_dollars"] == pytest.approx(
            o["import_avoided_dollars"] - o["feed_in_lost_dollars"] - o["grid_topup_cost_dollars"],
            abs=cents * 3)
        assert o["period_savings_dollars"] == pytest.approx(
            s["total_cost_without_battery_dollars"] - o["total_cost_with_battery_dollars"],
            abs=cents * 3)
        # A battery cuts both grid import and export.
        assert o["import_with_battery_kwh"] < s["total_consumption_kwh"]
        assert o["export_with_battery_kwh"] < s["total_export_kwh"]
        assert o["payback_years"] == pytest.approx(
            o["cost_dollars"] / o["annualised_savings_dollars"], abs=0.05)
        assert o["ten_year_net_dollars"] == pytest.approx(
            o["annualised_savings_dollars"] * 10 - o["cost_dollars"], abs=0.15)
    # A bigger battery never saves less or imports more. (The synthetic year
    # repeats one day whose needs fit in under 10 kWh, so these sizes plateau;
    # the recommendation test checks the rising part of the curve.)
    savings = [o["annualised_savings_dollars"] for o in opts]
    assert savings[0] <= savings[1] <= savings[2]
    imports = [o["import_with_battery_kwh"] for o in opts]
    assert imports[0] >= imports[1] >= imports[2]
    # The summary cards describe the primary option (15 kWh).
    assert s["annualised_savings_dollars"] == opts[1]["annualised_savings_dollars"]
    assert s["payback_years"] == opts[1]["payback_years"]
    assert s["import_with_battery_kwh"] == opts[1]["import_with_battery_kwh"]
    assert data["inputs"]["battery_capacity_kwh"] == 15.0


def test_recommendation_is_the_knee_of_the_savings_curve(client, data_dir):
    name = _write_cache(data_dir, "year.json", _days(date(2025, 7, 1), 365))
    rec = _analyse(client, cached_file=name, batteries=THREE)["recommendation"]
    curve = rec["curve"]
    assert curve[0]["capacity_kwh"] == 0 and curve[0]["annual_savings_dollars"] == 0
    assert curve[-1]["capacity_kwh"] == rec["sweep_max_kwh"] == 30.0
    savings = [c["annual_savings_dollars"] for c in curve]
    assert savings == sorted(savings)  # never worse with more capacity
    top = rec["max_annual_savings_dollars"]
    assert rec["annual_savings_dollars"] >= 0.9 * top
    # ...and it is the smallest size that does.
    smaller = [c for c in curve if c["capacity_kwh"] < rec["capacity_kwh"]]
    assert all(c["annual_savings_dollars"] < 0.9 * top for c in smaller)
    assert rec["best_payback_option"] in (0, 1, 2)
    assert rec["best_value_option"] in (0, 1, 2)


def test_sweep_extends_to_the_largest_option(client, data_dir):
    name = _write_cache(data_dir, "day.json", [FIXTURE_DAY])
    rec = _analyse(client, cached_file=name,
                   batteries=[{"capacity_kwh": 42.0}])["recommendation"]
    assert rec["sweep_max_kwh"] == 42.0
    assert rec["curve"][-1]["capacity_kwh"] == 42.0
    # No prices entered -> no best-value pick among options.
    assert rec["best_payback_option"] is None and rec["best_value_option"] is None


@pytest.mark.parametrize("body", [
    {"batteries": []},
    {"batteries": THREE + [{"capacity_kwh": 5.0}]},          # more than 3
    {"batteries": [{"capacity_kwh": -1.0}]},
    {"batteries": [{"capacity_kwh": 10.0, "cost_dollars": -5}]},
    {"batteries": THREE, "primary_battery": 3},             # out of range
    {},                                                     # no battery at all
])
def test_battery_options_are_validated(client, body):
    response = client.post("/api/analyse", json={"cached_file": "x.json", **body})
    assert response.status_code == 422


def test_cost_breakdown_rows_add_up(client, data_dir):
    # Usage charges + supply charge - feed-in credits = net cost, for the
    # no-battery column and every battery; the supply charge never changes.
    name = _write_cache(data_dir, "year.json", _days(date(2025, 7, 1), 365))
    data = _analyse(client, cached_file=name, batteries=THREE, grid_topup=True)
    s, cents = data["summary"], 0.011 * 3
    assert s["usage_cost_dollars"] + s["supply_cost_dollars"] - s["export_credits_dollars"] == \
        pytest.approx(s["total_cost_without_battery_dollars"], abs=cents)
    for o in data["options"]:
        assert o["usage_cost_with_battery_dollars"] + s["supply_cost_dollars"] \
            - o["export_credits_with_battery_dollars"] == \
            pytest.approx(o["total_cost_with_battery_dollars"], abs=cents)
        assert o["usage_cost_with_battery_dollars"] < s["usage_cost_dollars"]


def test_favicon_served(client):
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    assert r.content[:4] == b"\x00\x00\x01\x00"  # ICO header
