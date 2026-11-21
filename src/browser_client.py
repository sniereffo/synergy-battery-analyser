"""Synergy portal client: login in the system web view, data over Aura RPC.

Synergy's login runs an invisible reCAPTCHA score check that automation-driven
browsers fail, so the person logs in themselves in the operating system's own
web view (see webview_host.py), which runs in a helper process. Once the
portal app is running there, data calls (`MyAccount_ChartData` etc.) are made
from inside that logged-in page, so the session never leaves the web view.
Half-hourly usage is fetched in date-span requests (the portal returns every
day in StartDate..EndDate per call).
"""

import asyncio
import json
import logging
import subprocess
import sys
from collections import deque
from datetime import date, timedelta

from .aura import (
    build_apex_action,
    build_chartdata_input,
    build_ip_action,
    build_message,
    parse_ip_result,
)
from .chartdata import parse_halfhourly_response

log = logging.getLogger(__name__)

AURA_PATH = "/s/sfsites/aura"
START_TIMEOUT = 60.0      # seconds for the login window to appear
REQUEST_TIMEOUT = 180.0   # seconds for one data call
MAX_LINE = 256 * 1024 * 1024  # a year-long data reply is a few MB; allow plenty


def host_command() -> list[str]:
    """How to start the login-window helper process."""
    if getattr(sys, "frozen", False):
        # The standalone binary is its own helper (cli.main dispatches on this flag).
        return [sys.executable, "--webview-host"]
    return [sys.executable, "-m", "synergy_analyser.webview_host"]


class NotLoggedIn(RuntimeError):
    pass


class SynergyBrowserClient:
    def __init__(self):
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail: deque[str] = deque(maxlen=20)
        self._pending: dict[int, asyncio.Future] = {}
        self._ready = asyncio.Event()
        self._logged_in = False
        self._window_open = False
        self._request_id = 1000
        self.captcha_score: float | None = None  # last login bot-check result
        self.browser_name = "webview"

    async def start(self):
        """Open the login window (helper process) on Synergy's portal.

        On failure everything already started is torn down before the
        exception propagates.
        """
        try:
            kwargs = {}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # no extra console
            self._proc = await asyncio.create_subprocess_exec(
                *host_command(),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=MAX_LINE, **kwargs,
            )
            self._reader = asyncio.create_task(self._read_events())
            self._stderr_task = asyncio.create_task(self._read_stderr())
            done, _ = await asyncio.wait(
                [asyncio.create_task(self._ready.wait()), self._reader],
                timeout=START_TIMEOUT, return_when=asyncio.FIRST_COMPLETED,
            )
            if not self._ready.is_set():
                await asyncio.sleep(0.2)  # let stderr catch up for the message
                detail = self._stderr_tail[-1] if self._stderr_tail else "no details"
                raise RuntimeError(f"the login window didn't start ({detail})")
        except BaseException:
            await self.close()
            raise

    async def _read_events(self):
        """Dispatch the helper's messages until it exits."""
        try:
            async for raw in self._proc.stdout:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                self._handle(msg)
        finally:
            self._logged_in = False
            self._window_open = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(NotLoggedIn("The Synergy login window closed; log in again"))
            self._pending.clear()

    def _handle(self, msg: dict):
        event = msg.get("event")
        if event == "ready":
            self._window_open = True
            self._ready.set()
        elif event == "logged_in":
            self._logged_in = True
        elif event == "captcha":
            self.captcha_score = msg.get("score")
            if not msg.get("passed"):
                log.warning("Synergy's login check failed (score %s)", msg.get("score"))
        elif event == "closed":
            self._window_open = False
        elif "id" in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut is not None and not fut.done():
                fut.set_result((msg.get("status"), msg.get("text") or ""))

    async def _read_stderr(self):
        async for raw in self._proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            if line:
                self._stderr_tail.append(line[:300])
                log.debug("login window: %s", line)

    def _send(self, obj: dict):
        if self._proc is None or self._proc.stdin is None or self._proc.returncode is not None:
            raise NotLoggedIn("The Synergy login window isn't open; log in again")
        self._proc.stdin.write((json.dumps(obj) + "\n").encode())

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    @property
    def window_open(self) -> bool:
        """True while the login window is showing (not closed, not hidden)."""
        return self._window_open

    def _invalidate(self):
        """Forget the session after the portal rejects it."""
        self._logged_in = False

    async def wait_until_logged_in(self, timeout: float = 300.0, poll: float = 1.0):
        """Block until the user has logged in. Raises TimeoutError otherwise."""
        waited = 0.0
        while waited < timeout:
            if self.logged_in:
                return
            await asyncio.sleep(poll)
            waited += poll
        raise TimeoutError("Timed out waiting for Synergy login")

    async def finalize_login(self):
        """Hide the login window once logged in.

        The session lives in the (now hidden) page, which keeps serving data
        calls until the app closes it.
        """
        if self._window_open:
            self._send({"op": "hide"})
            self._window_open = False

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _post_action(self, action: dict) -> dict:
        """POST a single Aura action from inside the logged-in page."""
        request_id = self._next_id()
        fut = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut
        try:
            self._send({"op": "post", "id": request_id, "message": build_message([action])})
            status, text = await asyncio.wait_for(fut, REQUEST_TIMEOUT)
        finally:
            self._pending.pop(request_id, None)
        return self._parse_response(status, text)

    def _parse_response(self, status, text: str) -> dict:
        if status == 0:  # the page has no portal session (e.g. it went back to the login page)
            self._invalidate()
            raise NotLoggedIn("Your Synergy session has ended; log in again")
        if status in (401, 403):
            self._invalidate()
            raise NotLoggedIn(f"Synergy session rejected (HTTP {status}); log in again")
        if not isinstance(status, int) or not 200 <= status < 300:
            raise RuntimeError(f"Aura call failed: {'HTTP ' + str(status) if status != -1 else text[:200]}")
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
            raise NotLoggedIn("Log in to Synergy first")
        return await self._post_action(build_ip_action(ip_method, ip_input, self._next_id()))

    async def _call_apex(self, namespace: str, classname: str, method: str, params: dict) -> dict:
        """Invoke a direct @AuraEnabled Apex controller method and return its result."""
        if not self.logged_in:
            raise NotLoggedIn("Log in to Synergy first")
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
        """Close the login window and its process. Idempotent; safe after a partial start()."""
        proc, self._proc = self._proc, None
        self._logged_in = self._window_open = False
        if proc is not None and proc.returncode is None:
            try:
                proc.stdin.write(b'{"op": "close"}\n')
                await proc.stdin.drain()
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), 5)
            except Exception:  # noqa: BLE001 — not answering: make sure it goes
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
        for task in (self._reader, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader = self._stderr_task = None
