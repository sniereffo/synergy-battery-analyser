from contextlib import asynccontextmanager
from datetime import date, datetime, time
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, model_validator
from pathlib import Path
import httpx
import json
import sys

from .synergy_client import SynergyClient
from .models import RATE_SCHEDULES, EnergyPlan, TariffPeriod, build_plan_from_api
from .tariff import categorise_usage_data
from .battery import simulate_battery


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.synergy_client = SynergyClient()
    app.state.allow_contract = None
    yield
    await app.state.synergy_client.close()


app = FastAPI(title="Synergy Battery Feasibility Analyser", lifespan=lifespan)

# Support both normal and PyInstaller-bundled paths
if getattr(sys, 'frozen', False):
    static_dir = Path(sys._MEIPASS) / "static"
else:
    static_dir = Path(__file__).parent / "static"


# --- Request/Response models ---

class PremiseSearchRequest(BaseModel):
    address: str

class EmailTokenRequest(BaseModel):
    email: str
    premise_id: str

class SmsCodeRequest(BaseModel):
    mobile: str
    premise_id: str

class LoginRequest(BaseModel):
    token: str
    method: str = "email"  # "email" or "sms"

class TariffPeriodOverride(BaseModel):
    name: str
    rate_cents: float
    start_time: str  # "HH:MM" format
    end_time: str

class AnalyseRequest(BaseModel):
    start_date: date
    end_date: date
    battery_capacity_kwh: float
    battery_cost_dollars: float | None = None
    inverter_capacity_kw: float = 5.0
    grid_topup: bool = False
    feed_in_peak_cents: float = 10.0    # DEBS peak rate (3pm-9pm)
    feed_in_off_peak_cents: float = 2.0  # DEBS off-peak rate
    custom_supply_charge_cents: float | None = None
    custom_tariff_periods: list[TariffPeriodOverride] | None = None
    cached_file: str | None = None

    @model_validator(mode="after")
    def validate_dates(self):
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self


DATA_DIR = Path("data")


# --- API routes ---

@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/data/files")
def list_data_files():
    """List cached API response files."""
    if not DATA_DIR.exists():
        return []
    return sorted(f.name for f in DATA_DIR.glob("*.json"))


@app.get("/api/rate-schedules")
def list_rate_schedules():
    """Return available rate schedules for display in the UI."""
    return RATE_SCHEDULES


@app.post("/api/premise/search")
async def search_premise(req: PremiseSearchRequest):
    try:
        client: SynergyClient = app.state.synergy_client
        results = await client.search_premise(req.address)
        return results
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Synergy API error: {e.response.text}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Synergy API: {e}")


@app.post("/api/auth/request-token")
async def request_token(req: EmailTokenRequest):
    try:
        client: SynergyClient = app.state.synergy_client
        allow_contract = await client.request_email_token(req.email, req.premise_id)
        app.state.allow_contract = allow_contract
        app.state.login_method = "email"
        return {"message": "Token sent. Check your email."}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400 and "too many attempts" in e.response.text.lower():
            raise HTTPException(429, "Too many attempts. Try again tomorrow.")
        raise HTTPException(e.response.status_code, f"Synergy API error: {e.response.text}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Synergy API: {e}")


@app.post("/api/auth/request-sms")
async def request_sms(req: SmsCodeRequest):
    try:
        client: SynergyClient = app.state.synergy_client
        allow_contract = await client.request_sms_code(req.mobile, req.premise_id)
        app.state.allow_contract = allow_contract
        app.state.login_method = "sms"
        return {"message": "SMS code sent."}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 400 and "too many attempts" in e.response.text.lower():
            raise HTTPException(429, "Too many attempts. Try again tomorrow.")
        raise HTTPException(e.response.status_code, f"Synergy API error: {e.response.text}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Synergy API: {e}")


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    try:
        client: SynergyClient = app.state.synergy_client
        if not app.state.allow_contract:
            raise HTTPException(400, "Request a token first.")

        if req.method == "sms":
            success = await client.login_with_sms_code(req.token, app.state.allow_contract)
        else:
            success = await client.login_with_email_token(req.token, app.state.allow_contract)

        if not success:
            raise HTTPException(401, "Login failed.")
        return {"message": "Logged in."}
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Login failed: {e.response.text}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Synergy API: {e}")


@app.post("/api/analyse")
async def analyse(req: AnalyseRequest):
    try:
        if req.cached_file:
            # Load from cached file — no auth required
            cache_path = DATA_DIR / Path(req.cached_file).name  # sanitise path
            if not cache_path.exists():
                raise HTTPException(404, f"Cached file not found: {req.cached_file}")
            raw_data = json.loads(cache_path.read_text())
        else:
            client: SynergyClient = app.state.synergy_client

            # Get account and device info
            account_number = await client.get_account_number()
            device_id = await client.get_device_id(account_number)

            # Fetch usage data
            raw_data = await client.get_usage_data(account_number, device_id, req.start_date, req.end_date)

            # Save to cache
            DATA_DIR.mkdir(exist_ok=True)
            cache_file = DATA_DIR / f"{req.start_date}_to_{req.end_date}.json"
            cache_file.write_text(json.dumps(raw_data, indent=2))

        # Build plan: use custom overrides if provided, otherwise auto-detect from API
        if req.custom_tariff_periods and req.custom_supply_charge_cents is not None:
            plan = EnergyPlan(
                id="custom",
                name="Custom",
                supply_charge_cents=req.custom_supply_charge_cents,
                tariff_periods=[
                    TariffPeriod(
                        name=tp.name,
                        rate_cents=tp.rate_cents,
                        start_time=time.fromisoformat(tp.start_time),
                        end_time=time.fromisoformat(tp.end_time),
                    )
                    for tp in req.custom_tariff_periods
                ],
            )
        else:
            interval_details = raw_data.get("retrieveIntervalDetails", [])
            if not interval_details:
                raise HTTPException(400, "No plan details found in API response.")
            plan = build_plan_from_api(interval_details[0])

        # Categorise usage data
        consumption = raw_data.get("kwHalfHourlyValues", [])
        generation = raw_data.get("kwhHalfHourlyValuesGeneration", [])
        start_dt = datetime.combine(req.start_date, datetime.min.time())

        categorised = categorise_usage_data(
            consumption, start_dt, plan, generation or None,
            feed_in_peak_cents=req.feed_in_peak_cents,
            feed_in_off_peak_cents=req.feed_in_off_peak_cents,
        )

        # Battery simulation
        battery_result = simulate_battery(categorised, req.battery_capacity_kwh, req.grid_topup, req.inverter_capacity_kw)

        # Calculate summaries
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
            period_savings_dollars = net_savings / 100
            daily_savings = period_savings_dollars / num_days
            annual_savings = daily_savings * 365
            payback_years = round(req.battery_cost_dollars / annual_savings, 1)

        return {
            "plan": {
                "name": plan.name,
                "supply_charge_cents": plan.supply_charge_cents,
                "tariff_periods": [p.model_dump() for p in plan.tariff_periods],
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
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, f"Synergy API error: {e.response.text}")
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Synergy API: {e}")
    except KeyError as e:
        raise HTTPException(400, f"Unsupported energy plan: {e}")


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
