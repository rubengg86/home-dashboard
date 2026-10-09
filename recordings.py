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
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

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
    def _acquire(self) -> None:
        if not self._slots.acquire(timeout=20):
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
            self._slots.release()

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
