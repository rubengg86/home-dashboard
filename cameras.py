"""yi-hack cameras: snapshot proxy with a short shared cache.

Configure with CAMERAS in .env, e.g.
    CAMERAS=Living room=192.168.17.250
    CAMERAS=Living room=http://user:pass@192.168.17.250;Garden=192.168.17.251
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import time
from collections import deque
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger("tapo.cameras")


class Camera:
    def __init__(self, name: str, url: str, min_interval: float) -> None:
        if "://" not in url:
            url = "http://" + url
        parts = urlsplit(url)
        self.name = name.strip()
        self.id = re.sub(r"[^a-z0-9]+", "-", self.name.lower()).strip("-") or parts.hostname
        self.host = parts.hostname or ""
        self.base = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        self.auth = (parts.username, parts.password or "") if parts.username else None
        self.min_interval = min_interval
        self.online: bool | None = None
        self.error: str | None = None
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def rtsp_url(self, quality: str, with_auth: bool = True) -> str:
        auth = ""
        if with_auth and self.auth:
            auth = f"{quote(self.auth[0], safe='')}:{quote(self.auth[1], safe='')}@"
        return f"rtsp://{auth}{self.host}/ch0_{0 if quality == 'hd' else 1}.h264"

    def info(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "host": self.host,
            "online": self.online,
            "error": self.error,
            "rtsp": {"high": self.rtsp_url("hd", False), "low": self.rtsp_url("sd", False)},
        }

    async def snapshot(self, client: httpx.AsyncClient, res: str) -> bytes:
        lock = self._locks.setdefault(res, asyncio.Lock())
        async with lock:  # one request in flight per resolution; other viewers share it
            ts, data = self._cache.get(res, (0.0, b""))
            if data and time.monotonic() - ts < self.min_interval:
                return data
            try:
                r = await client.get(
                    f"{self.base}/cgi-bin/snapshot.sh",
                    params={"res": res, "watermark": "no"},
                    auth=self.auth,
                    timeout=10,
                )
                r.raise_for_status()
                if not r.headers.get("content-type", "").startswith("image/") or not r.content:
                    raise ValueError(f"camera returned {r.headers.get('content-type') or 'no content'}")
            except Exception as e:  # noqa: BLE001
                if self.online is not False:
                    log.warning("Camera %s unavailable: %s", self.name, e)
                self.online, self.error = False, str(e) or type(e).__name__
                raise
            self.online, self.error = True, None
            self._cache[res] = (time.monotonic(), r.content)
            return r.content


class LiveStream:
    """One ffmpeg process per camera+quality, RTSP in, MJPEG frames out, shared by all viewers.

    Starts on the first subscriber and stops IDLE_STOP_SECONDS after the last one leaves.
    """

    IDLE_STOP_SECONDS = 10
    STALL_SECONDS = 15

    def __init__(self, cam: Camera, quality: str, ffmpeg: str, fps: int, max_width: int) -> None:
        self.cam, self.quality, self.ffmpeg = cam, quality, ffmpeg
        self.fps, self.max_width = fps, max_width
        self.subs: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self._stop_handle: asyncio.TimerHandle | None = None
        self.last_error: str | None = None

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.subs.add(q)
        if self._stop_handle:
            self._stop_handle.cancel()
            self._stop_handle = None
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run())
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)
        if not self.subs and self.task and not self.task.done():
            self._stop_handle = asyncio.get_running_loop().call_later(self.IDLE_STOP_SECONDS, self._stop_if_idle)

    def _stop_if_idle(self) -> None:
        self._stop_handle = None
        if not self.subs and self.task:
            log.info("Live %s/%s: no viewers, stopping ffmpeg", self.cam.name, self.quality)
            self.task.cancel()

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    def _publish(self, frame: bytes) -> None:
        for q in list(self.subs):
            if q.full():
                q.get_nowait()  # drop the stale frame; viewers always get the newest
            q.put_nowait(frame)

    async def _run(self) -> None:
        backoff = 1.0
        while self.subs:
            vf = f"fps={self.fps},scale=w='min({self.max_width},iw)':h=-2"
            args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
                    "-i", self.cam.rtsp_url(self.quality), "-an", "-vf", vf,
                    "-q:v", "5", "-f", "mjpeg", "pipe:1"]
            log.info("Live %s/%s: starting ffmpeg", self.cam.name, self.quality)
            proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            errors: deque[str] = deque(maxlen=5)

            async def drain_stderr() -> None:
                async for line in proc.stderr:
                    errors.append(line.decode(errors="replace").strip())

            drain = asyncio.create_task(drain_stderr())
            buf = b""
            try:
                while True:
                    chunk = await asyncio.wait_for(proc.stdout.read(65536), timeout=self.STALL_SECONDS)
                    if not chunk:
                        break
                    buf += chunk
                    while True:  # split the MJPEG byte stream into JPEG frames (SOI ... EOI)
                        start = buf.find(b"\xff\xd8")
                        end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                        if end < 0:
                            break
                        self._publish(buf[start:end + 2])
                        buf = buf[end + 2:]
                        backoff = 1.0
                    if len(buf) > 8_000_000:
                        buf = b""
            except asyncio.TimeoutError:
                errors.append("no frames from camera (stalled)")
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
                drain.cancel()
            self.last_error = "; ".join(errors) or f"ffmpeg exited ({proc.returncode})"
            log.warning("Live %s/%s stopped: %s", self.cam.name, self.quality, self.last_error)
            if self.subs:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


class CameraManager:
    def __init__(self, spec: str, min_interval: float = 1.0, ffmpeg: str | None = None,
                 live_fps: int = 10, live_max_width: int = 1280) -> None:
        self.cameras: dict[str, Camera] = {}
        self.ffmpeg = ffmpeg or shutil.which("ffmpeg")
        self.live_fps, self.live_max_width = live_fps, live_max_width
        self.streams: dict[tuple[str, str], LiveStream] = {}
        if not self.ffmpeg:
            log.warning("ffmpeg not found: live video disabled (brew install ffmpeg)")
        for entry in filter(None, (e.strip() for e in re.split(r"[;\n]", spec or ""))):
            name, _, url = entry.partition("=")
            if not url:
                url = name  # bare IP: use it as the name too
            cam = Camera(name, url.strip(), min_interval)
            self.cameras[cam.id] = cam
            log.info("Camera %s at %s", cam.name, cam.base)
        self.client = httpx.AsyncClient()

    def list(self) -> list[dict]:
        return [c.info() for c in self.cameras.values()]

    async def snapshot(self, cam_id: str, res: str = "low") -> bytes:
        if cam_id not in self.cameras:
            raise KeyError(cam_id)
        return await self.cameras[cam_id].snapshot(self.client, "high" if res == "high" else "low")

    @property
    def live_available(self) -> bool:
        return bool(self.ffmpeg)

    def live(self, cam_id: str, quality: str = "hd") -> LiveStream:
        if cam_id not in self.cameras:
            raise KeyError(cam_id)
        if not self.ffmpeg:
            raise RuntimeError("ffmpeg is not installed on the server (brew install ffmpeg)")
        quality = "sd" if quality == "sd" else "hd"
        key = (cam_id, quality)
        if key not in self.streams:
            self.streams[key] = LiveStream(self.cameras[cam_id], quality, self.ffmpeg,
                                           self.live_fps, self.live_max_width)
        return self.streams[key]

    async def close(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.streams.values()), return_exceptions=True)
        await self.client.aclose()
