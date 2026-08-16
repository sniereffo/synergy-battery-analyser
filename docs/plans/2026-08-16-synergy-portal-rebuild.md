# Synergy Portal Rebuild Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Restore the analyser after Synergy retired its old REST API (28 Apr 2026) by re-targeting the new Salesforce/Vlocity portal at `my.synergy.net.au`, using browser-assisted login and the `MyAccount_ChartData` integration procedure.

**Architecture:** A Playwright-driven browser handles the reCAPTCHA+MFA login interactively (the user logs in once in a real window); the app then reuses that authenticated page's own `fetch` to call the `/s/sfsites/aura` endpoint (`BusinessProcessDisplayController.GenericInvoke2NoCont` → `MyAccount_ChartData`), so the session cookie, CSRF `aura.token`, and framework `fwuid` never have to be extracted or forged. Half-hourly usage is fetched one day per call and stitched into the existing interval model. Tariff periods are **read from the data** (each 30-min slot is pre-labelled PEAK/OFF_PEAK/OFF_PEAK(AM)/OVERNIGHT/SUPER_OFFPEAK), so time-window categorisation is removed; per-period rates are auto-sourced with editable prefilled defaults.

**Tech Stack:** FastAPI, httpx (kept for rate scraping), **Playwright (async, Chromium)** for login+API, Pydantic, vanilla-JS frontend, pytest/pytest-asyncio.

---

## Data model reference (confirmed from captures)

**Auth:** POST `https://my.synergy.net.au/s/sfsites/aura?r=<n>&aura.ApexAction.execute=1`, form-encoded: `message`, `aura.context` (contains live `fwuid`), `aura.token`, `aura.pageURI`. Requires the community `sid` cookie. Login page uses reCAPTCHA Enterprise → cannot be scripted headless; user logs in interactively.

**Account/service discovery** — `MyAccount_ServiceAccountInformation`, input `{"ServiceId": <id>}` (ServiceId itself comes from the initial page / `CommunityServiceController.getLinkedServices`). Returns ActiveServices[] with `Id` (ServiceId, `001...`), `ContractAccountNumber`, `Tarrif` (e.g. "EV2 TARIFF"), `SupplyAddress`.

**Half-hourly usage** — `MyAccount_ChartData`, input:
```json
{"StartDate":"YYYYMMDD","EndDate":"YYYYMMDD","ServiceId":"001...","IntervalType":"DAILY",
 "ChartType":"INTERVAL_DATA","Interval":"X","Daily":"","Monthly":"","DisplayOptionValue":"Daily",
 "Device":[],"PeriodStartDate":"YYYY-MM-DD","PeriodEndDate":"YYYY-MM-DD","PreviousMeters":[],
 "UnbilledStartDate":"YYYY-MM-DD","UnbilledEndDate":"YYYY-MM-DD","AmiMeterCount":1}
```
(Start==End==single day for half-hourly.) Response `IPResult.ChartData.Response` = 48 rows; each row:
`{"VAL_TIME":"HHMM"(slot start 0000..2330),"VAL_DAY":"YYYY-MM-DD","VAL_SOLAR":"<kWh export>",<ONE tariff col>:"<kWh import>","BillingStatus":...}`
where the tariff col is one of `PEAK|OFF_PEAK|OFF_PEAK(AM)|OVERNIGHT|SUPER_OFFPEAK`. Values are **kWh already** (no ×0.5). Half-hourly rows sum exactly to the daily-bucket view (validated 2026-08-02). Also `IPResult.Total_Cost`.

A multi-day range = loop day-by-day, one call each.

---

## Phase 0: Fixtures & scaffolding

### Task 0.1: Sanitised test fixtures from real capture

**Files:**
- Create: `tests/fixtures/chartdata_halfhourly_day.json` (the 48-row INTERVAL_DATA/DAILY response)
- Create: `tests/fixtures/chartdata_daily_week.json` (the 7-row daily-bucket response)
- Create: `tests/fixtures/service_account_information.json`

**Steps:**
1. From the analyst's captures (in the non-worktree `captures/`), extract the `IPResult` JSON for entries: half-hourly 2 Aug (#310), weekly (#248), and `MyAccount_ServiceAccountInformation` (#43).
2. **Sanitise**: replace real name → "JANE CITIZEN", email → "user@example.com", address → "1 Example St, Suburb WA 6000", ContractAccountNumber → "123456789", ServiceId → "001TESTSERVICE0001", ContractNumber → "99999999", meter → "0000000000". Keep all numeric usage values and structure intact.
3. Commit fixtures.

