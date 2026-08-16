from contextlib import asynccontextmanager
from datetime import date, datetime
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, model_validator
from pathlib import Path
import json
import sys

from .browser_client import SynergyBrowserClient, NotLoggedIn
from .chartdata import parse_halfhourly_response, apply_rates  # noqa: F401 (parse used via client)
from .models import RATE_SCHEDULES, plan_for_tariff, display_name
from .battery import simulate_battery


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The browser is only launched when the user asks to log in, so importing
    # the app (e.g. under tests) never spawns Chromium.
    app.state.synergy = None
    app.state.services = []
    yield
    if app.state.synergy is not None:
        await app.state.synergy.close()


app = FastAPI(title="Synergy Battery Feasibility Analyser", lifespan=lifespan)

if getattr(sys, "frozen", False):
    static_dir = Path(sys._MEIPASS) / "static"
else:
    static_dir = Path(__file__).parent / "static"

DATA_DIR = Path("data")


# --- Request models ---

class AnalyseRequest(BaseModel):
    service_id: str | None = None
    tariff_label: str | None = None
    start_date: date
    end_date: date
    battery_capacity_kwh: float
    battery_cost_dollars: float | None = None
    inverter_capacity_kw: float = 5.0
    grid_topup: bool = False
    feed_in_peak_cents: float = 10.0
    feed_in_off_peak_cents: float = 2.0
    supply_charge_cents: float | None = None
    rate_overrides: dict[str, float] | None = None
    cached_file: str | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


# --- Basic routes ---

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/rate-schedules")
def list_rate_schedules():
    return RATE_SCHEDULES


@app.get("/api/data/files")
def list_data_files():
    if not DATA_DIR.exists():
        return []
    return sorted(f.name for f in DATA_DIR.glob("*.json"))


# --- Auth (browser-assisted) ---

@app.post("/api/auth/open-login")
async def open_login():
    """Launch the browser window for the user to log in to Synergy."""
    if app.state.synergy is None:
        app.state.synergy = SynergyBrowserClient(headless=False)
        await app.state.synergy.start()
    return {"status": "browser_opened"}


@app.get("/api/auth/status")
async def auth_status():
    """Report whether the user is logged in and, if so, their services."""
    client: SynergyBrowserClient | None = app.state.synergy
    if client is None or not client.logged_in:
        return {"logged_in": False, "services": []}
    if not app.state.services:
        app.state.services = await client.get_services()
    return {"logged_in": True, "services": app.state.services}


# --- Analysis ---

def _cache_path(req: AnalyseRequest) -> Path:
    return DATA_DIR / f"{req.start_date}_to_{req.end_date}.json"


def _load_cached(name: str) -> tuple[list[dict], str | None]:
    path = DATA_DIR / Path(name).name  # sanitise
    if not path.exists():
        raise HTTPException(404, f"Cached file not found: {name}")
    payload = json.loads(path.read_text())
    intervals = payload["intervals"] if isinstance(payload, dict) else payload
    for i in intervals:
        i["timestamp"] = datetime.fromisoformat(i["timestamp"])
    tariff = payload.get("tariff_label") if isinstance(payload, dict) else None
    return intervals, tariff


@app.post("/api/analyse")
async def analyse(req: AnalyseRequest):
    # 1. Obtain raw intervals (timestamp, consumption_kwh, export_kwh, tariff_name)
    if req.cached_file:
        intervals, cached_tariff = _load_cached(req.cached_file)
        tariff_label = req.tariff_label or cached_tariff
    else:
        client: SynergyBrowserClient | None = app.state.synergy
        if client is None or not client.logged_in:
            raise HTTPException(401, "Not logged in. Open the Synergy login first.")
        if not req.service_id:
            raise HTTPException(400, "service_id is required for a live fetch.")
        try:
            intervals = await client.get_range(req.service_id, req.start_date, req.end_date)
        except NotLoggedIn:
            raise HTTPException(401, "Not logged in. Open the Synergy login first.")

        tariff_label = req.tariff_label
        if not tariff_label:
            svc = next((s for s in app.state.services if s["service_id"] == req.service_id), None)
            tariff_label = svc["tariff"] if svc else None

        DATA_DIR.mkdir(exist_ok=True)
        _cache_path(req).write_text(json.dumps({
            "tariff_label": tariff_label,
            "intervals": [{**i, "timestamp": i["timestamp"].isoformat()} for i in intervals],
        }, indent=2))

    if not intervals:
        raise HTTPException(400, "No interval data returned for that date range.")

    # 2. Build a data-driven plan for the periods actually present
    periods_present = sorted({i["tariff_name"] for i in intervals})
    plan = plan_for_tariff(
        tariff_label, periods_present,
        supply_charge_cents=req.supply_charge_cents,
        rate_overrides=req.rate_overrides,
    )

    # 3. Price + simulate
    priced = apply_rates(
        intervals, plan.rates,
        feed_in_peak_cents=req.feed_in_peak_cents,
        feed_in_off_peak_cents=req.feed_in_off_peak_cents,
    )
    battery_result = simulate_battery(
        priced, req.battery_capacity_kwh, req.grid_topup, req.inverter_capacity_kw
    )

    # 4. Summarise
    num_days = (req.end_date - req.start_date).days + 1
    total_supply_cost = num_days * plan.supply_charge_cents
    total_consumption = sum(e["consumption_kwh"] for e in battery_result)
    total_export = sum(e["export_kwh"] for e in battery_result)
    total_import_cost = sum(e["cost_cents"] for e in battery_result)
    total_export_credits = sum(e["export_credit_cents"] for e in battery_result)
    total_cost_without_battery = total_import_cost + total_supply_cost - total_export_credits

    total_discharge_savings = sum(e["savings_cents"] for e in battery_result)
    total_lost_feed_in = sum(e["lost_feed_in_cents"] for e in battery_result)
    total_topup_cost = sum(e["grid_topup_cost_cents"] for e in battery_result)
    net_savings = total_discharge_savings - total_lost_feed_in - total_topup_cost
    total_cost_with_battery = total_cost_without_battery - net_savings

    payback_years = None
    if req.battery_cost_dollars and net_savings > 0:
        annual_savings = (net_savings / 100) / num_days * 365
        payback_years = round(req.battery_cost_dollars / annual_savings, 1)

    return {
        "plan": {
            "name": plan.name,
            "supply_charge_cents": plan.supply_charge_cents,
            "rates": {display_name(k): v for k, v in plan.rates.items()},
            "rates_by_period": plan.rates,
        },
        "summary": {
            "total_consumption_kwh": round(total_consumption, 2),
            "total_export_kwh": round(total_export, 2),
            "total_cost_without_battery_dollars": round(total_cost_without_battery / 100, 2),
            "total_cost_with_battery_dollars": round(total_cost_with_battery / 100, 2),
            "period_savings_dollars": round(net_savings / 100, 2),
            "export_credits_dollars": round(total_export_credits / 100, 2),
            "payback_years": payback_years,
            "num_days": num_days,
            "supply_cost_dollars": round(total_supply_cost / 100, 2),
        },
        "intervals": [
            {
                "timestamp": e["timestamp"].isoformat(),
                "consumption_kwh": e["consumption_kwh"],
                "export_kwh": e["export_kwh"],
                "tariff_name": e["tariff_name"],
                "rate_cents": e["rate_cents"],
                "cost_cents": round(e["cost_cents"], 4),
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
    import uvicorn
    import webbrowser
    import threading

    threading.Timer(1.0, webbrowser.open, args=["http://127.0.0.1:8000"]).start()
    uvicorn.run(app, host="127.0.0.1", port=8000)
