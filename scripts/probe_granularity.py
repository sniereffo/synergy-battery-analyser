"""Probe whether Synergy will return half-hourly data for a multi-day request.

This is the probe that established span fetching: the app originally fetched
half-hourly usage one day per request, and this script tested whether a WEEK or
MONTH request (with the half-hourly `Interval=X` flag) returns
many days of half-hourly rows in a single call. It does (IntervalType=DAILY with
Interval=X), so the app now fetches in roughly monthly spans.

Run:  uv run python scripts/probe_granularity.py
Log in when the browser opens; the script then issues several MyAccount_ChartData
variants for a fixed week/month and reports, for each, how many rows came back
and whether they are half-hourly (have VAL_TIME) or daily (VAL_DAY only).
"""

import asyncio

from synergy_analyser.browser_client import SynergyBrowserClient


def _input(service_id, start, end, *, interval_type, interval, daily, monthly, display):
    return {
        "StartDate": start.replace("-", ""),
        "EndDate": end.replace("-", ""),
        "ServiceId": service_id,
        "IntervalType": interval_type,
        "ChartType": "INTERVAL_DATA",
        "Interval": interval,
        "Daily": daily,
        "Monthly": monthly,
        "DisplayOptionValue": display,
        "Device": [],
        "PeriodStartDate": start,
        "PeriodEndDate": end,
        "PreviousMeters": [],
        "UnbilledStartDate": start,
        "UnbilledEndDate": end,
        "AmiMeterCount": 1,
        "OtherStartDate": None,
        "OtherEndDate": None,
    }


def _summarise(result):
    try:
        rows = result["IPResult"]["ChartData"]["Response"]
    except (KeyError, TypeError):
        return "no Response array (keys: %s)" % list(result.get("IPResult", {}))
    if not isinstance(rows, list):
        return f"Response not a list: {type(rows).__name__}"
    if not rows:
        return "0 rows"
    keys = list(rows[0])
    halfhourly = "VAL_TIME" in rows[0]
    days = len({r.get("VAL_DAY") for r in rows})
    kind = "HALF-HOURLY" if halfhourly else "daily-bucketed"
    return f"{len(rows)} rows across {days} day(s) — {kind}; first-row keys: {keys}"


async def main():
    client = SynergyBrowserClient(headless=False)
    await client.start()
    print("\n>>> Log in to Synergy in the browser window…")
    await client.wait_until_logged_in(timeout=300)
    print(">>> Logged in.\n")
    services = await client.get_services()
    if not services:
        print("No services found."); await client.close(); return
    sid = services[0]["service_id"]

    week_start, week_end = "2026-08-08", "2026-08-14"
    month_start, month_end = "2026-08-01", "2026-08-31"

    trials = [
        ("Single day, Interval=X (current method)",
         _input(sid, "2026-08-08", "2026-08-08", interval_type="DAILY", interval="X", daily="", monthly="", display="Daily")),
        ("Whole WEEK, Interval=X",
         _input(sid, week_start, week_end, interval_type="WEEK", interval="X", daily="", monthly="", display="Weekly")),
        ("Whole WEEK, IntervalType=DAILY, Interval=X (span)",
         _input(sid, week_start, week_end, interval_type="DAILY", interval="X", daily="", monthly="", display="Daily")),
        ("Whole MONTH, Interval=X",
         _input(sid, month_start, month_end, interval_type="MONTH", interval="X", daily="", monthly="", display="Monthly")),
        ("Whole MONTH, IntervalType=DAILY, Interval=X (span)",
         _input(sid, month_start, month_end, interval_type="DAILY", interval="X", daily="", monthly="", display="Daily")),
    ]

    for label, payload in trials:
        try:
            result = await client._call_ip("MyAccount_ChartData", payload)
            print(f"[{label}]\n    {_summarise(result)}\n")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}]\n    ERROR: {e}\n")

    await client.close()
    print("Done. If any WEEK/MONTH trial returned HALF-HOURLY across many days,")
    print("we can fetch in far fewer requests.")


if __name__ == "__main__":
    asyncio.run(main())