---

## Phase 1: Tariff/usage core (data-driven, TDD)

### Task 1.1: Parse a ChartData half-hourly response into intervals

**Files:**
- Create: `src/synergy_analyser/chartdata.py`
- Test: `tests/test_chartdata.py`

**Step 1 — failing test** (`tests/test_chartdata.py`):
```python
import json
from pathlib import Path
from datetime import datetime
from synergy_analyser.chartdata import parse_halfhourly_response

FIX = Path(__file__).parent / "fixtures" / "chartdata_halfhourly_day.json"

def test_parse_halfhourly_returns_48_intervals():
    data = json.loads(FIX.read_text())
    intervals = parse_halfhourly_response(data)
    assert len(intervals) == 48

def test_first_interval_shape():
    data = json.loads(FIX.read_text())
    first = parse_halfhourly_response(data)[0]
    assert first["timestamp"] == datetime(2026, 8, 2, 0, 0)
    assert first["tariff_name"] == "OVERNIGHT"
    assert first["consumption_kwh"] == 0.254   # import kWh for that slot
    assert first["export_kwh"] == 0.0          # VAL_SOLAR

def test_halfhourly_totals_match_daily_bucket():
    data = json.loads(FIX.read_text())
    intervals = parse_halfhourly_response(data)
    peak = sum(i["consumption_kwh"] for i in intervals if i["tariff_name"] == "PEAK")
    assert round(peak, 3) == 3.209
    solar = sum(i["export_kwh"] for i in intervals)
    assert round(solar, 3) == 15.811
```

**Step 2** — run, expect ImportError/fail.

**Step 3 — implement** `parse_halfhourly_response(ipresult_or_full: dict) -> list[dict]`:
- Accept either the full Aura returnValue or the inner `IPResult`; normalise to `IPResult.ChartData.Response`.
- `TARIFF_COLS = ["PEAK","OFF_PEAK","OFF_PEAK(AM)","OVERNIGHT","SUPER_OFFPEAK"]`.
- For each row: `timestamp = datetime.strptime(VAL_DAY + VAL_TIME, "%Y-%m-%d%H%M")`; `consumption_kwh = sum(float(row[c]) for c in TARIFF_COLS if c in row)`; `tariff_name` = the present tariff col (or `"UNKNOWN"` if none); `export_kwh = float(row.get("VAL_SOLAR", 0))`.
- Return list of dicts sorted by timestamp with keys `timestamp, consumption_kwh, export_kwh, tariff_name`.

**Step 4** — run tests, expect PASS. **Step 5** — commit.

### Task 1.2: Attach rates & feed-in to parsed intervals

**Files:**
- Modify: `src/synergy_analyser/chartdata.py`
- Test: `tests/test_chartdata.py`

**Behaviour (TDD):** `apply_rates(intervals, rates: dict[str,float], feed_in_peak_cents, feed_in_off_peak_cents, feed_in_peak_start="15:00", feed_in_peak_end="21:00") -> list[dict]` adds `rate_cents` (from `rates[tariff_name]`, default 0.0 + collect a set of unknown names), `cost_cents = consumption_kwh*rate_cents`, `feed_in_rate_cents` (DEBS window by timestamp), `export_credit_cents`. Keep the exact dict keys `battery.py` already consumes. Test with a small hand-built rates dict and assert cost/credit for a known slot.

### Task 1.3: Simplify models to a name→rate plan

**Files:**
- Modify: `src/synergy_analyser/models.py`
- Test: `tests/test_models.py`

Reduce `EnergyPlan` to `{id, name, supply_charge_cents, rates: dict[str,float]}` (drop `TariffPeriod` time windows and all `_parse_hours`/`_infer_base_period_hours`/`build_plan_from_api` time logic). Add `normalise_period_name()` mapping the API cols to display names (`OFF_PEAK(AM)`→"Off Peak (AM)", `SUPER_OFFPEAK`→"Super Off Peak", etc.). Keep `RATE_SCHEDULES` as prefill defaults keyed by tariff label ("EV2 TARIFF" → the existing EV rates). Delete tests that assert the removed time-window behaviour; add tests for the new shape.

### Task 1.4: Point battery.py + summary at the new intervals

