"""Samsung laundry (washer / dryer) status via the SmartThings cloud API.

Samsung appliances have no local API, so this polls https://api.smartthings.com.
Auth is pluggable (see smartthings_auth.py): OAuth with automatic refresh (recommended), or a
24 h personal access token from .env.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any, Awaitable, Callable

import httpx

from smartthings_auth import NeedsLogin, OAuth, PatAuth

log = logging.getLogger("tapo.smartthings")
API = "https://api.smartthings.com/v1"

WASHER_CAPS = {"washerOperatingState", "samsungce.washerOperatingState"}
DRYER_CAPS = {"dryerOperatingState", "samsungce.dryerOperatingState"}

Emit = Callable[[dict], Awaitable[None]]


def _val(status: dict, cap: str, attr: str) -> Any:
    return ((status.get(cap) or {}).get(attr) or {}).get("value")


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _pretty(s: Any) -> str | None:
    if not s or not isinstance(s, str):
        return None
    out = "".join(" " + c.lower() if c.isupper() else c for c in s).strip()
    return out[:1].upper() + out[1:]


class Appliance:
    def __init__(self, device: dict) -> None:
        self.id = device["deviceId"]
        self.name = device.get("label") or device.get("name") or self.id
        caps = {c["id"] for comp in device.get("components", []) for c in comp.get("capabilities", [])}
        self.kind = "washer" if caps & WASHER_CAPS else "dryer"
        self.state: dict = {}
        self.prev_machine_state: str | None = None

    def update(self, status: dict) -> dict:
        main = (status.get("components") or {}).get("main") or {}
        k = self.kind  # "washer" / "dryer"
        ce = f"samsungce.{k}OperatingState"
        std = f"{k}OperatingState"

        machine = _val(main, std, "machineState") or _val(main, ce, "operatingState")  # run | pause | stop | ready
        job = _val(main, ce, "jobState") or _val(main, std, f"{k}JobState")
        ends_at = _parse_time(_val(main, std, "completionTime"))
        remaining = _val(main, ce, "remainingTime")  # minutes
        progress = _val(main, ce, "progress")         # percent
        power = _val(main, "switch", "switch")
        pcr = _val(main, "powerConsumptionReport", "powerConsumption") or {}

        running = machine in ("run", "pause")
        if running and remaining is None and ends_at:
            remaining = max(0, round((ends_at - dt.datetime.now(dt.timezone.utc)).total_seconds() / 60))
        if running and ends_at is None and isinstance(remaining, (int, float)):
            ends_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=remaining)

        self.state = {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "online": True,
            "power": power,                              # on | off
            "machine_state": machine,
            "status": ("Paused" if machine == "pause" else "Running" if machine == "run"
                       else "Off" if power == "off" else "Idle"),
            "phase": _pretty(job) if running or job in ("finish", "finished") else None,
            "remaining_min": remaining if running else None,
            "ends_at": ends_at.isoformat() if running and ends_at else None,
            "progress": progress if running else None,
            "remote_control": _val(main, "remoteControlStatus", "remoteControlEnabled"),
            "power_w": pcr.get("power") if isinstance(pcr, dict) else None,
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
        }
        return self.state


class SmartThings:
    def __init__(self, auth: OAuth | PatAuth, poll_seconds: float = 30,
                 device_ids: list[str] | None = None) -> None:
        self.auth = auth
        self._wake = asyncio.Event()
        self.poll_seconds = poll_seconds
        self.device_ids = [d.strip() for d in (device_ids or []) if d.strip()]
        self.appliances: dict[str, Appliance] = {}
        self.error: str | None = None
        self.client = httpx.AsyncClient(base_url=API, timeout=15)
        self.task: asyncio.Task | None = None
        self._on_update: list[Emit] = []
        self._on_event: list[Emit] = []

    def on_update(self, fn: Emit) -> None:
        self._on_update.append(fn)

    def on_event(self, fn: Emit) -> None:
        self._on_event.append(fn)

    def snapshot(self) -> dict:
        return {"error": self.error, "auth": self.auth.status(),
                "appliances": [a.state for a in self.appliances.values() if a.state]}

    def poke(self) -> None:
        """Poll now (e.g. right after the user connected)."""
        self._wake.set()

    async def start(self) -> None:
        await self.auth.start()
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.auth.stop()
        await self.client.aclose()

    # -------------------------------------------------------------- http
    async def _get(self, path: str) -> dict:
        for attempt in (1, 2):
            token = await self.auth.get_token()  # OAuth refreshes here when close to expiry
            r = await self.client.get(path, headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 401 and attempt == 1 and await self.auth.on_unauthorized():
                continue
            if r.status_code == 401:
                if self.auth.mode == "pat":
                    raise PermissionError("SmartThings token expired or invalid. Paste a new one in .env "
                                          "(SMARTTHINGS_TOKEN), or switch to OAuth for automatic renewal.")
                raise NeedsLogin("SmartThings rejected the login, connect again")
            if r.status_code == 429:
                raise RuntimeError("SmartThings rate limit hit; backing off")
            r.raise_for_status()
            return r.json()
        raise RuntimeError("unreachable")

    async def _discover(self) -> None:
        items: list[dict] = []
        path: str | None = "/devices"
        while path:
            data = await self._get(path)
            items += data.get("items", [])
            nxt = (data.get("_links") or {}).get("next") or {}
            path = nxt.get("href", "").replace(API, "") or None
        for d in items:
            caps = {c["id"] for comp in d.get("components", []) for c in comp.get("capabilities", [])}
            wanted = d["deviceId"] in self.device_ids if self.device_ids else bool(caps & (WASHER_CAPS | DRYER_CAPS))
            if wanted and d["deviceId"] not in self.appliances:
                a = Appliance(d)
                self.appliances[a.id] = a
                log.info("SmartThings %s: %s (%s)", a.kind, a.name, a.id)
        if not self.appliances:
            log.warning("SmartThings: no washer/dryer found on this account (%d devices total)", len(items))

    # -------------------------------------------------------------- polling
    async def _loop(self) -> None:
        delay = 0.0
        while True:
            try:
                if not self.appliances:
                    await self._discover()
                for a in list(self.appliances.values()):
                    try:
                        state = a.update(await self._get(f"/devices/{a.id}/status"))
                    except (PermissionError, RuntimeError, NeedsLogin):
                        raise
                    except Exception as e:  # noqa: BLE001
                        log.warning("SmartThings %s: %s", a.name, e)
                        a.state = {**(a.state or {"id": a.id, "name": a.name, "kind": a.kind}),
                                   "online": False, "error": str(e)}
                        continue
                    await self._detect_events(a, state)
                self.error = None
                delay = self.poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                msg = str(e) or type(e).__name__
                if msg != self.error:
                    log.warning("SmartThings: %s", msg)
                self.error = msg
                delay = (30 if isinstance(e, NeedsLogin) else 300 if isinstance(e, PermissionError)
                         else max(self.poll_seconds * 2, 120))
            await self._fire(self._on_update, {"type": "appliances", **self.snapshot()})
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            delay = 0.0

    async def _detect_events(self, a: Appliance, state: dict) -> None:
        now_ms, prev = state["machine_state"], a.prev_machine_state
        a.prev_machine_state = now_ms
        if prev is None or now_ms == prev:
            return  # first poll, or nothing changed
        event = None
        if now_ms == "run" and prev != "pause":
            event = "started"
        elif prev in ("run", "pause") and now_ms not in ("run", "pause"):
            event = "finished"
        if event:
            ev = {
                "device_id": a.id,
                "alias": a.name,
                "event": event,
                "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
                "received": dt.datetime.now().isoformat(timespec="seconds"),
                "source": "smartthings",
            }
            log.info("%s %s", a.name, event)
            await self._fire(self._on_event, ev)

    @staticmethod
    async def _fire(fns: list[Emit], msg: dict) -> None:
        for fn in fns:
            try:
                await fn(msg)
            except Exception as e:  # noqa: BLE001
                log.debug("listener failed: %s", e)
