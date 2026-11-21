from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pathlib import Path
import asyncio
import json
import os
import re
import sys

from .browser_client import SynergyBrowserClient, NotLoggedIn
from .chartdata import apply_rates, unpriced_consumption, count_unknown, unknown_kwh
from .models import (
    EnergyPlan, plan_for_tariff, display_name, rate_schedule_for,
    rate_schedules_with_aliases, DEFAULT_SUPPLY_CHARGE_CENTS,
)
from .rates import fetch_plan_rates
from .plans import (
    PLANS, available_plans, default_plan_for_tariff, assign_periods, rate_map_for_plan,
    plan_periods, plan_details,
)
from .battery import simulate_steady_state


MAX_ERROR_CHARS = 300
NOT_LOGGED_IN_MSG = "Not logged in. Open the Synergy login first."
SESSION_EXPIRED_MSG = (
    "Your Synergy session has expired or the login window was closed — "
    "please log in again."
)
# Below this many days of data, the 12-month projection gets a warning; from
# here up to FULL_YEAR_DAYS it is still a projection but close enough to a year.
PROJECTION_WARNING_DAYS = 330
# A run with at least this many days counts as a full year (not a projection).
# The default range ends yesterday, which Synergy may not have published yet,
# so a "12 month" run often has 363-364 days; those shouldn't read as projected.
FULL_YEAR_DAYS = 360

LEGACY_CACHE_MESSAGE = (
    "was saved by the pre-April-2026 version of this tool (old Synergy API) and "
    "can't be analysed — fetch the data again from Synergy."
)
MAX_LISTED_MISSING_DATES = 5


def _truncate(text, limit: int = MAX_ERROR_CHARS) -> str:
    """Bound upstream/exception text before it lands in an error message."""
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "…"


def _idle_job() -> dict:
    return {
        "status": "idle",   # idle | running | done | error
        "phase": "",        # human-readable current step
        "total": 0,
        "done": 0,
        "current_day": None,
        "result": None,
        "error": None,
        "error_code": None,  # "not_logged_in" when the Synergy session is gone
    }


def _reset_session_state():
    """Forget per-login state (services, window-closed flag)."""
    app.state.services = []
    app.state.services_loaded = False  # services fetched (even if empty)
    app.state.login_finalized = False


async def _close_quietly(client: SynergyBrowserClient):
    try:
        await client.close()
    except Exception:  # noqa: BLE001 — the browser may already be gone
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The browser is only launched when the user asks to log in, so importing
    # the app (e.g. under tests) never spawns Chromium.
    app.state.synergy = None
    _reset_session_state()
    app.state.auth_lock = asyncio.Lock()
    app.state.job = _idle_job()
    app.state.job_task = None  # strong ref so the background job isn't GC'd
    yield
    if app.state.synergy is not None:
        await _close_quietly(app.state.synergy)


app = FastAPI(title="Synergy Battery Feasibility Analyser", lifespan=lifespan)

# DNS-rebinding protection: the server is bound to 127.0.0.1, but a malicious
# page could still point its own hostname at 127.0.0.1 and read responses
# (/api/auth/status returns account details). Only accept local Host headers.
# "testserver" is the Host FastAPI's TestClient sends.
app.add_middleware(
    TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)

if getattr(sys, "frozen", False):
    static_dir = Path(sys._MEIPASS) / "static"
else:
    static_dir = Path(__file__).parent / "static"

# Cached interval data. Override with the SYNERGY_DATA_DIR env var; otherwise
# "data/" relative to the current working directory (run from the repo root).
# Referenced at call time, so tests can monkeypatch it.
DATA_DIR = Path(os.environ.get("SYNERGY_DATA_DIR") or "data")


# --- Request models ---

NonNegative = Annotated[float, Field(ge=0)]


MAX_BATTERY_OPTIONS = 3
# Battery warranties are typically ~10 years; used for the "10-year net" figure.
BATTERY_LIFE_YEARS = 10
# Recommended size = smallest capacity capturing this share of the saving
# available from the largest size in the sweep (the knee of the curve).
RECOMMEND_SHARE = 0.9
SWEEP_STEP_KWH = 2.5
SWEEP_MIN_MAX_KWH = 30.0


