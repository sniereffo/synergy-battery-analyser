import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

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


# --- events from the login window helper ---

def test_helper_events_drive_state():
    client = SynergyBrowserClient()
    assert not client.window_open and not client.logged_in
    client._handle({"event": "ready"})
    assert client.window_open and client._ready.is_set()
    client._handle({"event": "captcha", "score": 0.9, "passed": True})
    assert client.captcha_score == 0.9
    client._handle({"event": "logged_in"})
    assert client.logged_in
    client._handle({"event": "closed"})
    assert not client.window_open


# --- data calls through the page ---

def _logged_in_client(status: int, text: str) -> SynergyBrowserClient:
    """A client whose helper answers every post with (status, text)."""
    client = SynergyBrowserClient()
    client._handle({"event": "ready"})
    client._handle({"event": "logged_in"})
    client.sent = []

    def send(obj):
        client.sent.append(obj)
        if obj["op"] == "post":
            asyncio.get_running_loop().call_soon(
                client._handle, {"id": obj["id"], "status": status, "text": text})
    client._send = send
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (0, "not_logged_in"),                      # the page lost its portal session
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
    client = _logged_in_client(status, body)
    with pytest.raises(NotLoggedIn):
        await client._call_ip("MyAccount_ChartData", {"ServiceId": "001X"})
    assert client.logged_in is False
    # A follow-up call fails fast without touching the page.
    sent = len(client.sent)
    with pytest.raises(NotLoggedIn):
        await client._call_ip("MyAccount_ChartData", {"ServiceId": "001X"})
    assert len(client.sent) == sent


@pytest.mark.asyncio
@pytest.mark.parametrize("status,text", [(500, "oops"), (-1, "TypeError: Load failed")])
async def test_server_or_network_error_is_not_treated_as_logout(status, text):
    client = _logged_in_client(status, text)
    with pytest.raises(RuntimeError) as err:
        await client._call_ip("MyAccount_ChartData", {})
    assert not isinstance(err.value, NotLoggedIn)
    assert client.logged_in


@pytest.mark.asyncio
async def test_successful_call_returns_ip_result():
    inner = {"IPResult": {"ChartData": {"Response": []}}}
    body = json.dumps({"actions": [{"state": "SUCCESS",
                                    "returnValue": {"returnValue": json.dumps(inner)}}]})
    client = _logged_in_client(200, body)
    assert await client._call_ip("MyAccount_ChartData", {}) == inner
    assert client.logged_in
    post = client.sent[0]
    # The page adds the session token itself; the app only sends the action.
    assert set(post) == {"op", "id", "message"}
    assert "GenericInvoke2NoCont" in post["message"]


@pytest.mark.asyncio
async def test_finalize_login_hides_the_window():
    client = _logged_in_client(200, "{}")
    await client.finalize_login()
    assert client.sent == [{"op": "hide"}]
    assert not client.window_open and client.logged_in


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
    client._logged_in = True
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
    client._logged_in = True

    async def fake_call_ip(ip_method, ip_input):
        return {"ChartData": {}}

    client._call_ip = fake_call_ip
    assert await client.get_day("001X", date(2026, 8, 2)) == []


# --- lifecycle (real helper process, stood in for by a tiny script) ---

FAKE_HOST = r"""
import json, sys
print(json.dumps({"event": "ready"}), flush=True)
for line in sys.stdin:
    cmd = json.loads(line)
    if cmd["op"] == "post":
        if "die" in cmd["message"]:
            sys.exit(3)
        print(json.dumps({"event": "logged_in"}), flush=True)
        body = {"actions": [{"state": "SUCCESS", "returnValue": {"returnValue": {"ok": cmd["id"]}}}]}
        print(json.dumps({"id": cmd["id"], "status": 200, "text": json.dumps(body)}), flush=True)
    elif cmd["op"] == "close":
        break
"""


@pytest.mark.asyncio
async def test_start_post_and_close_with_a_helper_process(monkeypatch):
    monkeypatch.setattr(browser_client, "host_command", lambda: [sys.executable, "-c", FAKE_HOST])
    client = SynergyBrowserClient()
    await client.start()
    assert client.window_open
    client._logged_in = True
    result = await client._call_apex("", "C", "m", {})
    assert "ok" in result
    proc = client._proc
    await client.close()
    assert proc.returncode == 0  # it was asked to close, not killed
    assert client._proc is None and not client.logged_in


@pytest.mark.asyncio
async def test_helper_exiting_mid_request_reports_logout(monkeypatch):
    monkeypatch.setattr(browser_client, "host_command", lambda: [sys.executable, "-c", FAKE_HOST])
    client = SynergyBrowserClient()
    await client.start()
    client._logged_in = True
    with pytest.raises(NotLoggedIn):
        await client._call_apex("", "die", "m", {})
    assert not client.logged_in
    await client.close()


@pytest.mark.asyncio
async def test_close_is_idempotent_without_start():
    client = SynergyBrowserClient()
    await client.close()
    await client.close()
    assert client.window_open is False


@pytest.mark.asyncio
async def test_start_failure_tears_down_and_reports_why(monkeypatch):
    monkeypatch.setattr(browser_client, "host_command", lambda: [
        sys.executable, "-c", "import sys; sys.stderr.write('Namespace WebKit2 not available\\n'); sys.exit(1)"])
    client = SynergyBrowserClient()
    with pytest.raises(RuntimeError, match="WebKit2 not available"):
        await client.start()
    assert client._proc is None and not client.window_open
