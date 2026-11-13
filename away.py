"""Out-of-home mode: a one-off plan for when nobody is home.

away.json (in the data dir): {"from": "2026-11-14T10:00", "until": "2026-11-16T18:00",
"cap_c": 15.0, "boosted": false} — times are local, minute precision.

From departure until return, any radiator target above the cap is pulled back down
(every tick, so manual changes don't stick) and any Tuya switch found on is switched
off. During the last hour before the return the immersion heater is boosted instead,
so there's hot water on arrival (the plug's own countdown bounds it even if the
dashboard dies). The weekly heating schedule pauses meanwhile and re-applies its
current block when the plan ends or is cancelled. The plan removes itself at the
return time.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger("tapo.away")

Emit = Callable[[dict], Awaitable[None]]

FMT = "%Y-%m-%dT%H:%M"
BOOST_MINUTES = 60


def _parse(s: str) -> dt.datetime:
    try:
        return dt.datetime.strptime(s, FMT)
    except (TypeError, ValueError):
        raise ValueError(f"bad time {s!r} (use YYYY-MM-DDTHH:MM)")


class AwayManager:
    TICK_SECONDS = 30

    def __init__(self, file: Path, ha: Any = None, tuya: Any = None, schedule: Any = None) -> None:
        self.file = file
        self.ha, self.tuya, self.schedule = ha, tuya, schedule
        self.plan: dict | None = self._load()
        self._listeners: list[Emit] = []
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle
    def on_update(self, fn: Emit) -> None:
        self._listeners.append(fn)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def _load(self) -> dict | None:
        try:
            p = json.loads(self.file.read_text())
            _parse(p["from"]), _parse(p["until"])  # noqa: B018 - validate
            return p
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            log.error("Ignoring %s: %s", self.file.name, e)
            return None

    def _save(self) -> None:
        if self.plan is None:
            self.file.unlink(missing_ok=True)
            return
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.plan, indent=1))
        tmp.replace(self.file)

    # ------------------------------------------------------------------ state
    def active(self, now: dt.datetime | None = None) -> bool:
        if not self.plan:
            return False
        now = now or dt.datetime.now()
        return _parse(self.plan["from"]) <= now < _parse(self.plan["until"])

    def snapshot(self) -> dict:
        return {"plan": self.plan, "active": self.active()}

    async def set(self, data: dict) -> dict:
        now = dt.datetime.now()
        frm = _parse(data["from"]) if data.get("from") else now
        until = _parse(data.get("until") or "")
        if until <= max(now, frm):
            raise ValueError("the return time must be after now and after the departure")
        cap = round(float(data.get("cap_c", 15)) * 2) / 2
        if not 5 <= cap <= 30:
            raise ValueError(f"max temperature {cap} °C is outside 5-30")
        self.plan = {"from": frm.strftime(FMT), "until": until.strftime(FMT), "cap_c": cap,
                     "boosted": False}
        self._save()
        log.info("Out of home: %s -> %s, heaters capped at %.1f °C", self.plan["from"], self.plan["until"], cap)
        await self._publish()
        await self._tick()  # if we're already away, enforce right now
        return self.snapshot()

    async def cancel(self) -> dict:
        if self.plan:
            log.info("Out of home cancelled")
        self.plan = None
        self._save()
        self._resume_schedule()
        await self._publish()
        return self.snapshot()

    def _resume_schedule(self) -> None:
        if self.schedule:
            self.schedule.reapply()  # re-send the current block's targets on its next tick

    # ------------------------------------------------------------------ engine
    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Away tick failed: %s", e)
            await asyncio.sleep(self.TICK_SECONDS)

    async def _tick(self) -> None:
        if not self.plan:
            return
        now = dt.datetime.now()
        frm, until = _parse(self.plan["from"]), _parse(self.plan["until"])
        if now >= until:
            log.info("Out of home ended, back to the normal schedule")
            self.plan = None
            self._save()
            self._resume_schedule()
            await self._publish()
            return
        if now < frm:
            return
        # away: cap the radiators
        cap = self.plan["cap_c"]
        if self.ha:
            for h in self.ha.snapshot():
                if h["online"] and h["on"] and h["target_c"] is not None and h["target_c"] > cap + 0.01:
                    try:
                        await self.ha.set_temperature(h["id"], cap)
                        log.info("Away: %s capped %.1f -> %.1f °C", h["name"], h["target_c"], cap)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Away: could not cap %s: %s", h.get("name"), e)
        if not self.tuya:
            return
        if now >= until - dt.timedelta(minutes=BOOST_MINUTES):
            # the last hour: boost the water heater once, so it's hot on arrival
            if not self.plan.get("boosted"):
                done = True
                for d in self.tuya.snapshot():
                    if not d.get("has_boost"):
                        continue
                    try:
                        await self.tuya.boost(d["id"], BOOST_MINUTES)
                        log.info("Away: boosted %s for the hour before the return", d["name"])
                    except Exception as e:  # noqa: BLE001
                        done = False  # offline plug: retried next tick
                        log.warning("Away: could not boost %s: %s", d.get("name"), e)
                if done:
                    self.plan["boosted"] = True
                    self._save()
            return  # and don't switch it off meanwhile
        # the rest of the stay: keep the water heater off
        for d in self.tuya.snapshot():
            if d.get("online") and d.get("on"):
                try:
                    await self.tuya.set_on(d["id"], False)
                    log.info("Away: switched %s off", d["name"])
                except Exception as e:  # noqa: BLE001
                    log.warning("Away: could not switch %s off: %s", d.get("name"), e)

    async def _publish(self) -> None:
        msg = {"type": "away", **self.snapshot()}
        for fn in list(self._listeners):
            try:
                await fn(msg)
            except Exception as e:  # noqa: BLE001
                log.debug("listener failed: %s", e)