class BatteryOption(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    capacity_kwh: float = Field(ge=0, le=200)
    cost_dollars: float | None = Field(None, ge=0)


class AnalyseRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)

    service_id: str | None = None
    tariff_label: str | None = None
    # Plan to project. None = the plan matching the data's own tariff label,
    # else (unknown label) price the periods as labelled in the data.
    plan_id: str | None = None
    # Live fetch: the range to fetch (both required). Cached file: optional
    # inclusive filter; omitted = all cached data.
    start_date: date | None = None
    end_date: date | None = None
    # Up to MAX_BATTERY_OPTIONS batteries to compare; `primary_battery` picks
    # the one the cards and charts describe. The single-battery fields are the
    # older form and are used when `batteries` is omitted.
    batteries: list[BatteryOption] | None = Field(None, min_length=1,
                                                  max_length=MAX_BATTERY_OPTIONS)
    primary_battery: int = Field(0, ge=0)
    battery_capacity_kwh: float | None = Field(None, ge=0)
    battery_cost_dollars: float | None = Field(None, ge=0)
    inverter_capacity_kw: float = Field(5.0, gt=0)
    round_trip_efficiency: float = Field(0.9, gt=0, le=1)
    grid_topup: bool = False
    feed_in_peak_cents: float = Field(10.0, ge=0)
    feed_in_off_peak_cents: float = Field(2.0, ge=0)
    supply_charge_cents: float | None = Field(None, ge=0)
    rate_overrides: dict[str, NonNegative] | None = None
    cached_file: str | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self

    @model_validator(mode="after")
    def validate_batteries(self):
        if not self.batteries:
            if self.battery_capacity_kwh is None:
                raise ValueError("Give at least one battery (batteries or battery_capacity_kwh)")
            self.batteries = [BatteryOption(capacity_kwh=self.battery_capacity_kwh,
                                            cost_dollars=self.battery_cost_dollars)]
        if self.primary_battery >= len(self.batteries):
            raise ValueError("primary_battery is out of range")
        primary = self.batteries[self.primary_battery]
        # Keep the single-battery fields describing the primary option.
        self.battery_capacity_kwh = primary.capacity_kwh
        self.battery_cost_dollars = primary.cost_dollars
        return self


def _simulate(priced: list[dict], capacity_kwh: float, req: "AnalyseRequest") -> list[dict]:
    # Steady state: each size starts at its own end-of-period charge, so sizes
    # are compared fairly (no leftover-energy penalty for bigger batteries).
    return simulate_steady_state(
        priced, capacity_kwh, req.grid_topup, req.inverter_capacity_kw,
        round_trip_efficiency=req.round_trip_efficiency,
    )


def _battery_outcome(battery_result: list[dict], base: dict, num_days: int,
                     cost_dollars: float | None) -> dict:
    """Figures for one battery size, in cents/kWh (rounded by the caller).

    Identities: net_savings = import_avoided - feed_in_lost - topup_cost
    = cost_without - cost_with; credits_with = credits - feed_in_lost.
    """
    import_avoided = sum(e["savings_cents"] for e in battery_result)
    feed_in_lost = sum(e["lost_feed_in_cents"] for e in battery_result)
    topup_cost = sum(e["grid_topup_cost_cents"] for e in battery_result)
    net_savings = import_avoided - feed_in_lost - topup_cost
    solar_charge = sum(e["charge_kwh"] for e in battery_result)
    discharge = sum(e["discharge_kwh"] for e in battery_result)
    topup_kwh = sum(e["grid_topup_kwh"] for e in battery_result)
    annual = (net_savings / 100) / num_days * 365 if num_days else None
    payback = None
    if cost_dollars and annual is not None and annual > 0:
        payback = cost_dollars / annual
    ten_year = None
    if cost_dollars is not None and annual is not None:
        ten_year = annual * BATTERY_LIFE_YEARS - cost_dollars
    return {
        # Grid usage charges with the battery: what you'd still buy, plus top-up.
        "usage_cost_with": base["usage_cost"] - import_avoided + topup_cost,
        "import_avoided": import_avoided,
        "feed_in_lost": feed_in_lost,
        "topup_cost": topup_cost,
        "net_savings": net_savings,
        "cost_with": base["cost_without"] - net_savings,
        "import_with_kwh": base["import_kwh"] - discharge + topup_kwh,
        "export_with_kwh": base["export_kwh"] - solar_charge,
        "credits_with": base["credits"] - feed_in_lost,
        "annual": annual,
        "payback": payback,
        "ten_year": ten_year,
    }


