import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

import pytest

from synergy_analyser import browser_client
from synergy_analyser.browser_client import NotLoggedIn, SynergyBrowserClient

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_get_range_chunks_orders_and_reports_progress():
    client = SynergyBrowserClient()
    calls = []

    async def fake_span(service_id, start, end):
        calls.append((start, end))
        out = []
        d = start
        while d <= end:
            out.append({
                "timestamp": datetime(d.year, d.month, d.day, 0, 0),
                "consumption_kwh": 1.0, "export_kwh": 0.0, "tariff_name": "PEAK",
            })
            d += timedelta(days=1)
        return out

    client.get_span = fake_span  # bypass the network/browser

    progress = []
    result = await client.get_range(
        "001X", date(2026, 1, 1), date(2026, 3, 15),
        progress=lambda done, total, day: progress.append((done, total)),
        chunk_days=31,
    )

    # 74 days -> chunks of 31: [Jan1-31], [Feb1-Mar3], [Mar4-15]
    assert calls[0] == (date(2026, 1, 1), date(2026, 1, 31))
    assert len(calls) == 3
    total = (date(2026, 3, 15) - date(2026, 1, 1)).days + 1
    assert len(result) == total
    # chronological order regardless of chunk completion order
    timestamps = [iv["timestamp"] for iv in result]
    assert timestamps == sorted(timestamps)
    # progress ends at the full day count
    assert progress[-1][1] == total
    assert max(d for d, _ in progress) == total


@pytest.mark.asyncio
async def test_get_range_single_chunk_for_short_span():
    client = SynergyBrowserClient()
    calls = []

    async def fake_span(service_id, start, end):
        calls.append((start, end))
        return [{"timestamp": datetime(start.year, start.month, start.day, 0, 0),
                 "consumption_kwh": 1.0, "export_kwh": 0.0, "tariff_name": "PEAK"}]

    client.get_span = fake_span
    await client.get_range("001X", date(2026, 8, 1), date(2026, 8, 7), chunk_days=31)
    # A week fits in one chunk -> one request.
    assert calls == [(date(2026, 8, 1), date(2026, 8, 7))]


# --- token harvesting ---

AURA_URL = "https://my.synergy.net.au/s/sfsites/aura?r=5&other.Controller=1"


class FakeRequest:
    def __init__(self, post_data, method="POST", url=AURA_URL):
        self.method = method
        self.url = url
        self.post_data = post_data


def _aura_form(app: str, token: str = "TOKEN123", page_uri: str = "/s/usage") -> str:
    ctx = json.dumps({"mode": "PROD", "app": app, "fwuid": "abc"})
    return urlencode({
        "message": "{}", "aura.context": ctx, "aura.token": token, "aura.pageURI": page_uri,
    })


def test_on_request_accepts_community_app_token():
    client = SynergyBrowserClient()
    assert not client.logged_in
    client._on_request(FakeRequest(_aura_form("siteforce:communityApp")))
    assert client.logged_in
    assert client._aura_token == "TOKEN123"
    assert "siteforce:communityApp" in client._aura_context
    assert client._page_uri == "/s/usage"


@pytest.mark.parametrize("request_", [
    # The login page's own Aura traffic carries a token but no data session.
    FakeRequest(_aura_form("siteforce:loginApp2")),
    FakeRequest(_aura_form("siteforce:communityApp"), method="GET"),
    FakeRequest(_aura_form("siteforce:communityApp"), url="https://my.synergy.net.au/s/other"),
    FakeRequest(None),
    FakeRequest(urlencode({"aura.context": '{"app":"siteforce:communityApp"}'})),  # no token
])
def test_on_request_ignores_non_community_traffic(request_):
    client = SynergyBrowserClient()
    client._on_request(request_)
    assert not client.logged_in


def test_window_open_tracks_page():
    class Page:
        closed = False

        def is_closed(self):
            return self.closed

    client = SynergyBrowserClient()
    assert client.window_open is False
    client._page = Page()
    assert client.window_open is True
    client._page.closed = True
    assert client.window_open is False


# --- session-expiry mapping ---

class FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self.ok = 200 <= status < 300
        self._body = body

    async def text(self):
        return self._body


class FakeContext:
    def __init__(self, response: FakeResponse):
        self.posts = []
        response_ = response

        class _Request:
            async def post(_self, url, form=None, headers=None):
                self.posts.append((url, form))
                return response_

        self.request = _Request()


