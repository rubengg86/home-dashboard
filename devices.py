"""Local Tapo device manager built on python-kasa.

Discovers devices on the LAN, polls each one on its own interval, keeps a
serialised snapshot of their state, and turns S200B trigger-log entries into
button events.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import enum
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from kasa import Credentials, Device, Discover, Feature, Module

log = logging.getLogger("tapo.devices")

# Feature ids we never show (noise / debug-ish values).
HIDDEN_FEATURES = {
    "state", "brightness", "device_id", "ssid", "rssi", "signal_level",
    "current_firmware_version", "available_firmware_version", "check_latest_firmware",
    "update_available", "cloud_connection", "overheated", "reboot", "factory_reset",
}
READ_ONLY_TYPES = {Feature.Type.Sensor, Feature.Type.BinarySensor}

Listener = Callable[[dict], Awaitable[None]]


def _jsonable(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return int(value.total_seconds())
    if isinstance(value, enum.Enum):
        return value.name if not isinstance(value.value, (str, int)) else value.value
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def _device_type(dev: Device) -> str:
    t = getattr(dev, "device_type", None)
    return str(getattr(t, "value", t) or "unknown").lower()


def _device_id(dev: Device) -> str:
    return str(getattr(dev, "device_id", None) or dev.host)


@dataclass
class Tracked:
    """A top-level device (bulb, plug, hub...) with its poll task."""
    dev: Device
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    online: bool = True
    error: str | None = None
    task: asyncio.Task | None = None


class DeviceManager:
    def __init__(
        self,
        username: str,
        password: str,
        hosts: list[str] | None = None,
        poll_seconds: float = 5,
        hub_poll_seconds: float = 1.5,
        rediscover_seconds: float = 300,
        protected: list[str] | None = None,
        energy_file: Path | None = None,
    ) -> None:
        self.creds = Credentials(username, password)
        self.hosts = [h.strip() for h in (hosts or []) if h.strip()]
        self.poll_seconds = poll_seconds
        self.hub_poll_seconds = hub_poll_seconds
        self.rediscover_seconds = rediscover_seconds
        # aliases that ask for confirmation before switching off and are skipped by "All off" (e.g. a fridge)
        self.protected = {a.strip().lower() for a in (protected or []) if a.strip()}

        self.tracked: dict[str, Tracked] = {}        # host -> Tracked
        self.by_id: dict[str, tuple[Device, Tracked]] = {}  # device id -> (device or child, parent)
        self.events: deque[dict] = deque(maxlen=50)
        self._last_log_id: dict[str, int] = {}       # child id -> newest trigger log id seen
        self._trigger_failures: dict[str, int] = {}  # child id -> consecutive get_trigger_logs failures
        self._listeners: list[Listener] = []
        self._snapshot_cache: str | None = None
        self._discover_task: asyncio.Task | None = None
        # plug energy history, merged from each plug's memory and kept on disk: survives restarts and
        # outlives the plug's own window (7 days of hours). id -> {name, hourly, daily, monthly, fetched_at}
        self.energy_file = energy_file
        self._plug_energy: dict[str, dict] = self._load_plug_energy()
        self._energy_lock = asyncio.Lock()

    # ------------------------------------------------------------------ lifecycle
    def subscribe(self, fn: Listener) -> None:
        self._listeners.append(fn)

    async def start(self) -> None:
        await self.discover()
        self._discover_task = asyncio.create_task(self._rediscover_loop())

    async def stop(self) -> None:
        tasks = [t.task for t in self.tracked.values() if t.task]
        if self._discover_task:
            tasks.append(self._discover_task)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for t in self.tracked.values():
            try:
                await t.dev.disconnect()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ discovery
    async def discover(self) -> None:
        self._trigger_failures.clear()
        found: dict[str, Device] = {}
        if self.hosts:
            results = await asyncio.gather(
                *(Discover.discover_single(h, credentials=self.creds) for h in self.hosts),
                return_exceptions=True,
            )
            for host, res in zip(self.hosts, results):
                if isinstance(res, Exception):
                    log.warning("Could not reach %s: %s", host, res)
                elif res is not None:
                    found[host] = res
        else:
            try:
                found = await Discover.discover(credentials=self.creds)
            except Exception as e:  # noqa: BLE001
                log.error("Broadcast discovery failed: %s", e)

        new = [(h, d) for h, d in found.items() if h not in self.tracked]
        for host, dev in new:
            tr = Tracked(dev=dev)
            try:
                async with tr.lock:
                    await dev.update()
            except Exception as e:  # noqa: BLE001
                log.warning("Initial update failed for %s: %s", host, e)
                tr.online, tr.error = False, str(e)
            self.tracked[host] = tr
            self._index(tr)
            await self._poll_trigger_logs(tr, emit=False)
            interval = self.hub_poll_seconds if _device_type(dev) == "hub" else self.poll_seconds
            tr.task = asyncio.create_task(self._poll_loop(tr, interval))
            log.info("Tracking %s %s (%s) at %s", dev.model, dev.alias, _device_type(dev), host)
        if new:
            await self._publish()
        if not self.tracked:
            log.warning("No devices found. Set TAPO_HOSTS if discovery is blocked on your network.")

    async def _rediscover_loop(self) -> None:
        while True:
            await asyncio.sleep(self.rediscover_seconds)
            await self.discover()

    def _index(self, tr: Tracked) -> None:
        self.by_id[_device_id(tr.dev)] = (tr.dev, tr)
        for child in tr.dev.children or []:
            self.by_id[_device_id(child)] = (child, tr)

    # ------------------------------------------------------------------ polling
    async def _poll_loop(self, tr: Tracked, interval: float) -> None:
        backoff = interval
        while True:
            await asyncio.sleep(backoff)
            try:
                async with tr.lock:
                    await asyncio.wait_for(tr.dev.update(), timeout=10)
                tr.online, tr.error, backoff = True, None, interval
                self._index(tr)  # hub children can change
                await self._poll_trigger_logs(tr, emit=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if tr.online:
                    log.warning("%s went offline: %s", tr.dev.host, e)
                tr.online, tr.error = False, str(e) or type(e).__name__
                backoff = min(backoff * 2, 60)
            await self._publish()

    async def refresh(self, tr: Tracked) -> None:
        async with tr.lock:
            await tr.dev.update()
        tr.online, tr.error = True, None
        await self._publish(force=True)

    # ------------------------------------------------------------------ buttons
    # python-kasa refreshes the TriggerLogs module of hub children at most once a day
    # (MINIMUM_HUB_CHILD_UPDATE_INTERVAL_SECS), so we query get_trigger_logs ourselves on
    # every hub poll. The request goes through the hub via the child's control_child wrapper.
    async def _poll_trigger_logs(self, tr: Tracked, emit: bool) -> None:
        for child in tr.dev.children or []:
            if "TriggerLogs" not in getattr(child, "modules", {}):
                continue  # child has no trigger_log component (bulbs, plugs, ...)
            cid = _device_id(child)
            if self._trigger_failures.get(cid, 0) >= 5:
                continue  # stop hammering a child that keeps failing; reset on rediscovery
            try:
                async with tr.lock:
                    resp = await asyncio.wait_for(
                        child.protocol.query({"get_trigger_logs": {"start_id": 0}}), timeout=5)
                logs = (resp.get("get_trigger_logs") or {}).get("logs") or []
                self._trigger_failures.pop(cid, None)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                n = self._trigger_failures[cid] = self._trigger_failures.get(cid, 0) + 1
                log.warning("Trigger logs for %s failed (%d/5): %s", child.alias, n, e)
                continue
            self._handle_trigger_logs(child, logs, emit)

    async def debug_buttons(self) -> list[dict]:
        """Raw trigger-log responses for every hub child, for troubleshooting."""
        out = []
        for tr in self.tracked.values():
            for child in tr.dev.children or []:
                item = {"alias": child.alias, "model": child.model, "id": _device_id(child),
                        "has_trigger_log": "TriggerLogs" in getattr(child, "modules", {}),
                        "last_seen_id": self._last_log_id.get(_device_id(child)),
                        "failures": self._trigger_failures.get(_device_id(child), 0)}
                if item["has_trigger_log"]:
                    try:
                        async with tr.lock:
                            item["response"] = await child.protocol.query({"get_trigger_logs": {"start_id": 0}})
                    except Exception as e:  # noqa: BLE001
                        item["error"] = f"{type(e).__name__}: {e}"
                out.append(item)
        return out

    def _handle_trigger_logs(self, child: Device, logs: list[dict], emit: bool) -> None:
        if not logs:
            return
        cid = _device_id(child)
        newest = max(int(e["id"]) for e in logs)
        last = self._last_log_id.get(cid)
        self._last_log_id[cid] = max(newest, last or 0)
        if last is None or not emit:
            return  # first sight: remember where we are, don't replay old presses
        for entry in sorted(logs, key=lambda e: int(e["id"])):
            if int(entry["id"]) <= last:
                continue
            ts = entry.get("timestamp")
            event = {
                "device_id": cid,
                "alias": child.alias,
                "event": str(entry.get("event", "")),
                "timestamp": dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
                if isinstance(ts, (int, float)) else _jsonable(ts),
                "received": dt.datetime.now().isoformat(timespec="seconds"),
            }
            self.events.appendleft(event)
            log.info("Button %s: %s", child.alias, event["event"])
            asyncio.create_task(self._emit({"type": "event", "event": event}))

    # ------------------------------------------------------------------ actions
    def _lookup(self, device_id: str) -> tuple[Device, Tracked]:
        if device_id not in self.by_id:
            raise KeyError(device_id)
        return self.by_id[device_id]

    def find_by_alias(self, alias: str) -> str | None:
        alias = alias.strip().lower()
        for did, (dev, _) in self.by_id.items():
            if (dev.alias or "").strip().lower() == alias:
                return did
        return None

    async def set_on(self, device_id: str, on: bool | None = None) -> None:
        dev, tr = self._lookup(device_id)
        feat = dev.features.get("state")
        if feat is None:
            raise ValueError(f"{dev.alias} cannot be switched")
        target = (not feat.value) if on is None else on
        async with tr.lock:
            await feat.set_value(target)
        await self.refresh(tr)

    async def set_brightness(self, device_id: str, value: int) -> None:
        dev, tr = self._lookup(device_id)
        feat = dev.features.get("brightness")
        if feat is None:
            raise ValueError(f"{dev.alias} is not dimmable")
        lo = getattr(feat, "minimum_value", 0) or 0
        hi = getattr(feat, "maximum_value", 100) or 100
        async with tr.lock:
            await feat.set_value(max(lo, min(hi, int(value))))
        await self.refresh(tr)

    async def all_off(self) -> int:
        targets = [
            did for did, (dev, _) in self.by_id.items()
            if (f := dev.features.get("state")) is not None and f.value and not self._protected(dev)
        ]
        results = await asyncio.gather(*(self.set_on(d, False) for d in targets), return_exceptions=True)
        for d, r in zip(targets, results):
            if isinstance(r, Exception):
                log.warning("all_off failed for %s: %s", d, r)
        return len(targets)

    # ------------------------------------------------------------------ energy history
    ENERGY_CACHE_SECONDS = 300

    async def energy_history(self) -> list[dict]:
        """kWh per hour, day and month for every energy-monitoring plug. Each plug is re-read from its own
        memory at most every ENERGY_CACHE_SECONDS (it's slow to answer); a plug that isn't discovered yet
        or fails to answer keeps its saved history and is retried on the next call."""
        async with self._energy_lock:  # one read per plug even if several clients ask at once
            now, changed = time.time(), False
            for tr in list(self.tracked.values()):
                dev = tr.dev
                if not tr.online or Module.Energy not in dev.modules:
                    continue
                did = _device_id(dev)
                rec = self._plug_energy.get(did)
                if rec and now - rec.get("fetched_at", 0) < self.ENERGY_CACHE_SECONDS:
                    continue
                try:
                    fresh = await self._plug_history(dev)
                except Exception as e:  # noqa: BLE001
                    log.warning("Energy history for %s failed, using the saved one: %s", dev.alias, e)
                    continue
                rec = self._plug_energy.setdefault(did, {"hourly": {}, "daily": {}, "monthly": {}})
                for k in ("hourly", "daily", "monthly"):
                    rec[k].update(fresh[k])  # the plug is authoritative for what it still remembers
                rec["name"], rec["fetched_at"] = dev.alias or dev.model, now
                changed = True
            if changed:
                self._save_plug_energy()
            return [{"id": did, "name": rec.get("name") or did, "kind": "plug",
                     "hourly": rec["hourly"], "daily": rec["daily"], "monthly": rec["monthly"],
                     "power_w": self._live_power(did)} for did, rec in self._plug_energy.items()]

    def _live_power(self, did: str) -> float | None:
        found = self.by_id.get(did)
        if not found or not found[1].online or Module.Energy not in found[0].modules:
            return None
        return found[0].modules[Module.Energy].current_consumption

    def _load_plug_energy(self) -> dict:
        if not self.energy_file:
            return {}
        try:
            return json.loads(self.energy_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            log.error("Could not read %s: %s", self.energy_file, e)
            return {}

    def _save_plug_energy(self) -> None:
        if not self.energy_file:
            return
        today = dt.date.today()
        cut = {"hourly": (today - dt.timedelta(days=8)).isoformat(), "daily": (today - dt.timedelta(days=400)).isoformat(),
               "monthly": f"{today.year - 3}-{today.month:02d}"}
        for rec in self._plug_energy.values():
            for k, c in cut.items():
                rec[k] = {key: v for key, v in rec[k].items() if key >= c}
        tmp = self.energy_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._plug_energy, indent=1))
            tmp.replace(self.energy_file)
        except OSError as e:
            log.error("Could not save plug energy history to %s: %s", self.energy_file, e)

    @staticmethod
    async def _plug_history(dev: Device) -> dict:
        async def query(start: dt.datetime, end: dt.datetime, interval: int) -> tuple[dt.datetime, list[int]]:
            res = await dev.protocol.query({"get_energy_data": {
                "start_timestamp": int(start.timestamp()), "end_timestamp": int(end.timestamp()), "interval": interval}})
            data = res.get("get_energy_data", res)
            return dt.datetime.fromtimestamp(data["start_timestamp"]), data.get("data") or []

        now = dt.datetime.now()
        today = dt.datetime.combine(dt.date.today(), dt.time())
        hourly, daily, monthly = {}, {}, {}  # values in Wh from the plug, stored as kWh

        start, data = await query(today - dt.timedelta(days=6), now, 60)  # max 7 days per request
        for i, wh in enumerate(data):
            t = start + dt.timedelta(hours=i)
            if t <= now:
                hourly[t.strftime("%Y-%m-%dT%H")] = wh / 1000

        # daily data only comes a calendar quarter at a time, starting on the quarter's first day
        def quarter(d: dt.date) -> dt.datetime:
            return dt.datetime(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
        q = quarter((today - dt.timedelta(days=59)).date())
        while q <= now:
            nxt = dt.datetime(q.year + (q.month > 9), (q.month + 2) % 12 + 1, 1)
            start, data = await query(q, min(nxt - dt.timedelta(seconds=1), now), 1440)
            for i, wh in enumerate(data):
                d = start.date() + dt.timedelta(days=i)
                if d <= now.date():
                    daily[d.isoformat()] = wh / 1000
            q = nxt

        for year in (now.year - 1, now.year):
            start, data = await query(dt.datetime(year, 1, 1), min(dt.datetime(year, 12, 31, 23, 59, 59), now), 43200)
            for i, wh in enumerate(data[:12]):
                if (year, i + 1) <= (now.year, now.month):
                    monthly[f"{year}-{i + 1:02d}"] = wh / 1000

        energy = dev.modules[Module.Energy]
        return {"hourly": hourly, "daily": daily, "monthly": monthly, "power_w": energy.current_consumption}

    def _protected(self, dev: Device) -> bool:
        return (dev.alias or "").strip().lower() in self.protected

    # ------------------------------------------------------------------ snapshot
    def _serialize(self, dev: Device, tr: Tracked, is_child: bool) -> dict:
        state = dev.features.get("state")
        bright = dev.features.get("brightness")
        sensors = []
        for fid, feat in dev.features.items():
            if fid in HIDDEN_FEATURES or feat.type not in READ_ONLY_TYPES:
                continue
            if getattr(feat, "category", None) == Feature.Category.Debug:
                continue
            try:
                value = feat.value
            except Exception:  # noqa: BLE001
                continue
            sensors.append({"id": fid, "name": feat.name, "value": _jsonable(value),
                            "unit": getattr(feat, "unit", None)})
        data = {
            "id": _device_id(dev),
            "alias": dev.alias or dev.model,
            "model": dev.model,
            "type": _device_type(dev),
            "host": tr.dev.host,
            "online": tr.online,
            "error": None if is_child else tr.error,
            "switchable": state is not None,
            "on": bool(state.value) if state is not None else None,
            "confirm_off": self._protected(dev),
            "dimmable": bright is not None,
            "brightness": bright.value if bright is not None else None,
            "sensors": sensors,
        }
        if not is_child:
            data["children"] = [self._serialize(c, tr, True) for c in (dev.children or [])]
        return data

    def snapshot(self) -> list[dict]:
        out = []
        for tr in self.tracked.values():
            try:
                out.append(self._serialize(tr.dev, tr, False))
            except Exception as e:  # noqa: BLE001
                log.debug("serialize failed for %s: %s", tr.dev.host, e)
                out.append({"id": tr.dev.host, "alias": tr.dev.host, "model": "?", "type": "unknown",
                            "host": tr.dev.host, "online": False, "error": str(e), "switchable": False,
                            "on": None, "dimmable": False, "brightness": None, "sensors": [], "children": []})
        return sorted(out, key=lambda d: (d["type"] != "hub", d["alias"].lower()))

    async def _publish(self, force: bool = False) -> None:
        import json
        snap = self.snapshot()
        encoded = json.dumps(snap, sort_keys=True)
        if not force and encoded == self._snapshot_cache:
            return
        self._snapshot_cache = encoded
        await self._emit({"type": "devices", "devices": snap})

    async def _emit(self, msg: dict) -> None:
        for fn in list(self._listeners):
            try:
                await fn(msg)
            except Exception as e:  # noqa: BLE001
                log.debug("listener failed: %s", e)
