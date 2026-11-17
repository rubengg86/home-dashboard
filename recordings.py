"""Browse and stream yi-hack camera recordings over FTP.

yi-hack stores motion clips on the SD card as hour folders with one file per clip:
    record/2026Y10M09D14H/05M00S60.mp4     -> 2026-10-09 14:05:00, 60 s long
(optionally with a .jpg thumbnail next to each .mp4).

The browser can't speak FTP, so the backend lists folders and streams files, with HTTP Range
support (mapped to FTP REST) so the <video> player can seek.
"""
from __future__ import annotations

import ftplib
import logging
import posixpath
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

log = logging.getLogger("tapo.recordings")

ROOT_CANDIDATES = ["/tmp/sd/record", "/record", "record", "/mnt/mmc/record", "/tmp/sd/yi-hack/www/record"]
DIR_RE = re.compile(r"^(\d{4})Y(\d{2})M(\d{2})D(\d{2})H$")
FILE_RE = re.compile(r"^(\d{2})M(\d{2})S(\d{2,3})?\.(mp4|jpg)$", re.I)
CHUNK = 64 * 1024
MEDIA_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".h264": "video/h264"}


class FtpRecordings:
    def __init__(self, host: str, user: str = "root", password: str = "", root: str | None = None,
                 port: int = 21, max_connections: int = 3) -> None:
        self.host, self.port, self.user, self.password = host, port, user, password
        self.root = root.rstrip("/") if root else None
        self._slots = threading.BoundedSemaphore(max_connections)  # busybox ftpd dislikes many sessions

    # ---------------------------------------------------------------- ftp plumbing (blocking)
    def _acquire(self, timeout: float = 20) -> None:
        if not self._slots.acquire(timeout=timeout):
            raise TimeoutError("camera FTP busy, try again")

    @contextmanager
    def _session(self):
        self._acquire()
        try:
            ftp = self._open()
            try:
                yield ftp
            finally:
                try:
                    ftp.close()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._slots.release()

    def _open(self) -> ftplib.FTP:
        ftp = ftplib.FTP()
        ftp.connect(self.host, self.port, timeout=10)
        ftp.login(self.user, self.password)
        ftp.voidcmd("TYPE I")
        return ftp

    def _find_root(self, ftp: ftplib.FTP) -> str:
        if self.root:
            return self.root
        for cand in ROOT_CANDIDATES:
            try:
                ftp.cwd(cand)
                self.root = ftp.pwd().rstrip("/") or "/"
                log.info("Camera recordings found at ftp://%s%s", self.host, self.root)
                return self.root
            except ftplib.error_perm:
                continue
        raise FileNotFoundError("Recordings folder not found on the camera FTP "
                                f"(tried {', '.join(ROOT_CANDIDATES)}). Set CAMERA_RECORD_DIR in .env.")

    def _abs(self, ftp: ftplib.FTP, rel: str) -> str:
        """Resolve a path relative to the recordings root, refusing anything outside it."""
        root = self._find_root(ftp)
        rel = posixpath.normpath("/" + (rel or "").lstrip("/"))
        if ".." in rel.split("/"):
            raise PermissionError("invalid path")
        return root if rel == "/" else root + rel

    @staticmethod
    def _list(ftp: ftplib.FTP, path: str) -> list[dict]:
        """Parse `ls -l` style LIST output (busybox ftpd has no MLSD)."""
        lines: list[str] = []
        ftp.retrlines(f"LIST {path}", lines.append)
        out = []
        for line in lines:
            parts = line.split(None, 8)
            if len(parts) < 9 or parts[8] in (".", ".."):
                continue
            name = parts[8].split(" -> ")[0]
            out.append({"name": name, "dir": line.startswith("d"),
                        "size": int(parts[4]) if parts[4].isdigit() else None})
        return out

    # ---------------------------------------------------------------- public API
    def list_hours(self) -> list[dict]:
        """Hour folders, newest first, with a parsed timestamp when the name follows yi-hack's scheme."""
        with self._session() as ftp:
            entries = self._list(ftp, self._abs(ftp, "/"))
        hours = []
        for e in entries:
            if not e["dir"]:
                continue
            m = DIR_RE.match(e["name"])
            start = datetime(*map(int, m.groups())) if m else None
            hours.append({"dir": e["name"], "start": start.isoformat() if start else None,
                          "day": start.date().isoformat() if start else None,
                          "label": start.strftime("%H:00") if start else e["name"]})
        hours.sort(key=lambda h: h["start"] or h["dir"], reverse=True)
        return hours

    def list_clips(self, folder: str) -> list[dict]:
        with self._session() as ftp:
            entries = self._list(ftp, self._abs(ftp, folder))
        m_dir = DIR_RE.match(folder.strip("/"))
        hour = datetime(*map(int, m_dir.groups())) if m_dir else None
        names = {e["name"] for e in entries}
        clips = []
        for e in entries:
            if e["dir"] or not e["name"].lower().endswith((".mp4", ".h264")):
                continue
            m = FILE_RE.match(e["name"])
            start = hour.replace(minute=int(m.group(1)), second=int(m.group(2))) if (m and hour) else None
            thumb = re.sub(r"\.(mp4|h264)$", ".jpg", e["name"], flags=re.I)
            clips.append({
                "name": e["name"],
                "path": f"{folder.strip('/')}/{e['name']}",
                "start": start.isoformat() if start else None,
                "label": start.strftime("%H:%M:%S") if start else e["name"],
                "duration": int(m.group(3)) if (m and m.group(3)) else None,
                "size": e["size"],
                "thumb": f"{folder.strip('/')}/{thumb}" if thumb in names else None,
            })
        clips.sort(key=lambda c: c["start"] or c["name"])
        return clips

    def size(self, path: str) -> int:
        with self._session() as ftp:
            full = self._abs(ftp, path)
            try:
                return int(ftp.size(full))
            except (ftplib.error_perm, TypeError, ValueError):  # no SIZE support: fall back to LIST
                folder, name = posixpath.split(full)
                for e in self._list(ftp, folder):
                    if e["name"] == name and e["size"] is not None:
                        return e["size"]
                raise FileNotFoundError(path)

    def stream(self, path: str, start: int = 0, length: int | None = None) -> Iterator[bytes]:
        """Yield bytes [start, start+length) of a file. Runs in Starlette's threadpool."""
        self._acquire()
        try:
            yield from self._stream(path, start, length)
        finally:
            self._slots.release()

    def _stream(self, path: str, start: int = 0, length: int | None = None) -> Iterator[bytes]:
        """The transfer itself; the caller holds a connection slot."""
        ftp = conn = None
        try:
            ftp = self._open()
            conn = ftp.transfercmd(f"RETR {self._abs(ftp, path)}", rest=start or None)
            remaining = length
            while remaining is None or remaining > 0:
                data = conn.recv(CHUNK if remaining is None else min(CHUNK, remaining))
                if not data:
                    break
                if remaining is not None:
                    remaining -= len(data)
                yield data
        finally:
            for c in (conn, ftp):
                try:
                    c and c.close()  # closing mid-transfer aborts it; fine for range requests
                except Exception:  # noqa: BLE001
                    pass

    def thumbnail(self, path: str, out: Path, width: int = 400) -> None:
        """Extract a frame of the clip into `out` (jpg). ffmpeg first reads straight over FTP
        (it seeks with REST, so only the header and one keyframe move); some camera ftpds
        won't serve ffmpeg, so on failure the clip is downloaded over our own FTP session
        and decoded locally. Raises TimeoutError when the camera is busy (transient) and
        RuntimeError when the clip genuinely can't be decoded."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise FileNotFoundError("ffmpeg is not installed on the server")
        with self._session() as ftp:
            full = self._abs(ftp, path)  # also resolves/caches the recordings root
        url = (f"ftp://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
               f"@{self.host}:{self.port}{quote(full)}")
        out.parent.mkdir(parents=True, exist_ok=True)
        # a clip grid asks for dozens at once: queue patiently instead of giving up at 20 s
        self._acquire(timeout=90)  # ffmpeg opens its own sessions; stay within the ftpd's limits
        try:
            err = self._extract(ffmpeg, url, out, width)
            if err is None:
                return
            log.info("Thumbnail %s: ffmpeg over FTP failed (%s), downloading the clip", path, err)
            clip = out.with_suffix(".clip.tmp")
            try:
                with clip.open("wb") as f:
                    for chunk in self._stream(path):
                        f.write(chunk)
                err = self._extract(ffmpeg, str(clip), out, width)
                if err is not None:
                    raise RuntimeError(err)
            finally:
                clip.unlink(missing_ok=True)
        finally:
            self._slots.release()

    @staticmethod
    def _extract(ffmpeg: str, src: str, out: Path, width: int) -> str | None:
        """One ffmpeg run, src -> out jpg. Returns None on success, the error line otherwise."""
        tmp = out.with_suffix(".tmp.jpg")
        try:
            proc = subprocess.run(
                [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-rw_timeout", "15000000",
                 "-i", src, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "6", "-y", str(tmp)],
                capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            return "ffmpeg timed out"
        if proc.returncode != 0 or not tmp.exists() or not tmp.stat().st_size:
            tmp.unlink(missing_ok=True)
            lines = proc.stderr.decode(errors="replace").strip().splitlines()
            return lines[-1] if lines else f"ffmpeg exited {proc.returncode}"
        tmp.replace(out)
        return None

    def debug(self) -> dict:
        """What the FTP server looks like, to troubleshoot an unusual layout."""
        with self._session() as ftp:
            info = {"welcome": ftp.getwelcome(), "login_dir": ftp.pwd(), "login_dir_listing": ftp.nlst()[:50]}
            try:
                info["root"] = self._find_root(ftp)
                info["root_listing"] = ftp.nlst(info["root"])[:50]
            except FileNotFoundError as e:
                info["root_error"] = str(e)
            return info