**Files:**
- Modify: `src/synergy_analyser/battery.py` (only if key names changed — aim for none)
- Test: `tests/test_battery.py` (update fixtures to new interval dicts)

`battery.py` logic is unchanged; verify it runs on intervals produced by Task 1.2 and that a full day simulates without error. Update any test fixtures that used the old ×0.5 kW model.

---

## Phase 2: Browser-assisted Synergy client (Playwright)

### Task 2.1: Add Playwright dependency

**Files:** Modify `pyproject.toml` (add `playwright`), note `uv run playwright install chromium` in README/CLAUDE.md.

### Task 2.2: `SynergyBrowserClient`

**Files:**
- Rewrite: `src/synergy_analyser/synergy_client.py`
- Test: `tests/test_synergy_client.py` (unit-test only the pure helpers; live flow is manual)

Implement an async class:
- `async def start_login(headless=False)`: launch Chromium, open `https://my.synergy.net.au/s/`, return control so the user logs in. Expose `async def wait_until_logged_in(timeout)` that polls for the post-login dashboard (e.g. presence of a service context / a successful `getLinkedServices`).
- `async def _aura_apex(class_name, method, params)`: run inside the page via `page.evaluate`, POSTing to `/s/sfsites/aura` reusing the page's own `window.location`/token. Simplest robust route: have the page call the existing `$A`/Aura endpoint, OR replay a `fetch` with `aura.context`/`aura.token` scraped from `document`/network. Prefer `page.request`/`fetch` from within the authenticated context so cookies auto-attach.
- `async def get_services()` → `MyAccount_ServiceAccountInformation`.
- `async def get_day(service_id, day: date)` → `MyAccount_ChartData` (INTERVAL_DATA/DAILY/Interval="X"), returns parsed intervals via `chartdata.parse_halfhourly_response`.
- `async def get_range(service_id, start, end)` → loop days, concat, with polite concurrency (≤3) and a progress hook.
- Pure helper `build_chartdata_input(service_id, day, unbilled_start, unbilled_end)` → the input dict. **Unit-test this helper** against the exact captured payload.

### Task 2.3: Token/context extraction fallback

If in-page `fetch` proves unreliable, add `_scrape_aura_context(page)` reading `fwuid`/`aura.token` from the bootstrap; keep behind the same `_aura_apex` interface. Document which path is active.

---

## Phase 3: Rates auto-sourcing

### Task 3.1: Scrape published Synergy tariff rates

**Files:**
- Create: `src/synergy_analyser/rates.py`
- Test: `tests/test_rates.py` (parse a saved HTML fixture, not a live hit)

`async def fetch_plan_rates(tariff_label) -> dict[str,float] | None` from Synergy's public pricing pages; on miss, return `None` and the app falls back to `RATE_SCHEDULES` prefill. Save one HTML fixture for the EV plan page and test the parser offline. Investigate whether `Total_Cost` + known kWh lets us cross-check. Do **not** block the app on this — always allow manual override in the UI.

---

## Phase 4: Routes & frontend

### Task 4.1: Replace auth routes in `main.py`

Remove `/api/premise/search`, `/api/auth/request-token`, `/api/auth/request-sms`, `/api/auth/login`. Add:
- `POST /api/auth/open-login` → launches browser, returns a session handle.
- `GET  /api/auth/status` → logged-in? + discovered services.
- `POST /api/analyse` → for a date range, `get_range`, apply rates (fetched or supplied), simulate, summarise. Keep the cached-file path for offline replays (now stores parsed intervals).

### Task 4.2: Frontend

Replace address/OTP UI in `app.js` with: an "Open Synergy login" button + status poller, a service picker (if >1), a date-range picker, and a tariff-rates panel prefilled from fetched/default rates and editable. Keep the 6 ECharts. Update `style.css` as needed.

### Task 4.3: Docs & build

Update `CLAUDE.md` (new data flow, auth, `playwright install`), `README.md`, and `synergy-battery-analyser.spec` (bundle Chromium / document that live fetch needs a browser).

---

## Phase 5: Verification

- `uv run pytest -v` green.
- Manual live smoke test (needs the user): open login, log in + MFA, fetch a known day, confirm totals match the portal's own chart (cross-check 2 Aug: PEAK 3.209, solar 15.811, Total_Cost 4.44).
- Update GitHub issue #1 with the fix.