def _option_payload(capacity_kwh: float, cost_dollars: float | None, o: dict) -> dict:
    return {
        "capacity_kwh": capacity_kwh,
        "cost_dollars": cost_dollars,
        "import_with_battery_kwh": round(o["import_with_kwh"], 2),
        "export_with_battery_kwh": round(o["export_with_kwh"], 2),
        "export_credits_with_battery_dollars": round(o["credits_with"] / 100, 2),
        "total_cost_with_battery_dollars": round(o["cost_with"] / 100, 2),
        "usage_cost_with_battery_dollars": round(o["usage_cost_with"] / 100, 2),
        "import_avoided_dollars": round(o["import_avoided"] / 100, 2),
        "feed_in_lost_dollars": round(o["feed_in_lost"] / 100, 2),
        "grid_topup_cost_dollars": round(o["topup_cost"] / 100, 2),
        "period_savings_dollars": round(o["net_savings"] / 100, 2),
        "annualised_savings_dollars": round(o["annual"], 2) if o["annual"] is not None else None,
        "payback_years": round(o["payback"], 1) if o["payback"] is not None else None,
        "ten_year_net_dollars": round(o["ten_year"], 2) if o["ten_year"] is not None else None,
    }


def _recommend(priced: list[dict], req: "AnalyseRequest", base: dict, num_days: int,
               options: list[dict]) -> dict:
    """Sweep battery sizes to find where extra capacity stops paying its way.

    The recommended size is the smallest capacity that captures
    RECOMMEND_SHARE of the saving available from the largest size swept
    (at least SWEEP_MIN_MAX_KWH, or the largest option entered). Among the
    user's priced options, also report the fastest payback and the best
    net benefit over BATTERY_LIFE_YEARS.
    """
    top = max([SWEEP_MIN_MAX_KWH] + [b.capacity_kwh for b in req.batteries])
    steps = int(top // SWEEP_STEP_KWH)
    caps = sorted({round(k * SWEEP_STEP_KWH, 2) for k in range(steps + 1)} | {top})
    curve = []
    for cap in caps:
        o = _battery_outcome(_simulate(priced, cap, req), base, num_days, None)
        curve.append({"capacity_kwh": cap,
                      "annual_savings_dollars": round(o["annual"] or 0.0, 2)})
    best_saving = max((c["annual_savings_dollars"] for c in curve), default=0.0)
    recommended = None
    if best_saving > 0:
        target = RECOMMEND_SHARE * best_saving
        recommended = next(c for c in curve if c["annual_savings_dollars"] >= target)

    priced_opts = [(i, o) for i, o in enumerate(options)
                   if o["cost_dollars"] and (o["annualised_savings_dollars"] or 0) > 0]
    best_payback = min(priced_opts, key=lambda t: t[1]["payback_years"])[0] if priced_opts else None
    best_value = max(priced_opts, key=lambda t: t[1]["ten_year_net_dollars"])[0] if priced_opts else None
    return {
        "capacity_kwh": recommended["capacity_kwh"] if recommended else None,
        "annual_savings_dollars": recommended["annual_savings_dollars"] if recommended else None,
        "share": RECOMMEND_SHARE,
        "sweep_max_kwh": top,
        "max_annual_savings_dollars": round(best_saving, 2),
        "curve": curve,
        "best_payback_option": best_payback,
        "best_value_option": best_value,
        "life_years": BATTERY_LIFE_YEARS,
    }


# --- Basic routes ---

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/rate-schedules")
def list_rate_schedules():
    """Built-in prefill schedules, keyed by plan id and by known tariff label."""
    return rate_schedules_with_aliases()


@app.get("/api/rates")
async def get_rates(tariff: str | None = None):
    """Current rates for a tariff label or plan name: live from Synergy's plan
    page if available, otherwise the built-in prefill defaults."""
    sched = rate_schedule_for(tariff)
    scraped = await fetch_plan_rates(tariff)
    if scraped and scraped.get("rates"):
        supply = scraped.get("supply_charge_cents")
        if supply is None:
            supply = sched.get("supply_charge_cents")
        return {
            "source": "synergy",
            "supply_charge_cents": supply,
            "rates": scraped["rates"],
        }
    if sched:
        return {
            "source": "default",
            "supply_charge_cents": sched["supply_charge_cents"],
            "rates": sched["rates"],
        }
    return {"source": "none", "supply_charge_cents": None, "rates": {}}


@app.get("/api/plans")
def list_plans(tariff: str | None = None):
    """Available plans for projection, which one matches this tariff, and each
    plan's periods (keys = API period columns) / flat-rate flag."""
    return {
        "plans": available_plans(),
        "default": default_plan_for_tariff(tariff),
        "details": plan_details(),
    }


def _is_legacy_payload(payload) -> bool:
    """True for raw responses cached by the old (pre-April-2026) API client.

    Those are dicts of parallel half-hourly arrays (e.g. kwHalfHourlyValues)
    rather than this version's {"intervals": [...]} payload.
    """
    return (
        isinstance(payload, dict)
        and "intervals" not in payload
        and any(k.lower().endswith("halfhourlyvalues") for k in payload)
    )


def _cache_file_info(path: Path) -> dict:
    info = {"name": path.name, "tariff_label": None, "start": None, "end": None,
            "days": 0, "compatible": True, "note": None}
    try:
        payload = json.loads(path.read_text())
        if _is_legacy_payload(payload):
            info.update(compatible=False, note="Old format (pre-April 2026) — fetch again")
            return info
        intervals = payload["intervals"] if isinstance(payload, dict) else payload
        dates = sorted({str(i["timestamp"])[:10] for i in intervals})
    except (OSError, ValueError, KeyError, TypeError):
        info.update(compatible=False, note="Unreadable file")
        return info  # unreadable / foreign JSON: still list it by name
    if isinstance(payload, dict):
        info["tariff_label"] = payload.get("tariff_label")
    if dates:
        info.update(start=dates[0], end=dates[-1], days=len(dates))
    return info


@app.get("/api/data/files")
def list_data_files():
    """Cached interval files: [{name, tariff_label, start, end, days, compatible, note}]."""
    if not DATA_DIR.exists():
        return []
    return [_cache_file_info(f) for f in sorted(DATA_DIR.glob("*.json"), key=lambda f: f.name)]


# --- Auth (browser-assisted) ---

@app.post("/api/auth/open-login")
async def open_login():
    """Launch the browser window for the user to log in to Synergy.

    If a previous window was abandoned (closed, or its session expired) it is
    discarded and a fresh one started.
    """
    async with app.state.auth_lock:
        client: SynergyBrowserClient | None = app.state.synergy
        if client is not None and client.logged_in:
            return {"status": "logged_in"}
        if client is not None:
            app.state.synergy = None
            await _close_quietly(client)
        _reset_session_state()

        new_client = SynergyBrowserClient(headless=False)
        try:
            await new_client.start()
        except Exception as e:  # noqa: BLE001 — report any launch failure
            await _close_quietly(new_client)
            raise HTTPException(
                500,
                f"Could not open the login browser: {_truncate(e)}. The login window "
                "uses Chrome or Edge if installed; otherwise it downloads a browser "
                "once, which needs an internet connection.",
            )
        app.state.synergy = new_client
    return {"status": "browser_opened"}


@app.post("/api/auth/reset")
async def auth_reset():
    """Close any browser/session and forget login state."""
    async with app.state.auth_lock:
        client = app.state.synergy
        app.state.synergy = None
        _reset_session_state()
        if client is not None:
            await _close_quietly(client)
    return {"status": "reset"}


@app.get("/api/auth/status")
async def auth_status():
    """Report whether the user is logged in and, if so, their services.

    On the first status check after login we fetch the service list and close
    the visible browser window — the session persists in the browser context.
    `browser_open` is true while a login window is open but not yet logged in.
    Never fails with a 500: an unexpected error fetching services/finalising
    is reported as `error` (logged_in false) so the UI can stop polling.
    """
    client: SynergyBrowserClient | None = app.state.synergy
    if client is None or not client.logged_in:
        _reset_session_state()
        browser_open = client is not None and bool(getattr(client, "window_open", False))
        return {"logged_in": False, "services": [], "browser_open": browser_open}
    try:
        if not app.state.services_loaded:
            app.state.services = await client.get_services()
            app.state.services_loaded = True  # cache even an empty list
        if not app.state.login_finalized:
            await client.finalize_login()
            app.state.login_finalized = True
    except NotLoggedIn:
        _reset_session_state()
        return {
            "logged_in": False, "services": [],
            "browser_open": bool(getattr(client, "window_open", False)),
        }
    except Exception as e:  # noqa: BLE001 — status polling must never 500
        # Drop the broken session entirely so the next "Open Synergy login"
        # starts a fresh browser instead of reusing a client stuck in this state.
        _reset_session_state()
        if app.state.synergy is client:
            app.state.synergy = None
        await _close_quietly(client)
        return {
            "logged_in": False, "services": [], "browser_open": False,
            "error": "Could not load your Synergy services: "
                     + (_truncate(e) or type(e).__name__),
        }
    return {"logged_in": True, "services": app.state.services, "browser_open": False}


# --- Analysis ---

def _cache_path(start: date, end: date, service_id: str | None) -> Path:
    name = f"{start}_to_{end}"
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "", service_id or "")
    if safe_sid:
        name += f"_{safe_sid}"
    return DATA_DIR / f"{name}.json"


