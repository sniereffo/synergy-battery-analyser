"""Browser-assisted Synergy portal client.

The new portal logs in behind Salesforce MFA + reCAPTCHA Enterprise, which
cannot be scripted headlessly. So we launch a real (headed) Chromium window and
let the user log in themselves. Once authenticated, we harvest a valid
`aura.token` / `aura.context` from the page's own traffic and issue
`MyAccount_ChartData` calls ourselves via the shared browser context (cookies
attach automatically), fetching half-hourly usage in date-span requests
(the portal returns every day in StartDate..EndDate per call).
"""

import asyncio
import json
from datetime import date, timedelta

from playwright.async_api import async_playwright

from .aura import (
    build_apex_action,
    build_chartdata_input,
    build_ip_action,
    build_message,
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
        """Launch the browser and begin capturing Aura credentials.

        On failure (e.g. Chromium not installed) everything already started is
        torn down before the exception propagates.
        """
        try:
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=self._headless)
            self._context = await self._browser.new_context()
            self._page = await self._context.new_page()
            self._context.on("request", self._on_request)
            await self._page.goto(PORTAL_URL)
        except BaseException:
            await self.close()
            raise

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

    @property
    def window_open(self) -> bool:
        """True while the login page/window exists and has not been closed."""
        return self._page is not None and not self._page.is_closed()

    def _invalidate(self):
        """Forget harvested credentials after the portal rejects the session."""
        self._aura_token = None
        self._aura_context = None

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

    async def finalize_login(self):
        """Close the visible browser window once logged in.

        The authenticated session lives in the browser *context* (cookies) and
        the harvested aura token, not the page — so we can close the window and
        still issue data calls via `context.request`.
        """
        if self._page is not None and not self._page.is_closed():
            await self._page.close()
            self._page = None

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _post_action(self, action: dict) -> dict:
        """POST a single Aura action and return its parsed result."""
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
            headers={"Origin": "https://my.synergy.net.au"},
        )
        if resp.status in (401, 403):
            self._invalidate()
            raise NotLoggedIn(f"Synergy session rejected (HTTP {resp.status}); log in again")
        if not resp.ok:
            raise RuntimeError(f"Aura call failed: HTTP {resp.status}")
        text = await resp.text()
        try:
            body = json.loads(text)
        except ValueError:
            # Expired sessions typically bounce to an HTML login page.
            self._invalidate()
            raise NotLoggedIn("Synergy returned a non-JSON response (session expired?); log in again")
        if not isinstance(body, dict):
            self._invalidate()
            raise NotLoggedIn("Unexpected Synergy response shape; log in again")
        if self._session_invalid(body):
            self._invalidate()
            raise NotLoggedIn("Synergy session is invalid or expired; log in again")
        return parse_ip_result(body)

    @staticmethod
    def _session_invalid(body: dict) -> bool:
        """Detect Aura responses signalling an invalid/expired session."""
        if body.get("exceptionEvent"):
            return True
        markers = ("invalid session", "invalid_session", "clientoutofsync", "session expired")
        blob = json.dumps(
            [body.get("event"), [a.get("error") for a in body.get("actions") or [] if isinstance(a, dict)]]
        ).lower()
        return any(m in blob for m in markers)

    async def _call_ip(self, ip_method: str, ip_input: dict) -> dict:
        """Invoke a Vlocity Integration Procedure and return its result."""
        if not self.logged_in:
            raise NotLoggedIn("Log in via the browser first")
        return await self._post_action(build_ip_action(ip_method, ip_input, self._next_id()))

    async def _call_apex(self, namespace: str, classname: str, method: str, params: dict) -> dict:
        """Invoke a direct @AuraEnabled Apex controller method and return its result."""
        if not self.logged_in:
            raise NotLoggedIn("Log in via the browser first")
        return await self._post_action(
            build_apex_action(namespace, classname, method, params, self._next_id())
        )

    async def get_services(self) -> list[dict]:
        """Return active services: [{service_id, account_number, tariff, address}].

        Uses the portal's own discovery call, CommunityServiceController
        .getLinkedServices, which resolves the logged-in contact's services
        server-side (no ServiceId needed).
        """
        result = await self._call_apex(
            "", "CommunityServiceController", "getLinkedServices",
            {"contactId": None, "accountId": None},
        )
        services = []
        for s in result.get("ActiveServices", []):
            services.append({
                "service_id": s.get("Id") or s.get("value"),
                "account_number": s.get("AccountNumber") or s.get("ContractAccountNumber"),
                "tariff": s.get("Tarrif"),
                "address": s.get("SupplyAddress") or s.get("Address"),
                "type": s.get("Type"),
            })
        return services

    async def get_day(self, service_id: str, day: date) -> list[dict]:
        """Fetch and parse a single day (convenience wrapper; used by scripts/smoke_fetch.py)."""
        result = await self._call_ip(
            "MyAccount_ChartData", build_chartdata_input(service_id, day)
        )
        return self._parse(result)

    async def get_span(self, service_id: str, start: date, end: date) -> list[dict]:
        """Fetch and parse a contiguous span in a single request.

        MyAccount_ChartData returns half-hourly rows for every available day in
        StartDate..EndDate at once, so a whole month comes back in one call.
        """
        result = await self._call_ip(
            "MyAccount_ChartData", build_chartdata_input(service_id, start, end)
        )
        return self._parse(result)

    @staticmethod
    def _parse(result: dict) -> list[dict]:
        """Parse a ChartData result; a chunk with no Response array yields []
        (handled in chartdata._extract_rows)."""
        return parse_halfhourly_response(result)

    async def get_range(
        self, service_id: str, start: date, end: date, progress=None,
        chunk_days: int = 31, concurrency: int = 4,
    ) -> list[dict]:
        """Fetch a date range at half-hourly resolution in chunked requests.

        The portal serves the whole span per request, but very long spans are
        split into ~monthly chunks to keep each response modest, give useful
        progress, and run a few concurrently. `progress`, if given, is called
        with (days_done, total_days, latest_day). Results are chronological.
        """
        chunks: list[tuple[date, date]] = []
        cur = start
        while cur <= end:
            chunk_end = min(cur + timedelta(days=chunk_days - 1), end)
            chunks.append((cur, chunk_end))
            cur = chunk_end + timedelta(days=1)

        total_days = (end - start).days + 1
        sem = asyncio.Semaphore(max(1, concurrency))
        by_index: dict[int, list[dict]] = {}
        done_days = 0

        async def fetch(idx: int, cstart: date, cend: date):
            nonlocal done_days
            async with sem:
                intervals = await self.get_span(service_id, cstart, cend)
            by_index[idx] = intervals
            done_days += (cend - cstart).days + 1
            if progress:
                progress(min(done_days, total_days), total_days, cend)

        # TaskGroup cancels the remaining chunk requests on the first failure.
        try:
            async with asyncio.TaskGroup() as tg:
                for i, (s, e) in enumerate(chunks):
                    tg.create_task(fetch(i, s, e))
        except ExceptionGroup as eg:
            # Surface the underlying error (e.g. NotLoggedIn) rather than the group.
            raise eg.exceptions[0] from None

        intervals: list[dict] = []
        for i in range(len(chunks)):
            intervals.extend(by_index.get(i, []))
        intervals.sort(key=lambda iv: iv["timestamp"])
        return intervals

    async def close(self):
        """Tear down the browser. Idempotent; safe after a partial start()."""
        context, browser, pw = self._context, self._browser, self._pw
        self._context = self._browser = self._pw = self._page = None
        for closer in (
            context.close if context else None,
            browser.close if browser else None,
            pw.stop if pw else None,
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                pass
