"""Tapo local dashboard: FastAPI backend.

Run:  python app.py   (reads .env if present)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cameras import CameraManager
from devices import DeviceManager
from smartthings import SmartThings
from smartthings_auth import OAuth, PatAuth
from tuya import TuyaManager

BASE = Path(__file__).parent
# config + state (.env, devices.json, tokens, energy history); a mounted volume in Docker
DATA = Path(os.getenv("DATA_DIR") or BASE)
load_dotenv(DATA / ".env")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("tapo.app")

USER = os.getenv("TAPO_USER")
PASS = os.getenv("TAPO_PASS")
if not USER or not PASS:
    raise SystemExit("Set TAPO_USER and TAPO_PASS (in .env or the environment).")

manager = DeviceManager(
    USER,
    PASS,
    hosts=[h for h in os.getenv("TAPO_HOSTS", "").split(",") if h.strip()],
    poll_seconds=float(os.getenv("POLL_SECONDS", "5")),
    hub_poll_seconds=float(os.getenv("HUB_POLL_SECONDS", "1.5")),
    rediscover_seconds=float(os.getenv("REDISCOVER_SECONDS", "300")),
    protected=os.getenv("CONFIRM_OFF", "").split(","),
)
cameras = CameraManager(
    os.getenv("CAMERAS", ""),
    min_interval=float(os.getenv("SNAPSHOT_CACHE_SECONDS", "1")),
    ffmpeg=os.getenv("FFMPEG") or None,
    live_fps=int(os.getenv("LIVE_FPS", "10")),
    live_max_width=int(os.getenv("LIVE_MAX_WIDTH", "1280")),
    ftp={"user": os.getenv("CAMERA_FTP_USER") or None,
         "password": os.getenv("CAMERA_FTP_PASS"),
         "root": os.getenv("CAMERA_RECORD_DIR") or None},
)
smartthings = None
_st_auth = None
if os.getenv("SMARTTHINGS_CLIENT_ID") and os.getenv("SMARTTHINGS_CLIENT_SECRET"):
    _st_auth = OAuth(
        os.environ["SMARTTHINGS_CLIENT_ID"],
        os.environ["SMARTTHINGS_CLIENT_SECRET"],
        redirect_uri=os.getenv("SMARTTHINGS_REDIRECT_URI", "https://httpbin.org/get"),
        scopes=os.getenv("SMARTTHINGS_SCOPES", "r:devices:*"),
        store=DATA / ".smartthings_tokens.json",
    )
elif os.getenv("SMARTTHINGS_TOKEN"):
    _st_auth = PatAuth(os.environ["SMARTTHINGS_TOKEN"], env_file=DATA / ".env")
if _st_auth:
    smartthings = SmartThings(
        _st_auth,
        poll_seconds=float(os.getenv("SMARTTHINGS_POLL_SECONDS", "30")),
        device_ids=os.getenv("SMARTTHINGS_DEVICES", "").split(","),
    )

tuya = None
_tuya_file = DATA / os.getenv("TUYA_DEVICES_FILE", "devices.json")
if _tuya_file.exists():
    try:
        tuya = TuyaManager(
            _tuya_file,
            energy_file=DATA / ".tuya_energy.json",
            poll_seconds=float(os.getenv("TUYA_POLL_SECONDS", "3")),
            max_on_minutes=float(os.getenv("HEATER_MAX_ON_MINUTES", "0")),
            price_per_kwh=float(os.getenv("ELECTRICITY_PRICE", "0")),
            currency=os.getenv("CURRENCY", "€"),
            cloud_config=DATA / os.getenv("TUYA_CLOUD_CONFIG", "tinytuya.json"),
        )
    except Exception as e:  # noqa: BLE001
        log.error("Tuya disabled, could not load %s: %s", _tuya_file.name, e)

# ---------------------------------------------------------------- automations
# actions.json maps source alias -> event name -> action, e.g.
# {"Bedroom button": {"singleClick": {"toggle": "Bedside lamp"}, "doubleClick": {"boost": {"Agua caliente": 30}}},
#  "Agua caliente": {"hot_water_ready": {"toggle": "Kitchen bulb"}}}
ACTIONS_FILE = DATA / "actions.json"


def load_actions() -> dict:
    if not ACTIONS_FILE.exists():
        return {}
    try:
        data = json.loads(ACTIONS_FILE.read_text())
        return {k.strip().lower(): v for k, v in data.items()}
    except Exception as e:  # noqa: BLE001
        log.error("Could not read actions.json: %s", e)
        return {}


async def run_action(action) -> None:
    if action == "all_off":
        await manager.all_off()
        return
    if not isinstance(action, dict):
        log.warning("Unknown action %r", action)
        return
    for verb, target in action.items():
        # boost takes {"alias": minutes}; a bare alias / list uses 30 min
        if verb == "boost":
            items = target.items() if isinstance(target, dict) else [(t, 30) for t in
                                                                     (target if isinstance(target, list) else [target])]
            for alias, minutes in items:
                tid = tuya.find_by_alias(alias) if tuya else None
                if tid:
                    await tuya.boost(tid, float(minutes))
                else:
                    log.warning("Boost target %r not found", alias)
            continue
        targets = target if isinstance(target, list) else [target]
        for alias in targets:
            on = {"toggle": None, "on": True, "off": False}
            if verb not in on:
                log.warning("Unknown verb %r", verb)
                break
            if (did := manager.find_by_alias(alias)):
                await manager.set_on(did, on[verb])
            elif tuya and (tid := tuya.find_by_alias(alias)):
                await tuya.set_on(tid, on[verb])
            else:
                log.warning("Action target %r not found", alias)


async def on_message(msg: dict) -> None:
    if msg.get("type") != "event":
        return
    ev = msg["event"]
    rules = load_actions().get((ev["alias"] or "").strip().lower(), {})
    action = rules.get(ev["event"])
    if action:
        log.info("Running action for %s/%s: %s", ev["alias"], ev["event"], action)
        try:
            await run_action(action)
        except Exception as e:  # noqa: BLE001
            log.error("Action failed: %s", e)


# ---------------------------------------------------------------- websockets
clients: set[WebSocket] = set()


async def broadcast(msg: dict) -> None:
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(msg)
        except Exception:  # noqa: BLE001
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


@asynccontextmanager
async def lifespan(_: FastAPI):
    manager.subscribe(broadcast)
    manager.subscribe(on_message)
    asyncio.create_task(manager.start())  # don't block startup on discovery
    if smartthings:
        smartthings.on_update(broadcast)
        smartthings.on_event(publish_event)
        await smartthings.start()
    if tuya:
        tuya.on_update(broadcast)
        tuya.on_event(publish_event)
        await tuya.start()
    yield
    await manager.stop()
    await cameras.close()
    if smartthings:
        await smartthings.stop()
    if tuya:
        await tuya.stop()


async def publish_event(ev: dict) -> None:
    """Events from non-Tapo sources join the same activity feed and actions.json rules."""
    msg = {"type": "event", "event": ev}
    if not ev.get("quiet"):  # quiet events (plain on/off) only drive actions, not the feed
        manager.events.appendleft(ev)
        await broadcast(msg)
    await on_message(msg)


app = FastAPI(title="Tapo Dashboard", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


# ---------------------------------------------------------------- REST
class OnBody(BaseModel):
    on: bool | None = None  # None = toggle


class BrightnessBody(BaseModel):
    value: int


def _wrap_errors(e: Exception):
    if isinstance(e, KeyError):
        raise HTTPException(404, f"Unknown device {e}")
    if isinstance(e, ValueError):
        raise HTTPException(400, str(e))
    log.exception("Device command failed")
    raise HTTPException(502, f"Device error: {e}")


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/devices")
async def list_devices():
    return manager.snapshot()


@app.get("/api/events")
async def list_events():
    return list(manager.events)


@app.get("/api/appliances")
async def list_appliances():
    return smartthings.snapshot() if smartthings else {"error": None, "appliances": []}


class CodeBody(BaseModel):
    code: str  # the bare code or the whole URL SmartThings redirected to


def _oauth() -> OAuth:
    if not isinstance(_st_auth, OAuth):
        raise HTTPException(400, "OAuth not configured: set SMARTTHINGS_CLIENT_ID and SMARTTHINGS_CLIENT_SECRET")
    return _st_auth


@app.get("/api/smartthings/auth-url")
async def smartthings_auth_url():
    a = _oauth()
    return {"url": a.authorize_url(), "redirect_uri": a.redirect_uri}


async def _connect(code: str) -> None:
    try:
        await _oauth().exchange(code)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(400, f"SmartThings refused the code (it is single-use and expires quickly): {e}")
    smartthings.poke()


@app.post("/api/smartthings/connect")
async def smartthings_connect(body: CodeBody):
    await _connect(body.code)
    return {"ok": True, **_oauth().status()}


@app.get("/api/smartthings/callback")
async def smartthings_callback(code: str):
    """Used only if SMARTTHINGS_REDIRECT_URI points at this dashboard."""
    await _connect(code)
    return RedirectResponse("/")


class BoostBody(BaseModel):
    minutes: float  # 0 cancels the countdown


def _tuya() -> TuyaManager:
    if not tuya:
        raise HTTPException(404, "No Tuya devices configured (devices.json missing)")
    return tuya


@app.get("/api/tuya")
async def tuya_list():
    return _tuya().snapshot() if tuya else []


@app.post("/api/tuya/{dev_id}/state")
async def tuya_state(dev_id: str, body: OnBody):
    try:
        await _tuya().set_on(dev_id, body.on)
    except Exception as e:  # noqa: BLE001
        _wrap_errors(e)
    return {"ok": True}


@app.post("/api/tuya/{dev_id}/boost")
async def tuya_boost(dev_id: str, body: BoostBody):
    try:
        await _tuya().boost(dev_id, body.minutes)
    except Exception as e:  # noqa: BLE001
        _wrap_errors(e)
    return {"ok": True}


@app.get("/api/debug/buttons")
async def debug_buttons():
    return await manager.debug_buttons()


@app.post("/api/devices/{device_id}/toggle")
async def toggle(device_id: str):
    try:
        await manager.set_on(device_id, None)
    except Exception as e:  # noqa: BLE001
        _wrap_errors(e)
    return {"ok": True}


@app.post("/api/devices/{device_id}/state")
async def set_state(device_id: str, body: OnBody):
    try:
        await manager.set_on(device_id, body.on)
    except Exception as e:  # noqa: BLE001
        _wrap_errors(e)
    return {"ok": True}


@app.post("/api/devices/{device_id}/brightness")
async def brightness(device_id: str, body: BrightnessBody):
    try:
        await manager.set_brightness(device_id, body.value)
    except Exception as e:  # noqa: BLE001
        _wrap_errors(e)
    return {"ok": True}


@app.post("/api/scenes/all-off")
async def all_off():
    return {"ok": True, "switched_off": await manager.all_off()}


def _buckets(rng: str) -> list[tuple[str, str]]:
    """(key, ISO start) for each bar of a chart range, oldest first."""
    now = dt.datetime.now()
    if rng == "24h":
        hour = now.replace(minute=0, second=0, microsecond=0)
        return [((t := hour - dt.timedelta(hours=i)).strftime("%Y-%m-%dT%H"), t.isoformat()) for i in range(23, -1, -1)]
    if rng in ("7d", "30d"):
        days = 7 if rng == "7d" else 30
        return [((d := dt.date.today() - dt.timedelta(days=i)).isoformat(), d.isoformat()) for i in range(days - 1, -1, -1)]
    months = [(now.year - (now.month - 1 - i < 0), (now.month - 1 - i) % 12 + 1) for i in range(11, -1, -1)]
    return [(f"{y}-{m:02d}", f"{y}-{m:02d}-01") for y, m in months]


@app.get("/api/energy")
async def energy():
    """kWh per device for each chart range. null = no data for that bar (before a device's history starts)."""
    series = await manager.energy_history() + (tuya.energy_history() if tuya else [])
    field = {"24h": "hourly", "7d": "daily", "30d": "daily", "12m": "monthly"}
    ranges = {}
    for rng, src in field.items():
        buckets = _buckets(rng)
        values = {}
        for sr in series:
            data = sr[src]
            first = min(data) if data else None
            values[sr["id"]] = [None if first is None or key < first[:len(key)] else round(data.get(key, 0.0), 4)
                                for key, _ in buckets]
        ranges[rng] = {"buckets": [start for _, start in buckets], "values": values}
    return {
        "price": float(os.getenv("ELECTRICITY_PRICE", "0")), "currency": os.getenv("CURRENCY", "€"),
        "series": [{k: sr.get(k) for k in ("id", "name", "kind", "power_w", "since")} for sr in series],
        "ranges": ranges,
    }


@app.get("/api/cameras")
async def list_cameras():
    return {"refresh_seconds": float(os.getenv("SNAPSHOT_SECONDS", "2")),
            "live": cameras.live_available, "cameras": cameras.list()}


@app.get("/api/cameras/{cam_id}/live")
async def camera_live(cam_id: str, request: Request, q: str = "hd"):
    """MJPEG stream (multipart/x-mixed-replace): works as a plain <img src>."""
    try:
        stream = cameras.live(cam_id, q)
    except KeyError:
        raise HTTPException(404, f"Unknown camera {cam_id}")
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    sub = stream.subscribe()

    async def frames():
        try:
            while not await request.is_disconnected():
                try:
                    frame = await asyncio.wait_for(sub.get(), timeout=20)
                except asyncio.TimeoutError:
                    log.warning("Live %s: no frame in 20s (%s)", cam_id, stream.last_error)
                    break
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        finally:
            stream.unsubscribe(sub)

    return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store"})


@app.get("/api/cameras/{cam_id}/snapshot")
async def camera_snapshot(cam_id: str, res: str = "low"):
    try:
        data = await cameras.snapshot(cam_id, res)
    except KeyError:
        raise HTTPException(404, f"Unknown camera {cam_id}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Camera error: {e}")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- camera recordings (FTP)
def _recs(cam_id: str):
    try:
        return cameras.get(cam_id).recordings
    except KeyError:
        raise HTTPException(404, f"Unknown camera {cam_id}")


async def _ftp(fn, *args):
    try:
        return await asyncio.to_thread(fn, *args)
    except HTTPException:
        raise
    except PermissionError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("Camera FTP error: %s", e)
        raise HTTPException(502, f"Camera FTP: {e}")


@app.get("/api/cameras/{cam_id}/recordings")
async def recordings_list(cam_id: str, dir: str | None = None):
    """No dir: hour folders (newest first). With dir: the clips in that folder."""
    rec = _recs(cam_id)
    if dir:
        return {"dir": dir, "clips": await _ftp(rec.list_clips, dir)}
    return {"hours": await _ftp(rec.list_hours)}


@app.get("/api/cameras/{cam_id}/recordings/file")
async def recordings_file(cam_id: str, path: str, request: Request):
    """Stream a clip with HTTP Range support so the video player can seek."""
    rec = _recs(cam_id)
    total = await _ftp(rec.size, path)
    ext = os.path.splitext(path)[1].lower()
    media = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}.get(ext, "application/octet-stream")
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=3600"}
    rng = request.headers.get("range", "")
    m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip())
    if m and (m.group(1) or m.group(2)):
        if m.group(1):
            start = int(m.group(1))
            end = min(int(m.group(2)), total - 1) if m.group(2) else total - 1
        else:  # suffix range: last N bytes
            start, end = max(0, total - int(m.group(2))), total - 1
        if start >= total or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        length = end - start + 1
        headers.update({"Content-Range": f"bytes {start}-{end}/{total}", "Content-Length": str(length)})
        return StreamingResponse(rec.stream(path, start, length), status_code=206, media_type=media, headers=headers)
    headers["Content-Length"] = str(total)
    return StreamingResponse(rec.stream(path, 0, total), media_type=media, headers=headers)


@app.get("/api/debug/camera-ftp/{cam_id}")
async def camera_ftp_debug(cam_id: str):
    return await _ftp(_recs(cam_id).debug)


@app.post("/api/discover")
async def rediscover():
    await manager.discover()
    return {"ok": True, "devices": len(manager.tracked)}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_json({"type": "devices", "devices": manager.snapshot()})
    await ws.send_json({"type": "events", "events": list(manager.events)})
    if smartthings:
        await ws.send_json({"type": "appliances", **smartthings.snapshot()})
    if tuya:
        await ws.send_json({"type": "tuya", "devices": tuya.snapshot()})
    try:
        while True:
            await ws.receive_text()  # keepalive pings from the client
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
