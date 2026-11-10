"""Weekly heating schedule, applied to Home Assistant radiators.

heating_schedule.json (in the data dir) holds programs, each covering some
weekdays with blocks that start at a time and run until the next block:
{"enabled": true, "programs": [{"name": "Weekdays", "days": [0,1,2,3,4],
  "blocks": [{"time": "07:00", "targets": {"climate.x": 19.0, ...}}, ...]}]}

Targets are sent once when a block begins (and on startup / after an edit),
so a manual change on a radiator holds until the next block boundary, like a
classic thermostat. days: 0 = Monday ... 6 = Sunday; each day belongs to at
most one program.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
from pathlib import Path

from ha import HomeAssistant

log = logging.getLogger("tapo.schedule")

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validate(data: dict) -> dict:
    """Returns a normalised copy or raises ValueError."""
    if not isinstance(data, dict):
        raise ValueError("schedule must be an object")
    out = {"enabled": bool(data.get("enabled")), "programs": []}
    all_days: list[int] = []
    for p in data.get("programs") or []:
        days = sorted({int(d) for d in (p.get("days") or [])})
        if not days or days[0] < 0 or days[-1] > 6:
            raise ValueError("days must be 0 (Monday) to 6 (Sunday)")
        all_days += days
        blocks, seen = [], set()
        for b in p.get("blocks") or []:
            t = str(b.get("time") or "")
            if not TIME_RE.match(t):
                raise ValueError(f"bad block time {t!r} (use HH:MM)")
            if t in seen:
                raise ValueError(f"two blocks start at {t}")
            seen.add(t)
            targets = {}
            for eid, v in (b.get("targets") or {}).items():
                v = float(v)
                if not 5 <= v <= 30:
                    raise ValueError(f"target {v} °C for {eid} is outside 5-30")
                targets[str(eid)] = round(v * 2) / 2
            blocks.append({"time": t, "targets": targets})
        if not blocks:
            raise ValueError(f"program {p.get('name')!r} has no blocks")
        out["programs"].append({"name": str(p.get("name") or "Schedule"), "days": days,
                                "blocks": sorted(blocks, key=lambda b: b["time"])})
    if len(all_days) != len(set(all_days)):
        raise ValueError("a weekday appears in two programs")
    return out


class HeatingSchedule:
    TICK_SECONDS = 30

    def __init__(self, ha: HomeAssistant, file: Path) -> None:
        self.ha = ha
        self.file = file
        self.error: str | None = None
        self.data = self._load()
        self._applied: tuple | None = None  # (program idx, block time) last sent successfully
        self._task: asyncio.Task | None = None

    def _load(self) -> dict:
        try:
            return validate(json.loads(self.file.read_text()))
        except FileNotFoundError:
            return {"enabled": False, "programs": []}
        except Exception as e:  # noqa: BLE001
            log.error("Ignoring %s: %s", self.file.name, e)
            self.error = f"{self.file.name}: {e}"
            return {"enabled": False, "programs": []}

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def snapshot(self) -> dict:
        return {**self.data, "error": self.error}

    async def save(self, data: dict) -> dict:
        self.data = validate(data)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.file)
        self.error = None
        self._applied = None  # re-send the current block with the new numbers
        log.info("Heating schedule saved: %d program(s), enabled=%s",
                 len(self.data["programs"]), self.data["enabled"])
        return self.snapshot()

    # ------------------------------------------------------------------ engine
    def current_block(self, now: dt.datetime | None = None) -> tuple[tuple, dict] | None:
        """((program idx, block time), targets) in effect right now, or None."""
        now = now or dt.datetime.now()
        hhmm = now.strftime("%H:%M")
        for i, p in enumerate(self.data["programs"]):
            if now.weekday() not in p["days"]:
                continue
            started = [b for b in p["blocks"] if b["time"] <= hhmm]
            block = started[-1] if started else p["blocks"][-1]  # before the first block: wrap to the last
            return (i, block["time"]), block["targets"]
        return None

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("Schedule tick failed: %s", e)
            await asyncio.sleep(self.TICK_SECONDS)

    async def _tick(self) -> None:
        if not self.data["enabled"]:
            return
        cur = self.current_block()
        if cur is None or cur[0] == self._applied:
            return
        key, targets = cur
        failed = False
        for eid, temp in targets.items():
            if eid not in self.ha.states:
                continue  # radiator HA doesn't know (renamed/removed): skip quietly
            try:
                await self.ha.set_temperature(eid, temp)
                log.info("Schedule: %s -> %.1f °C (block %s)", eid, temp, key[1])
            except Exception as e:  # noqa: BLE001
                failed = True  # HA offline or busy: retried next tick
                log.warning("Schedule: could not set %s: %s", eid, e)
        if not failed:
            self._applied = key
