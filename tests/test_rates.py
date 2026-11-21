from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import synergy_analyser.rates as rates
from synergy_analyser.rates import (
    fetch_plan_rates,
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


def test_k1_is_not_auto_sourced():
    # K1's tiered rate labels never parse, so it has no plan-page slug.
    assert plan_slug_for_tariff("HOME BUSINESS PLAN K1") is None
    assert plan_slug_for_tariff("K1") is None


# --- fetch_plan_rates + in-memory cache (no network: httpx is faked) ---

EV_HTML = (FIXTURES / "synergy_ev_plan.html").read_text()


@pytest.fixture(autouse=True)
def clear_rates_cache():
    rates._rates_cache.clear()
    yield
    rates._rates_cache.clear()


class FakeHttp:
    """Replaces httpx.AsyncClient in rates; records requested URLs."""

    def __init__(self, status=200, text=EV_HTML, error=None):
        self.status = status
        self.text = text
        self.error = error
        self.urls = []

    def __call__(self, *args, **kwargs):
        return self  # httpx.AsyncClient(...) -> this fake

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return type("Resp", (), {"status_code": self.status, "text": self.text})()


@pytest.fixture
def fake_http(monkeypatch):
    fake = FakeHttp()
    monkeypatch.setattr(rates.httpx, "AsyncClient", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    # Swap rates' own `time` reference, not the global time.monotonic (the
    # asyncio event loop relies on that).
    monkeypatch.setattr(rates, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.mark.asyncio
async def test_fetch_caches_successful_parse(fake_http, clock):
    first = await fetch_plan_rates("EV2 TARIFF")
    assert first["rates"]["PEAK"] == 55.3253
    assert fake_http.urls == [rates.PLANS_BASE + "Electric-Vehicle-Add-On"]

    # Same slug via another label, within the TTL: served from memory.
    clock[0] += 60
    second = await fetch_plan_rates("Electric Vehicle Add On")
    assert second == first
    assert len(fake_http.urls) == 1

    # Callers get copies: mutating one doesn't poison the cache.
    second["rates"]["PEAK"] = 0.0
    assert (await fetch_plan_rates("EV2 TARIFF"))["rates"]["PEAK"] == 55.3253
    assert len(fake_http.urls) == 1


@pytest.mark.asyncio
async def test_cache_expires_after_ttl(fake_http, clock):
    await fetch_plan_rates("EV2 TARIFF")
    clock[0] += 6 * 60 * 60 - 1
    await fetch_plan_rates("EV2 TARIFF")
    assert len(fake_http.urls) == 1
    clock[0] += 2
    await fetch_plan_rates("EV2 TARIFF")
    assert len(fake_http.urls) == 2


@pytest.mark.parametrize("failure", [
    {"text": "<p>No rates on this page</p>"},
    {"status": 503},
    {"error": httpx.ConnectError("offline")},
])
@pytest.mark.asyncio
async def test_failures_are_not_cached(fake_http, clock, failure):
    for k, v in failure.items():
        setattr(fake_http, k, v)
    assert await fetch_plan_rates("EV2 TARIFF") is None
    assert await fetch_plan_rates("EV2 TARIFF") is None
    assert len(fake_http.urls) == 2
    assert rates._rates_cache == {}

    # Once the page is back, the next call fetches and caches it.
    fake_http.status, fake_http.text, fake_http.error = 200, EV_HTML, None
    assert (await fetch_plan_rates("EV2 TARIFF"))["rates"]["PEAK"] == 55.3253
    assert len(fake_http.urls) == 3


@pytest.mark.asyncio
async def test_unmapped_tariff_never_hits_network(fake_http):
    assert await fetch_plan_rates("HOME BUSINESS PLAN K1") is None
    assert await fetch_plan_rates(None) is None
    assert fake_http.urls == []
