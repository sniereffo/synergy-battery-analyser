from datetime import date, datetime, timedelta

import pytest

from synergy_analyser.browser_client import SynergyBrowserClient


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
