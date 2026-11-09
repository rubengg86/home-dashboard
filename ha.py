"""Home Assistant client: heaters (climate entities), areas and floors.

Connects to HA's WebSocket API with a long-lived access token (HA profile ->
Security -> Long-lived access tokens), loads the area/floor/device/entity
registries, and subscribes to state changes, so the dashboard mirrors HA live.
HA's areas are the house's source of truth for rooms (phase 2 will map Tapo
devices onto them); for now they label the heater cards.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import time
from typing import Any, Awaitable, Callable

import websockets

log = logging.getLogger("tapo.ha")

Emit = Callable[[dict], Awaitable[None]]

DOMAINS = ("climate",)  # entity domains shown as heaters
REGISTRY_EVENTS = ("area_registry_updated", "floor_registry_updated",
                   "device_registry_updated", "entity_registry_updated")


class HomeAssistant:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/")
        self.ws_url = self.url.replace("http", "ws", 1) + "/api/websocket"
        self.token = token
        self.error: str | None = "connecting"
        self.floors: dict[str, str] = {}          # floor_id -> name
        self.areas: dict[str, dict] = {}          # area_id -> {"name", "floor"}
        self.entity_area: dict[str, str | None] = {}  # entity_id -> area_id
        self.states: dict[str, dict] = {}         # entity_id -> HA state (climate only)
        self.energy_sensor: dict[str, str] = {}   # climate id -> its kWh sensor (same device)
        self.power_sensor: dict[str, str] = {}    # climate id -> its W sensor
        self.sensor_states: dict[str, dict] = {}  # those sensors' states, tracked quietly
        self._power_published = 0.0               # power changes publish at most every 15 s
        self._energy: dict = {"fetched_at": 0.0, "series": []}
        self._listeners: list[Emit] = []
        self._task: asyncio.Task | None = None
        self._ws: Any = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}

    # ------------------------------------------------------------------ lifecycle
    def on_update(self, fn: Emit) -> None:
        self._listeners.append(fn)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        backoff = 2.0
        while True:
            reader = None
            try:
                async with websockets.connect(self.ws_url, max_size=16 * 2 ** 20) as ws:
                    self._ws = ws
                    msg = json.loads(await ws.recv())  # auth_required
                    if msg.get("type") == "auth_required":
                        await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                        msg = json.loads(await ws.recv())
                    if msg.get("type") != "auth_ok":
                        raise ConnectionError(f"HA rejected the token: {msg.get('message') or msg.get('type')}")
                    reader = asyncio.create_task(self._reader(ws))
                    await self._bootstrap()
                    self.error, backoff = None, 2.0
                    log.info("Home Assistant %s: %d heaters, %d areas", self.url, len(self.states), len(self.areas))
                    await self._publish()
                    await reader  # until the connection drops
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                if self.error is None:
                    log.warning("Home Assistant connection lost: %s", e)
                self.error = str(e) or type(e).__name__
                await self._publish()
            finally:
                if reader:
                    reader.cancel()
                self._ws = None
                for fut in self._pending.values():
                    fut.cancel()
                self._pending.clear()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------ protocol
    async def _cmd(self, type_: str, **kw: Any) -> Any:
        """Send one command and wait for its result."""
        if self._ws is None:
            raise ConnectionError("Home Assistant is not connected")
        self._id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        await self._ws.send(json.dumps({"id": self._id, "type": type_, **kw}))
        try:
            return await asyncio.wait_for(fut, timeout=10)
        finally:
            self._pending.pop(self._id, None)

    async def _reader(self, ws: Any) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "result":
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    if msg.get("success"):
                        fut.set_result(msg.get("result"))
                    else:
                        err = (msg.get("error") or {}).get("message", "unknown error")
                        fut.set_exception(ValueError(f"Home Assistant: {err}"))
            elif msg.get("type") == "event":
                await self._on_event(msg["event"])

    async def _bootstrap(self) -> None:
        try:
            self.floors = {f["floor_id"]: f["name"] for f in await self._cmd("config/floor_registry/list")}
        except Exception:  # noqa: BLE001 - floors arrived in HA 2024.4
            self.floors = {}
        self.areas = {a["area_id"]: {"name": a["name"], "floor": self.floors.get(a.get("floor_id"))}
                      for a in await self._cmd("config/area_registry/list")}
        device_area = {d["id"]: d.get("area_id") for d in await self._cmd("config/device_registry/list")}
        entities = await self._cmd("config/entity_registry/list")
        self.entity_area = {e["entity_id"]: e.get("area_id") or device_area.get(e.get("device_id"))
                            for e in entities}
        all_states = {s["entity_id"]: s for s in await self._cmd("get_states")}
        self.states = {eid: s for eid, s in all_states.items() if eid.split(".")[0] in DOMAINS}
        # each heater's own kWh / W sensors live on the same HA device as its climate entity
        ent_device = {e["entity_id"]: e.get("device_id") for e in entities}
        dev_entities: dict[str, list[str]] = {}
        for eid, dev in ent_device.items():
            dev_entities.setdefault(dev, []).append(eid)
        self.energy_sensor, self.power_sensor, self.sensor_states = {}, {}, {}
        for eid in self.states:
            for sib in dev_entities.get(ent_device.get(eid), []):
                s = all_states.get(sib)
                if not s or not sib.startswith("sensor."):
                    continue
                cls = s["attributes"].get("device_class")
                if cls == "energy":
                    self.energy_sensor[eid] = sib
                    self.sensor_states[sib] = s
                elif cls == "power":
                    self.power_sensor[eid] = sib
                    self.sensor_states[sib] = s
        await self._cmd("subscribe_events", event_type="state_changed")
        for ev in REGISTRY_EVENTS:
            await self._cmd("subscribe_events", event_type=ev)

    async def _on_event(self, event: dict) -> None:
        if event.get("event_type") == "state_changed":
            eid = event["data"]["entity_id"]
            if eid in self.sensor_states:  # heater power/energy: track, publish throttled
                self.sensor_states[eid] = event["data"].get("new_state") or self.sensor_states[eid]
                if time.monotonic() - self._power_published < 15:
                    return
                self._power_published = time.monotonic()
                await self._publish()
                return
            if eid.split(".")[0] not in DOMAINS:
                return
            new = event["data"].get("new_state")
            if new is None:
                self.states.pop(eid, None)
            else:
                self.states[eid] = new
        elif event.get("event_type") in REGISTRY_EVENTS:
            try:
                await self._bootstrap()  # cheap, and keeps areas/entities exact
            except Exception as e:  # noqa: BLE001
                log.warning("Registry refresh failed: %s", e)
                return
        else:
            return
        await self._publish()

    # ------------------------------------------------------------------ actions
    def _entity(self, entity_id: str) -> dict:
        if entity_id not in self.states:
            raise KeyError(entity_id)
        return self.states[entity_id]

    async def set_state(self, entity_id: str, on: bool | None = None) -> None:
        cur = self._entity(entity_id)
        target = (cur["state"] == "off") if on is None else on
        await self._cmd("call_service", domain="climate",
                        service="turn_on" if target else "turn_off",
                        target={"entity_id": entity_id})

    async def set_temperature(self, entity_id: str, value: float) -> None:
        attrs = self._entity(entity_id).get("attributes", {})
        lo, hi = attrs.get("min_temp"), attrs.get("max_temp")
        if lo is not None:
            value = max(float(lo), value)
        if hi is not None:
            value = min(float(hi), value)
        await self._cmd("call_service", domain="climate", service="set_temperature",
                        target={"entity_id": entity_id}, service_data={"temperature": round(value, 1)})

    # ------------------------------------------------------------------ snapshot
    def _area_of(self, entity_id: str) -> dict:
        return self.areas.get(self.entity_area.get(entity_id) or "", {})

    def _power_of(self, entity_id: str) -> float | None:
        s = self.sensor_states.get(self.power_sensor.get(entity_id, ""))
        try:
            return round(float(s["state"]), 1) if s else None
        except (ValueError, TypeError):
            return None

    def snapshot(self) -> list[dict]:
        out = []
        for eid, s in self.states.items():
            a = s.get("attributes", {})
            area = self._area_of(eid)
            out.append({
                "id": eid,
                "name": a.get("friendly_name") or eid,
                "area": area.get("name"),
                "floor": area.get("floor"),
                "online": s["state"] not in ("unavailable", "unknown"),
                "on": s["state"] not in ("off", "unavailable", "unknown"),
                "mode": s["state"],                        # off / heat / auto / ...
                "action": a.get("hvac_action"),            # heating / idle / off / None
                "current_c": a.get("current_temperature"),
                "target_c": a.get("temperature"),
                "min_c": a.get("min_temp"),
                "max_c": a.get("max_temp"),
                "step": a.get("target_temp_step") or 0.5,
                "power_w": self._power_of(eid),
            })
        return sorted(out, key=lambda h: ((h["area"] or "~"), h["name"].lower()))

    # ------------------------------------------------------------------ energy history
    ENERGY_CACHE_SECONDS = 300

    async def energy_history(self) -> list[dict]:
        """One combined 'Heating' series (all radiators summed) in the same shape as the
        plug/Tuya histories, from HA's long-term statistics. Cached; keeps the last
        result when HA is unreachable."""
        if not self.energy_sensor:
            return []
        if time.time() - self._energy["fetched_at"] < self.ENERGY_CACHE_SECONDS:
            return self._energy["series"]
        ids = sorted(set(self.energy_sensor.values()))
        today = dt.datetime.combine(dt.date.today(), dt.time()).astimezone()

        async def stats(start: dt.datetime, period: str) -> dict[str, list[dict]]:
            return await self._cmd("recorder/statistics_during_period", start_time=start.isoformat(),
                                   statistic_ids=ids, period=period, types=["change", "state"])

        def ok(row: dict) -> bool:
            # a meter can't grow by more than its own reading: a larger "change" is the artifact
            # HA records when a sensor first appears or its counter drops out and recovers
            change = row.get("change")
            return bool(change) and (row.get("state") is None or change <= row["state"] + 0.001)

        def merge(per_id: dict, fmt: str, days_ok: dict[str, set] | None = None) -> dict[str, float]:
            out: dict[str, float] = {}
            for sid in ids:
                for row in per_id.get(sid) or []:
                    t = dt.datetime.fromtimestamp(row["start"] / 1000)
                    if ok(row) and (days_ok is None or t.strftime("%Y-%m-%d") in days_ok[sid]):
                        out[t.strftime(fmt)] = out.get(t.strftime(fmt), 0.0) + row["change"]
            return {k: round(v, 3) for k, v in sorted(out.items())}

        try:
            per_day = await stats(today - dt.timedelta(days=400), "day")
            # a day the daily guard rejected is garbage at hourly resolution too (the recovery
            # spreads over hours that each pass the meter check on their own)
            days_ok = {sid: {dt.datetime.fromtimestamp(r["start"] / 1000).strftime("%Y-%m-%d")
                             for r in per_day.get(sid) or [] if ok(r)} for sid in ids}
            series = [{
                "id": "ha-heating", "name": "Heating", "kind": "radiator",
                "hourly": merge(await stats(today - dt.timedelta(days=6), "hour"), "%Y-%m-%dT%H", days_ok),
                "daily": merge(per_day, "%Y-%m-%d"),
                "monthly": merge(await stats(today.replace(year=today.year - 2, month=1, day=1), "month"), "%Y-%m"),
                "power_w": None,
            }]
        except Exception as e:  # noqa: BLE001
            log.warning("Heater energy history failed, using the last one: %s", e)
            return self._energy["series"]
        self._energy = {"fetched_at": time.time(), "series": series}
        return series

    def areas_snapshot(self) -> dict:
        """Floors and areas, for debugging and for phase 2 (Tapo room mapping)."""
        return {"floors": self.floors,
                "areas": [{"id": aid, **a} for aid, a in sorted(self.areas.items(), key=lambda kv: kv[1]["name"])],
                "error": self.error}

    async def _publish(self) -> None:
        msg = {"type": "climate", "heaters": self.snapshot(), "error": self.error}
        for fn in list(self._listeners):
            try:
                await fn(msg)
            except Exception as e:  # noqa: BLE001
                log.debug("listener failed: %s", e)