def _load_cached(name: str) -> tuple[list[dict], str | None]:
    """Load a cached file (any naming scheme) -> (sorted intervals, tariff label)."""
    path = DATA_DIR / Path(name).name  # sanitise
    if not path.exists():
        raise HTTPException(404, f"Cached file not found: {_truncate(name, 100)}")
    try:
        payload = json.loads(path.read_text())
        if _is_legacy_payload(payload):
            raise HTTPException(400, f"Cached file {path.name} {LEGACY_CACHE_MESSAGE}")
        intervals = payload["intervals"] if isinstance(payload, dict) else payload
        for i in intervals:
            i["timestamp"] = datetime.fromisoformat(i["timestamp"])
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"Cached file {path.name} is unreadable: {_truncate(e)}")
    intervals.sort(key=lambda i: i["timestamp"])
    tariff = payload.get("tariff_label") if isinstance(payload, dict) else None
    return intervals, tariff


def _missing_dates(present: set[date], start: date, end: date) -> list[date]:
    """Dates in start..end (inclusive) that have no readings."""
    return [
        start + timedelta(days=k) for k in range((end - start).days + 1)
        if start + timedelta(days=k) not in present
    ]


def _format_dates(dates: list[date], limit: int = MAX_LISTED_MISSING_DATES) -> str:
    """"d1, d2, … (+N more)" — keeps long gap lists readable."""
    text = ", ".join(d.isoformat() for d in dates[:limit])
    if len(dates) > limit:
        text += f", … (+{len(dates) - limit} more)"
    return text


