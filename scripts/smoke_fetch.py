"""Manual end-to-end smoke test for the browser-assisted Synergy client.

Run:  uv run python scripts/smoke_fetch.py [YYYY-MM-DD]

A Chromium window opens on the Synergy portal. Log in (email/password + MFA).
Once you reach your dashboard, this script lists your services and fetches one
day of half-hourly data, printing per-tariff totals so you can cross-check
against the portal's own usage chart.

Cross-check reference (2026-08-02): PEAK 3.209, OFF_PEAK 1.596, OFF_PEAK(AM)
0.986, OVERNIGHT 3.195, SUPER_OFFPEAK 8.311, solar 15.811, Total_Cost 4.44.
"""

import asyncio
import sys
from collections import defaultdict
from datetime import date

from synergy_analyser.browser_client import SynergyBrowserClient


async def main():
    day = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 8, 2)
    client = SynergyBrowserClient(headless=False)
    await client.start()
    print("\n>>> A browser window has opened. Log in to Synergy (email/password + MFA).")
    print(">>> Waiting for login (up to 5 min)...")
    await client.wait_until_logged_in(timeout=300)
    print(">>> Logged in — session captured.\n")

    services = await client.get_services()
    print(f"Services found: {len(services)}")
    for s in services:
        print(f"  {s['service_id']}  {s['account_number']}  {s['tariff']}  {s['address']}")
    if not services:
        print("No services returned — check the response shape.")
        await client.close()
        return

    sid = services[0]["service_id"]
    print(f"\nFetching {day} for service {sid} ...")
    intervals = await client.get_day(sid, day)
    print(f"Got {len(intervals)} intervals\n")

    totals = defaultdict(float)
    solar = 0.0
    for i in intervals:
        totals[i["tariff_name"]] += i["consumption_kwh"]
        solar += i["export_kwh"]
    for name, kwh in sorted(totals.items()):
        print(f"  {name:14s} {kwh:8.3f} kWh")
    print(f"  {'SOLAR export':14s} {solar:8.3f} kWh")

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())
