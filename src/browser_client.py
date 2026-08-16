"""Browser-assisted Synergy portal client.

The new portal logs in behind Salesforce MFA + reCAPTCHA Enterprise, which
cannot be scripted headlessly. So we launch a real (headed) Chromium window and
let the user log in themselves. Once authenticated, we harvest a valid
`aura.token` / `aura.context` from the page's own traffic and issue
`MyAccount_ChartData` calls ourselves via the shared browser context (cookies
attach automatically), fetching half-hourly usage one day at a time.
"""

import asyncio
from datetime import date, timedelta

from playwright.async_api import async_playwright

from .aura import (
    build_apex_action,
    build_chartdata_input,
    build_message,
    build_service_info_input,
    parse_ip_result,
)
from .chartdata import parse_halfhourly_response

PORTAL_URL = "https://my.synergy.net.au/s/"
AURA_PATH = "/s/sfsites/aura"


class NotLoggedIn(RuntimeError):
    pass


class SynergyBrowserClient:
    def __init__(self, headless: bool = False):
        self._headless = headless
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        # Harvested from live traffic once the user is logged in.
        self._aura_context: str | None = None
        self._aura_token: str | None = None
        self._page_uri: str = "/s/"
        self._request_id = 1000

    async def start(self):
        """Launch the browser and begin capturing Aura credentials."""
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()
        self._context.on("request", self._on_request)
        await self._page.goto(PORTAL_URL)

    def _on_request(self, request):
        """Sniff Aura POSTs to harvest the authenticated session token + context.

        Only accept traffic from the community app (`siteforce:communityApp`).
        The login page also makes Aura calls (`siteforce:loginApp2`) that carry a
        token but no valid data session — ignoring those avoids reporting
        "logged in" while the user is still on the login screen.
        """
        if request.method != "POST" or AURA_PATH not in request.url:
            return
        data = request.post_data
        if not data:
            return
        from urllib.parse import parse_qs

        form = parse_qs(data)
        if "aura.token" not in form or "aura.context" not in form:
            return
        context = form["aura.context"][0]
        if "siteforce:communityApp" not in context:
            return
        self._aura_token = form["aura.token"][0]
        self._aura_context = context
        if "aura.pageURI" in form:
            self._page_uri = form["aura.pageURI"][0]

    @property
    def logged_in(self) -> bool:
        return self._aura_token is not None and self._aura_context is not None

    async def wait_until_logged_in(self, timeout: float = 300.0, poll: float = 1.0):
        """Block until the user has logged in (Aura credentials observed).

        The user completes email/password + MFA (+ any captcha) in the window.
        As soon as the authenticated app issues an Aura POST we capture what we
        need. Raises TimeoutError if not logged in within `timeout` seconds.
        """
        waited = 0.0
        while waited < timeout:
            if self.logged_in:
                return
            await asyncio.sleep(poll)
            waited += poll
        raise TimeoutError("Timed out waiting for Synergy login")

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _call_ip(self, ip_method: str, ip_input: dict) -> dict:
        """Invoke an Integration Procedure and return its IPResult."""
        if not self.logged_in:
            raise NotLoggedIn("Log in via the browser first")

        action = build_apex_action(ip_method, ip_input, self._next_id())
        form = {
            "message": build_message([action]),
            "aura.context": self._aura_context,
            "aura.token": self._aura_token,
            "aura.pageURI": self._page_uri,
        }
        # Reuse the browser context so the session cookies attach automatically.
        resp = await self._context.request.post(
            f"https://my.synergy.net.au{AURA_PATH}?r={self._next_id()}&aura.ApexAction.execute=1",
            form=form,
            headers={"X-SFDC-Page-Scope-Id": "", "Origin": "https://my.synergy.net.au"},
        )
        if not resp.ok:
            raise RuntimeError(f"Aura call {ip_method} failed: HTTP {resp.status}")
        body = await resp.json()
        return parse_ip_result(body)

    async def get_services(self) -> list[dict]:
        """Return active services: [{service_id, account_number, tariff, address}]."""
        # The dashboard fires ServiceAccountInformation with a ServiceId already,
        # but to enumerate we can call it with an empty id and read ActiveServices.
        result = await self._call_ip("MyAccount_ServiceAccountInformation", build_service_info_input(""))
        remote = result.get("RemoteActionGetSingleLinkedService", result)
        services = []
        for s in remote.get("ActiveServices", []):
            services.append({
                "service_id": s.get("Id") or s.get("value"),
                "account_number": s.get("AccountNumber") or s.get("ContractAccountNumber"),
                "tariff": s.get("Tarrif"),
                "address": s.get("SupplyAddress") or s.get("Address"),
                "type": s.get("Type"),
            })
        return services

    async def get_day(self, service_id: str, day: date) -> list[dict]:
        """Fetch and parse one day of half-hourly intervals."""
        result = await self._call_ip(
            "MyAccount_ChartData", build_chartdata_input(service_id, day)
        )
        return parse_halfhourly_response(result)

    async def get_range(
        self, service_id: str, start: date, end: date, progress=None
    ) -> list[dict]:
        """Fetch a contiguous date range, one day per call.

        Calls are made sequentially to stay polite to the portal. `progress`, if
        given, is called with (index, total, day) after each day.
        """
        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        intervals: list[dict] = []
        for i, day in enumerate(days, 1):
            intervals.extend(await self.get_day(service_id, day))
            if progress:
                progress(i, len(days), day)
        return intervals

    async def close(self):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