def _logged_in_client(response: FakeResponse) -> SynergyBrowserClient:
    client = SynergyBrowserClient()
    client._on_request(FakeRequest(_aura_form("siteforce:communityApp")))
    client._context = FakeContext(response)
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (401, '{"actions": []}'),
    (403, ""),
    (200, "<!DOCTYPE html><html><body>Login</body></html>"),
    (200, "[]"),
    (200, json.dumps({"exceptionEvent": True,
                      "event": {"descriptor": "markup://aura:invalidSession"}})),
    (200, json.dumps({"event": {"descriptor": "markup://aura:clientOutOfSync"}})),
    (200, json.dumps({"actions": [{"state": "ERROR",
                                   "error": [{"message": "Invalid Session ID"}]}]})),
])
async def test_rejected_session_raises_not_logged_in(status, body):
    client = _logged_in_client(FakeResponse(status, body))
    assert client.logged_in
    with pytest.raises(NotLoggedIn):
        await client._call_ip("MyAccount_ChartData", {"ServiceId": "001X"})
    assert client.logged_in is False
    # A follow-up call fails fast without touching the network.
    posts = len(client._context.posts)
    with pytest.raises(NotLoggedIn):
        await client._call_ip("MyAccount_ChartData", {"ServiceId": "001X"})
    assert len(client._context.posts) == posts


@pytest.mark.asyncio
async def test_server_error_is_not_treated_as_logout():
    client = _logged_in_client(FakeResponse(500, "oops"))
    with pytest.raises(RuntimeError) as err:
        await client._call_ip("MyAccount_ChartData", {})
    assert not isinstance(err.value, NotLoggedIn)
    assert client.logged_in


@pytest.mark.asyncio
async def test_successful_call_returns_ip_result_with_session_credentials():
    inner = {"IPResult": {"ChartData": {"Response": []}}}
    body = json.dumps({"actions": [{"state": "SUCCESS",
                                    "returnValue": {"returnValue": json.dumps(inner)}}]})
    client = _logged_in_client(FakeResponse(200, body))
    assert await client._call_ip("MyAccount_ChartData", {}) == inner
    assert client.logged_in
    _, form = client._context.posts[0]
    assert form["aura.token"] == "TOKEN123"
    assert form["aura.pageURI"] == "/s/usage"


@pytest.mark.asyncio
async def test_calls_before_login_raise_not_logged_in():
    client = SynergyBrowserClient()
    with pytest.raises(NotLoggedIn):
        await client.get_services()
    with pytest.raises(NotLoggedIn):
        await client.get_span("001X", date(2026, 8, 1), date(2026, 8, 2))


# --- get_range failure / empty chunks ---

@pytest.mark.asyncio
async def test_get_range_chunk_failure_propagates_and_cancels_others():
    client = SynergyBrowserClient()
    cancelled = []

    async def fake_span(service_id, start, end):
        if start == date(2026, 1, 1):
            await asyncio.sleep(0.01)
            raise NotLoggedIn("session expired")
        try:
            await asyncio.Event().wait()  # would hang forever if not cancelled
        except asyncio.CancelledError:
            cancelled.append(start)
            raise

    client.get_span = fake_span
    with pytest.raises(NotLoggedIn, match="session expired"):
        await asyncio.wait_for(
            client.get_range("001X", date(2026, 1, 1), date(2026, 3, 15), chunk_days=31),
            timeout=2.0,
        )
    assert sorted(cancelled) == [date(2026, 2, 1), date(2026, 3, 4)]


@pytest.mark.asyncio
async def test_get_range_empty_chunk_yields_no_intervals():
    client = SynergyBrowserClient()
    client._aura_token, client._aura_context = "T", "C"
    half = json.loads((FIXTURES / "chartdata_halfhourly_day.json").read_text())

    async def fake_call_ip(ip_method, ip_input):
        if ip_input["StartDate"] == "20260101":
            return {"IPResult": {"ChartData": {"Devices": []}}}  # no Response: no data
        return half

    client._call_ip = fake_call_ip
    progress = []
    result = await client.get_range(
        "001X", date(2026, 1, 1), date(2026, 2, 15), chunk_days=31,
        progress=lambda done, total, day: progress.append((done, total)),
    )
    assert len(result) == 48
    assert max(progress) == (46, 46)


@pytest.mark.asyncio
async def test_get_day_with_no_data_is_empty():
    client = SynergyBrowserClient()
    client._aura_token, client._aura_context = "T", "C"

    async def fake_call_ip(ip_method, ip_input):
        return {"ChartData": {}}

    client._call_ip = fake_call_ip
    assert await client.get_day("001X", date(2026, 8, 2)) == []


# --- lifecycle ---

@pytest.mark.asyncio
async def test_close_is_idempotent_without_start():
    client = SynergyBrowserClient()
    await client.close()
    await client.close()
    assert client.window_open is False


@pytest.mark.asyncio
async def test_start_failure_tears_down_and_reraises(monkeypatch):
    stopped = []

    class FakePW:
        class chromium:
            @staticmethod
            async def launch(headless):
                raise RuntimeError("Executable doesn't exist")

        async def stop(self):
            stopped.append(True)

    class FakeManager:
        async def start(self):
            return FakePW()

    monkeypatch.setattr(browser_client, "async_playwright", lambda: FakeManager())
    client = SynergyBrowserClient()
    with pytest.raises(RuntimeError, match="Executable doesn't exist"):
        await client.start()
    assert stopped == [True]
    assert client._pw is None and client._browser is None and client._page is None
