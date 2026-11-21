import json
from pathlib import Path

import pytest

import synergy_analyser.main as main
from synergy_analyser.chartdata import parse_halfhourly_response, apply_rates

FIXTURES = Path(__file__).parent / "fixtures"

# Real (sanitised) EV2 rates for tests. Keyed by API period column name.
TEST_RATES = {
    "PEAK": 53.8446,
    "OFF_PEAK": 23.6916,
    "OFF_PEAK(AM)": 23.6916,
    "OVERNIGHT": 19.3841,
    "SUPER_OFFPEAK": 8.6151,
}


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Keep every test offline and away from the real ./data directory.

    - main.DATA_DIR points at a per-test temp dir (cache reads/writes land there).
    - main.fetch_plan_rates (a live scrape of synergy.net.au) returns None, so the
      built-in rate schedules are used. Tests that want scraped rates patch it
      again themselves.

    Returns the temp data dir (not created until something writes to it).
    """
    path = tmp_path / "data"
    monkeypatch.setattr(main, "DATA_DIR", path)

    async def no_rates(tariff_label):
        return None

    monkeypatch.setattr(main, "fetch_plan_rates", no_rates)
    return path


@pytest.fixture
def halfhourly_response():
    """The sanitised 48-row INTERVAL_DATA response for one day (2026-08-02)."""
    return json.loads((FIXTURES / "chartdata_halfhourly_day.json").read_text())


@pytest.fixture
def priced_intervals(halfhourly_response):
    """Parsed + priced intervals for one real day of usage."""
    intervals = parse_halfhourly_response(halfhourly_response)
    return apply_rates(intervals, TEST_RATES, feed_in_peak_cents=10.0, feed_in_off_peak_cents=2.0)
