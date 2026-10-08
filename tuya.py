"""Local Tuya / Smart Life switches (e.g. an immersion heater on a metering smart switch).

Reads devices.json written by `python -m tinytuya wizard` (id, local key, ip, version, DP mapping)
and talks to each device directly on the LAN. No cloud after the wizard.

Features: on/off, boost via the device's own countdown (keeps working even if the dashboard
stops), live W/A/V, energy integrated from power (persisted per day), "hot water ready" when
the tank thermostat cuts the load while the switch is still on, optional max-on safety cap.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

import tinytuya

log = logging.getLogger("tapo.tuya")
Emit = Callable[[dict], Awaitable[None]]

HEATING_W = 300      # above this the element is drawing power
IDLE_W = 30          # below this the thermostat has cut out
IDLE_POLLS = 2       # consecutive idle polls before "hot water ready"


class TuyaSwitch:
    def __init__(self, spec: dict, max_on_minutes: float) -> None:
        self.id = spec["id"]
        self.name = spec.get("name") or self.id
        self.model = spec.get("product_name") or spec.get("model") or "Tuya switch"
        self.ip = spec.get("ip") or None
        self.version = float(spec.get("version") or 3.3)
        self.key = spec["key"]
        self.max_on_minutes = max_on_minutes
        # code -> (dp index, scale) from the wizard's mapping
        self.dp: dict[str, tuple[str, int]] = {}
        for idx, m in (spec.get("mapping") or {}).items():
            self.dp[m["code"]] = (idx, int((m.get("values") or {}).get("scale", 0) or 0))
        self.fault_labels = ((spec.get("mapping") or {}).get(self.dp.get("fault", ("",))[0], {})
                             .get("values", {}).get("label", []))
        self.switch_dp = (self.dp.get("switch_1") or self.dp.get("switch") or ("1", 0))[0]
        self.countdown_dp = (self.dp.get("countdown_1") or self.dp.get("countdown") or (None,))[0]

        self.dev: tinytuya.OutletDevice | None = None
        self.lock = asyncio.Lock()
        self.dps: dict = {}
        self.online = False
        self.error: str | None = None
        self.failures = 0
        self.on_since: float | None = None
        self.was_heating = False
        self.idle_polls = 0
        self.ready = False   # thermostat satisfied since last heating

    # ------------------------------------------------------------- tinytuya (blocking, runs in a thread)
    def _connect(self) -> None:
        if not self.ip:
            found = tinytuya.find_device(self.id)  # UDP broadcast scan
            if not found or not found.get("ip"):
                raise ConnectionError("device not found on the network")
            self.ip = found["ip"]
            self.version = float(found.get("version") or self.version)
            log.info("Tuya %s found at %s (v%s)", self.name, self.ip, self.version)
        self.dev = tinytuya.OutletDevice(dev_id=self.id, address=self.ip, local_key=self.key,
                                         version=self.version)
        self.dev.set_socketPersistent(True)
        self.dev.set_socketTimeout(5)

    def _call(self, fn: str, *args):
        if self.dev is None:
            self._connect()
        res = getattr(self.dev, fn)(*args)
        if isinstance(res, dict) and ("Error" in res or "Err" in res):
            raise ConnectionError(f"{res.get('Error') or 'error'} ({res.get('Err')})")
        return res

    async def call(self, fn: str, *args):
        async with self.lock:
            try:
                return await asyncio.to_thread(self._call, fn, *args)
            except Exception:
                self.dev = None  # reconnect next time
                raise

    # ------------------------------------------------------------- values
    def value(self, code: str):
        if code not in self.dp:
            return None
        idx, scale = self.dp[code]
        v = self.dps.get(idx)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and scale:
            return v / (10 ** scale)
        return v

    @property
    def is_on(self) -> bool:
        return bool(self.dps.get(self.switch_dp))

    @property
    def power_w(self) -> float | None:
        return self.value("cur_power")

    def faults(self) -> list[str]:
        bits = self.value("fault") or 0
        names = {"ov_cr": "over-current", "ov_vol": "over-voltage", "ov_pwr": "over-power",
                 "ls_cr": "low current", "ls_vol": "low voltage", "ls_pow": "low power"}
        return [names.get(lbl, lbl) for i, lbl in enumerate(self.fault_labels) if bits & (1 << i)]


class CloudHistory:
    """Daily kWh from the Tuya cloud, i.e. what the plug has reported there (same data as the Tuya console).

    Uses the tinytuya.json credentials from the wizard. Tries the statistics API first, then falls
    back to summing the plug's add_ele reports from the device log (kept ~7 days on free projects).
    Blocking (tinytuya.Cloud), so it's called from a thread.
    """

    def __init__(self, config_file: Path) -> None:
        cfg = json.loads(config_file.read_text())
        self.cfg = {k: cfg.get(k) for k in ("apiRegion", "apiKey", "apiSecret", "apiDeviceID")}
        self.source: str | None = None
        self.error: str | None = None

    def daily_kwh(self, dev_id: str, scale: int, days: int = 30) -> dict[str, float]:
        cloud = tinytuya.Cloud(**self.cfg)
        if getattr(cloud, "error", None):
            raise ConnectionError(f"Tuya cloud login failed: {cloud.error}")
        today = dt.date.today()

        # 1) statistics API: {"days": {"20261001": "0.42", ...}}
        r = cloud.cloudrequest(f"/v1.0/devices/{dev_id}/statistics/days", query={
            "code": "add_ele",
            "start_day": (today - dt.timedelta(days=days - 1)).strftime("%Y%m%d"),
            "end_day": today.strftime("%Y%m%d"),
        })
        if isinstance(r, dict) and r.get("success") and isinstance((r.get("result") or {}).get("days"), dict):
            raw = {k: float(v) for k, v in r["result"]["days"].items() if v not in (None, "")}
            # the API normally returns kWh already; if values look unscaled, apply the DP scale
            if raw and max(raw.values()) > 100:
                raw = {k: v / 10 ** scale for k, v in raw.items()}
            self.source, self.error = "Tuya cloud statistics", None
            return {f"{k[:4]}-{k[4:6]}-{k[6:]}": v for k, v in raw.items()}
        stats_msg = (r or {}).get("msg") if isinstance(r, dict) else str(r)
        log.info("Tuya statistics API unavailable (%s), using device logs", stats_msg)

        # 2) device log: sum add_ele increments per local day
        r = cloud.getdevicelog(dev_id, start=-7, end=0, evtype=7, size=0, max_fetches=50)
        if not (isinstance(r, dict) and r.get("success")):
            raise ConnectionError(f"Tuya cloud: statistics: {stats_msg}; logs: {(r or {}).get('msg') if isinstance(r, dict) else r}")
        out: dict[str, float] = {}
        for entry in (r.get("result") or {}).get("logs", []):
            if entry.get("code") != "add_ele":
                continue
            try:
                kwh = float(entry["value"]) / 10 ** scale
                day = dt.datetime.fromtimestamp(int(entry["event_time"]) / 1000).date().isoformat()
            except (KeyError, ValueError, TypeError):
                continue
            out[day] = out.get(day, 0.0) + kwh
        self.source, self.error = "Tuya cloud device log (last 7 days)", None
        return out


class TuyaManager:
    HISTORY_EVERY = 60 * 60  # re-import cloud history hourly

    def __init__(self, devices_file: Path, energy_file: Path, poll_seconds: float = 3,
                 max_on_minutes: float = 0, price_per_kwh: float = 0, currency: str = "€",
                 cloud_config: Path | None = None) -> None:
        self.history: CloudHistory | None = None
        if cloud_config and cloud_config.exists():
            try:
                self.history = CloudHistory(cloud_config)
            except Exception as e:  # noqa: BLE001
                log.warning("Tuya cloud history disabled: %s", e)
        self.poll_seconds = poll_seconds
        self.price, self.currency = price_per_kwh, currency
        self.energy_file = energy_file
        self.energy: dict[str, dict[str, float]] = self._load_energy()  # id -> {date: kWh}
        self.devices: dict[str, TuyaSwitch] = {}
        for spec in json.loads(devices_file.read_text()):
            if spec.get("key") and not spec.get("sub"):
                d = TuyaSwitch(spec, max_on_minutes)
                self.devices[d.id] = d
                log.info("Tuya %s (%s) at %s v%s", d.name, d.model, d.ip or "auto", d.version)
        self._on_update: list[Emit] = []
        self._on_event: list[Emit] = []
        self._tasks: list[asyncio.Task] = []
        self._last_poll: dict[str, float] = {}
        self._last_save = 0.0

    def on_update(self, fn: Emit) -> None:
        self._on_update.append(fn)

    def on_event(self, fn: Emit) -> None:
        self._on_event.append(fn)

    def find_by_alias(self, alias: str) -> str | None:
        a = alias.strip().lower()
        return next((d.id for d in self.devices.values() if d.name.strip().lower() == a), None)

    # ------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        self._tasks = [asyncio.create_task(self._loop(d)) for d in self.devices.values()]
        if self.history:
            self._tasks.append(asyncio.create_task(self._history_loop()))

    async def _history_loop(self) -> None:
        while True:
            for d in self.devices.values():
                try:
                    scale = d.dp.get("add_ele", ("", 3))[1]
                    cloud = await asyncio.to_thread(self.history.daily_kwh, d.id, scale)
                    per_day = self.energy.setdefault(d.id, {})
                    for day, kwh in cloud.items():
                        # cloud is authoritative for past days; for today keep whichever is further along
                        per_day[day] = max(kwh, per_day.get(day, 0.0)) if day == dt.date.today().isoformat() else kwh
                    self._save_energy()
                    log.info("Tuya %s: imported %d days of energy from %s (%.2f kWh)",
                             d.name, len(cloud), self.history.source, sum(cloud.values()))
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001
                    self.history.error = str(e) or type(e).__name__
                    log.warning("Tuya cloud history for %s failed: %s", d.name, self.history.error)
            self._last_sent = None  # force a push with the new totals
            await self._publish()
            await asyncio.sleep(self.HISTORY_EVERY)

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._save_energy()

    async def _loop(self, d: TuyaSwitch) -> None:
        delay = 0.0
        while True:
            await asyncio.sleep(delay)
            try:
                res = await d.call("status")
                d.dps.update((res or {}).get("dps") or {})
                if not d.online:
                    log.info("Tuya %s online", d.name)
                d.online, d.error, d.failures = True, None, 0
                await self._after_poll(d)
                delay = self.poll_seconds
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                d.failures += 1
                if d.online or d.failures == 1:
                    log.warning("Tuya %s unreachable: %s", d.name, e)
                d.online, d.error = False, str(e) or type(e).__name__
                if d.failures % 5 == 0:
                    d.ip = None  # DHCP may have moved it: rescan next attempt
                delay = min(self.poll_seconds * 2 ** min(d.failures, 4), 60)
            await self._publish()

    # ------------------------------------------------------------- logic
    async def _after_poll(self, d: TuyaSwitch) -> None:
        now = time.time()
        p = d.power_w or 0.0

        # energy: integrate power, skipping gaps (offline) so we never over-count
        last = self._last_poll.get(d.id)
        self._last_poll[d.id] = now
        if last and now - last < 60 and p > 0:
            day = dt.date.today().isoformat()
            per_day = self.energy.setdefault(d.id, {})
            per_day[day] = per_day.get(day, 0.0) + p * (now - last) / 3_600_000
            if now - self._last_save > 60:
                self._save_energy()

        # on/off transitions
        if d.is_on and d.on_since is None:
            d.on_since = now
            d.ready = False
            await self._event(d, "on")
        elif not d.is_on and d.on_since is not None:
            d.on_since = None
            d.was_heating = d.ready = False
            await self._event(d, "off")

        # heating -> thermostat satisfied = hot water ready
        if d.is_on:
            if p >= HEATING_W:
                if not d.was_heating:
                    await self._event(d, "heating")
                d.was_heating, d.idle_polls, d.ready = True, 0, False
            elif p <= IDLE_W and d.was_heating:
                d.idle_polls += 1
                if d.idle_polls >= IDLE_POLLS and not d.ready:
                    d.ready = True
                    d.was_heating = False
                    await self._event(d, "hot_water_ready")

        # safety cap: only when no device countdown is running (a boost has its own end)
        countdown = d.value("countdown_1") or 0
        if (d.max_on_minutes and d.is_on and d.on_since and not countdown
                and now - d.on_since > d.max_on_minutes * 60):
            log.warning("Tuya %s on for more than %d min: switching off (safety cap)", d.name, d.max_on_minutes)
            await self.set_on(d.id, False)
            await self._event(d, "safety_off")

    async def _event(self, d: TuyaSwitch, name: str) -> None:
        ev = {"device_id": d.id, "alias": d.name, "event": name, "source": "tuya",
              "timestamp": dt.datetime.now().isoformat(timespec="seconds"),
              "received": dt.datetime.now().isoformat(timespec="seconds")}
        if name in ("on", "off"):  # still fire actions, but keep the feed readable
            ev["quiet"] = True
        log.info("Tuya %s: %s", d.name, name)
        for fn in self._on_event:
            try:
                await fn(ev)
            except Exception as e:  # noqa: BLE001
                log.debug("event listener failed: %s", e)

    # ------------------------------------------------------------- commands
    def _get(self, dev_id: str) -> TuyaSwitch:
        if dev_id not in self.devices:
            raise KeyError(dev_id)
        return self.devices[dev_id]

    async def set_on(self, dev_id: str, on: bool | None = None) -> None:
        d = self._get(dev_id)
        target = (not d.is_on) if on is None else on
        await d.call("set_value", d.switch_dp, target)
        d.dps[d.switch_dp] = target
        if not target and d.countdown_dp:
            d.dps[d.countdown_dp] = 0
        await self._refresh(d)

    async def boost(self, dev_id: str, minutes: float) -> None:
        """Turn on for N minutes using the device's own countdown (0 = cancel boost, stay as is)."""
        d = self._get(dev_id)
        if not d.countdown_dp:
            raise ValueError(f"{d.name} has no countdown timer")
        if minutes > 0 and not d.is_on:
            await d.call("set_value", d.switch_dp, True)  # switching on can reset the countdown
            await asyncio.sleep(0.3)
        await d.call("set_value", d.countdown_dp, int(max(0, min(minutes * 60, 86400))))
        await self._refresh(d)

    async def _refresh(self, d: TuyaSwitch) -> None:
        try:
            res = await d.call("status")
            d.dps.update((res or {}).get("dps") or {})
            await self._after_poll(d)
        except Exception as e:  # noqa: BLE001
            log.debug("refresh after command failed: %s", e)
        await self._publish()

    # ------------------------------------------------------------- energy storage
    def _load_energy(self) -> dict:
        try:
            return json.loads(self.energy_file.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _save_energy(self) -> None:
        cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
        for per_day in self.energy.values():
            for day in [k for k in per_day if k < cutoff]:
                del per_day[day]
        tmp = self.energy_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.energy, indent=1))
        tmp.replace(self.energy_file)
        self._last_save = time.time()

    # ------------------------------------------------------------- snapshot
    def _serialize(self, d: TuyaSwitch) -> dict:
        p = d.power_w
        per_day = self.energy.get(d.id, {})
        today = dt.date.today()
        kwh_today = per_day.get(today.isoformat(), 0.0)
        kwh_7d = sum(v for k, v in per_day.items() if k >= (today - dt.timedelta(days=6)).isoformat())
        countdown = d.value("countdown_1") or 0
        if not d.online:
            status = "Offline"
        elif not d.is_on:
            status = "Off"
        elif p is not None and p >= HEATING_W:
            status = "Heating"
        elif d.ready:
            status = "Hot"
        else:
            status = "On"
        cur_ma = d.value("cur_current")
        return {
            "id": d.id, "name": d.name, "model": d.model, "ip": d.ip, "online": d.online,
            "error": d.error, "on": d.is_on, "status": status,
            "power_w": p, "current_a": round(cur_ma / 1000, 2) if isinstance(cur_ma, (int, float)) else None,
            "voltage_v": d.value("cur_voltage"),
            "boost_ends_at": (dt.datetime.now() + dt.timedelta(seconds=countdown)).isoformat(timespec="seconds")
            if countdown and d.is_on else None,
            "on_since": dt.datetime.fromtimestamp(d.on_since).isoformat(timespec="seconds") if d.on_since else None,
            "kwh_today": round(kwh_today, 3), "kwh_7d": round(kwh_7d, 3),
            "cost_today": round(kwh_today * self.price, 2) if self.price else None,
            "cost_7d": round(kwh_7d * self.price, 2) if self.price else None,
            "currency": self.currency,
            "faults": d.faults(),
            "has_boost": bool(d.countdown_dp),
            "max_on_minutes": d.max_on_minutes or None,
            "energy_source": (self.history.source if self.history and self.history.source
                              else "measured by the dashboard"),
            "energy_error": self.history.error if self.history else None,
        }

    def snapshot(self) -> list[dict]:
        return [self._serialize(d) for d in self.devices.values()]

    async def _publish(self) -> None:
        msg = {"type": "tuya", "devices": self.snapshot()}
        encoded = json.dumps(msg, sort_keys=True)
        if encoded == getattr(self, "_last_sent", None):
            return  # nothing changed since the last push
        self._last_sent = encoded
        for fn in self._on_update:
            try:
                await fn(msg)
            except Exception as e:  # noqa: BLE001
                log.debug("update listener failed: %s", e)