def _filter_dates(intervals: list[dict], start: date | None, end: date | None) -> list[dict]:
    return [
        i for i in intervals
        if (start is None or i["timestamp"].date() >= start)
        and (end is None or i["timestamp"].date() <= end)
    ]


@app.post("/api/analyse")
async def analyse(req: AnalyseRequest):
    """Start an analysis job. Fetching a live range can take a while, so this
    runs in the background; poll /api/analyse/progress for status + result."""
    if app.state.job["status"] == "running":
        raise HTTPException(409, "An analysis is already running.")
    if not req.cached_file and (req.start_date is None or req.end_date is None):
        raise HTTPException(400, "start_date and end_date are required for a live fetch.")
    app.state.job = _idle_job()
    app.state.job["status"] = "running"
    app.state.job["phase"] = "Starting…"
    # Keep a reference: the event loop only holds weak refs to tasks.
    app.state.job_task = asyncio.create_task(_run_analysis(req))
    return {"status": "started"}


@app.get("/api/analyse/progress")
async def analyse_progress():
    return app.state.job


async def _run_analysis(req: AnalyseRequest):
    job = app.state.job
    try:
        result = await _do_analysis(req, job)
        job["result"] = result
        job["phase"] = "Done"
        job["status"] = "done"
    except NotLoggedIn as e:
        job["error"] = str(e) or SESSION_EXPIRED_MSG
        job["error_code"] = "not_logged_in"
        job["status"] = "error"
        _reset_session_state()
    except HTTPException as e:
        job["error"] = e.detail
        job["status"] = "error"
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI
        job["error"] = _truncate(e) or type(e).__name__
        job["status"] = "error"


async def _do_analysis(req: AnalyseRequest, job: dict) -> dict:
    warnings: list[str] = []
    cache_name: str | None = None  # file the analysed data lives in (for re-runs)

    # 1. Obtain raw intervals (timestamp, consumption_kwh, export_kwh, tariff_name)
    if req.cached_file:
        job["phase"] = "Loading saved data…"
        all_intervals, cached_tariff = _load_cached(req.cached_file)
        cache_name = Path(req.cached_file).name
        # The file's own label describes the data; the request's is a fallback.
        tariff_label = cached_tariff or req.tariff_label
        if not all_intervals:
            raise HTTPException(400, "The cached file contains no interval data.")
        file_start = all_intervals[0]["timestamp"].date()
        file_end = all_intervals[-1]["timestamp"].date()
        intervals = _filter_dates(all_intervals, req.start_date, req.end_date)
        req_start = req.start_date or file_start
        req_end = req.end_date or file_end
        if not intervals:
            raise HTTPException(
                400,
                f"No cached data between {req_start} and {req_end} "
                f"(this file covers {file_start} to {file_end}).",
            )
        if req_start < file_start or req_end > file_end:
            warnings.append(
                f"Requested {req_start} to {req_end}, but the cached data only covers "
                f"{file_start} to {file_end}; only the overlapping days were analysed."
            )
    else:
        client: SynergyBrowserClient | None = app.state.synergy
        if client is None or not client.logged_in:
            raise NotLoggedIn(NOT_LOGGED_IN_MSG)
        if not req.service_id:
            raise HTTPException(400, "service_id is required for a live fetch.")

        job["phase"] = "Fetching usage from Synergy…"
        requested_days = (req.end_date - req.start_date).days + 1
        job["total"] = requested_days

        def progress(done, total, day):
            job["done"] = done
            job["total"] = total
            job["current_day"] = day.isoformat()

        try:
            intervals = await client.get_range(
                req.service_id, req.start_date, req.end_date, progress=progress
            )
        except NotLoggedIn:
            raise NotLoggedIn(SESSION_EXPIRED_MSG) from None

        tariff_label = req.tariff_label
        if not tariff_label:
            svc = next((s for s in app.state.services if s["service_id"] == req.service_id), None)
            tariff_label = svc["tariff"] if svc else None

        if not intervals:
            raise HTTPException(
                400, f"Synergy returned no interval data for {req.start_date} to {req.end_date}."
            )

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = _cache_path(req.start_date, req.end_date, req.service_id)
        cache_name = cache_path.name
        cache_path.write_text(json.dumps({
            "tariff_label": tariff_label,
            "service_id": req.service_id,
            "intervals": [{**i, "timestamp": i["timestamp"].isoformat()} for i in intervals],
        }, indent=2))

        fetched_dates = {i["timestamp"].date() for i in intervals}
        if len(fetched_dates) < requested_days:
            missing = _missing_dates(fetched_dates, req.start_date, req.end_date)
            warnings.append(
                f"Synergy returned data for only {len(fetched_dates)} of the {requested_days} "
                f"requested days; missing: {_format_dates(missing)}. "
                "Results cover only the days returned."
            )

    # Days actually present drive the supply charge and payback annualisation.
    data_dates = sorted({i["timestamp"].date() for i in intervals})
    num_days = len(data_dates)
    data_start, data_end = data_dates[0], data_dates[-1]
    span_days = (data_end - data_start).days + 1
    if req.cached_file:
        job["total"] = job["done"] = num_days
    if num_days < span_days:
        gaps = _missing_dates(set(data_dates), data_start, data_end)
        warnings.append(
            f"The data has gaps: only {num_days} of the {span_days} days between "
            f"{data_start} and {data_end} have readings; missing: {_format_dates(gaps)}."
        )

    # Usage under a tariff column we don't recognise. Counted before
    # assign_periods(), which overwrites tariff_name in known-plan mode.
    unknown_count = count_unknown(intervals)
    unknown_total_kwh = unknown_kwh(intervals)

    # 2. Choose the plan to project. A known plan re-bins each slot into that
    #    plan's time-of-use windows (enabling "what if I switched plans?"); an
    #    unknown tariff falls back to the periods the data already carries.
    job["phase"] = "Pricing and simulating…"
    if req.plan_id:
        plan_id = default_plan_for_tariff(req.plan_id)
        if plan_id is None:
            raise HTTPException(400, f"Unknown plan: {_truncate(req.plan_id, 100)}")
    else:
        plan_id = default_plan_for_tariff(tariff_label)
    overrides = req.rate_overrides or {}

    if plan_id in PLANS:
        # Rates keyed by API period column: built-in prefill, refreshed from
        # Synergy's site when reachable, with the user's overrides on top.
        prefill = rate_schedule_for(plan_id)
        base_rates = dict(prefill.get("rates", {}))
        base_supply = prefill.get("supply_charge_cents")
        rates_source = "default"
        plan_keys = {p["key"] for p in PLANS[plan_id]["periods"]}
        if not plan_keys <= overrides.keys() or req.supply_charge_cents is None:
            scraped = await fetch_plan_rates(plan_id)
            if scraped and scraped.get("rates"):
                base_rates.update(scraped["rates"])
                rates_source = "synergy"
                if scraped.get("supply_charge_cents") is not None:
                    base_supply = scraped["supply_charge_cents"]
        if plan_keys <= overrides.keys():
            rates_source = "override"
        rates_by_key = {**base_rates, **overrides}
        assign_periods(intervals, plan_id)
        if unknown_count:
            # Re-binning by time-of-day prices these too; the data-driven path
            # below prices UNKNOWN at 0c and the "No rate for" warning covers it.
            warnings.append(
                f"{unknown_count} half-hour{'s' if unknown_count != 1 else ''} had usage in "
                f"an unrecognised tariff column ({unknown_total_kwh:.1f} kWh) — they were "
                "priced by time-of-day under the selected plan."
            )
        rate_map = rate_map_for_plan(plan_id, rates_by_key)
        supply = req.supply_charge_cents
        if supply is None:
            supply = base_supply if base_supply is not None else DEFAULT_SUPPLY_CHARGE_CENTS
        plan = EnergyPlan(id=plan_id, name=plan_id, supply_charge_cents=supply, rates=rate_map)
        periods = plan_periods(plan_id)
    else:
        # Data-driven fallback: price the periods exactly as Synergy labelled them.
        periods_present = sorted({i["tariff_name"] for i in intervals})
        scraped = None
        if not set(periods_present) <= overrides.keys() or req.supply_charge_cents is None:
            scraped = await fetch_plan_rates(tariff_label)  # e.g. plans we can't re-bin
        scraped = scraped or {}
        plan = plan_for_tariff(
            tariff_label, periods_present,
            supply_charge_cents=req.supply_charge_cents,
            rate_overrides=overrides,
            base_rates=scraped.get("rates"),
            base_supply_cents=scraped.get("supply_charge_cents"),
        )
        rates_source = "synergy" if scraped.get("rates") else "default"
        periods = []
        warnings.append(
            f"Tariff '{tariff_label or 'unknown'}' doesn't match a known plan, so periods "
            "are priced as labelled in the data — check the rates."
        )

    # 3. Price + simulate
    priced = apply_rates(
        intervals, plan.rates,
        feed_in_peak_cents=req.feed_in_peak_cents,
        feed_in_off_peak_cents=req.feed_in_off_peak_cents,
    )
    for name, kwh in sorted(unpriced_consumption(priced, plan.rates).items()):
        warnings.append(f"No rate for '{display_name(name)}' — {kwh:.1f} kWh priced at 0c")

    # 4. Baseline (no battery), then each battery option on the same usage.
    total_supply_cost = num_days * plan.supply_charge_cents
    total_consumption = sum(e["consumption_kwh"] for e in priced)
    total_export = sum(e["export_kwh"] for e in priced)
    total_import_cost = sum(e["cost_cents"] for e in priced)
    total_export_credits = sum(e["export_credit_cents"] for e in priced)
    total_cost_without_battery = total_import_cost + total_supply_cost - total_export_credits
    base = {"cost_without": total_cost_without_battery, "import_kwh": total_consumption,
            "export_kwh": total_export, "credits": total_export_credits,
            "usage_cost": total_import_cost}

    job["phase"] = "Comparing battery sizes…"
    results_by_option = []
    options = []
    for b in req.batteries:
        res = _simulate(priced, b.capacity_kwh, req)
        o = _battery_outcome(res, base, num_days, b.cost_dollars)
        results_by_option.append((res, o))
        options.append(_option_payload(b.capacity_kwh, b.cost_dollars, o))
    recommendation = _recommend(priced, req, base, num_days, options)

    # The cards and charts describe the primary option.
    battery_result, primary = results_by_option[req.primary_battery]
    net_savings = primary["net_savings"]
    total_cost_with_battery = primary["cost_with"]
    total_discharge_savings = primary["import_avoided"]
    total_lost_feed_in = primary["feed_in_lost"]
    total_topup_cost = primary["topup_cost"]
    export_with_battery = primary["export_with_kwh"]
    export_credits_with_battery = primary["credits_with"]

    # Savings are projected to 12 months from however many days were analysed.
    # Short ranges are a rough estimate: solar and usage vary by season.
    annual_savings = (net_savings / 100) / num_days * 365 if num_days else None
    is_projection = num_days < FULL_YEAR_DAYS
    payback_years = None
    if req.battery_cost_dollars and annual_savings is not None and annual_savings > 0:
        payback_years = round(req.battery_cost_dollars / annual_savings, 1)
    if num_days < PROJECTION_WARNING_DAYS:
        warnings.append(
            f"Savings and payback are projected to 12 months from {num_days} "
            f"day{'s' if num_days != 1 else ''} of data ({data_start} to {data_end}). "
            "Solar generation and household usage vary a lot by season, so these "
            "figures are an estimate, not a measured result — analyse a full "
            "12 months for a reliable figure."
        )

    return {
        "plan": {
            "id": plan.id,
            "name": plan.name,
            "supply_charge_cents": plan.supply_charge_cents,
            "rates": {display_name(k): v for k, v in plan.rates.items()},
            "rates_by_period": plan.rates,
            "periods": periods,
            "rates_source": rates_source,  # synergy | default | override
        },
        "summary": {
            "total_consumption_kwh": round(total_consumption, 2),
            "total_export_kwh": round(total_export, 2),
            "total_cost_without_battery_dollars": round(total_cost_without_battery / 100, 2),
            "total_cost_with_battery_dollars": round(total_cost_with_battery / 100, 2),
            "period_savings_dollars": round(net_savings / 100, 2),
            "export_credits_dollars": round(total_export_credits / 100, 2),
            # Feed-in with a battery, and how the saving is made up:
            # period_savings = import_avoided - feed_in_lost - grid_topup_cost
            "import_with_battery_kwh": round(primary["import_with_kwh"], 2),
            "export_with_battery_kwh": round(export_with_battery, 2),
            "export_credits_with_battery_dollars": round(export_credits_with_battery / 100, 2),
            "import_avoided_dollars": round(total_discharge_savings / 100, 2),
            "feed_in_lost_dollars": round(total_lost_feed_in / 100, 2),
            "grid_topup_cost_dollars": round(total_topup_cost / 100, 2),
            "annualised_savings_dollars": (
                round(annual_savings, 2) if annual_savings is not None else None
            ),
            "is_projection": is_projection,
            "payback_years": payback_years,
            "num_days": num_days,
            "data_start": data_start.isoformat(),
            "data_end": data_end.isoformat(),
            "supply_cost_dollars": round(total_supply_cost / 100, 2),
            # Net cost = usage charges + supply charge - feed-in credits.
            "usage_cost_dollars": round(total_import_cost / 100, 2),
            "usage_cost_with_battery_dollars": round(primary["usage_cost_with"] / 100, 2),
        },
        "inputs": {
            "battery_capacity_kwh": req.battery_capacity_kwh,
            "inverter_capacity_kw": req.inverter_capacity_kw,
            "battery_cost_dollars": req.battery_cost_dollars,
            "round_trip_efficiency": req.round_trip_efficiency,
            "grid_topup": req.grid_topup,
            "batteries": [b.model_dump() for b in req.batteries],
            "primary_battery": req.primary_battery,
        },
        "options": options,
        "recommendation": recommendation,
        "warnings": warnings,
        "cache_file": cache_name,
        "intervals": [
            {
                "timestamp": e["timestamp"].isoformat(),
                "consumption_kwh": e["consumption_kwh"],
                "export_kwh": e["export_kwh"],
                "tariff_name": e["tariff_name"],
                "rate_cents": e["rate_cents"],
                "cost_cents": round(e["cost_cents"], 4),
                "export_credit_cents": round(e["export_credit_cents"], 4),
                "battery_kwh": round(e["battery_kwh"], 4),
                "discharge_kwh": round(e["discharge_kwh"], 4),
                "savings_cents": round(e["savings_cents"], 4),
                "grid_topup_kwh": round(e["grid_topup_kwh"], 4),
            }
            for e in battery_result
        ],
    }


# Serve frontend
@app.get("/")
async def root():
    return FileResponse(static_dir / "index.html")


app.mount("/static", StaticFiles(directory=static_dir), name="static")


if __name__ == "__main__":
    from .cli import main as run

    run()
