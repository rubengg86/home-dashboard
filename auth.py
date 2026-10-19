"""Token auth for the mobile app (/api/v1), on top of the dashboard's single user (DASHBOARD_USER / DASHBOARD_PASS).

- login -> short-lived access token (JWT, Bearer header) + long-lived refresh token (opaque, rotating)
- refresh tokens are single use; presenting an already-rotated one revokes that session (it leaked)
- sessions are stored hashed in DATA_DIR/.api_sessions.json, one per app install, and can be revoked
- changing DASHBOARD_PASS invalidates every session and token
- stream tokens: 15-minute, read-only, for URLs that can't carry a header (<img> MJPEG, video, WebSocket)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("tapo.auth")

ACCESS_SECONDS = 60 * 60            # 1 h
REFRESH_SECONDS = 90 * 24 * 3600    # 90 days, renewed on every refresh
STREAM_SECONDS = 15 * 60
LOGIN_MAX_FAILURES, LOGIN_WINDOW = 5, 5 * 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TokenStore:
    def __init__(self, user: str, password: str, data_dir: Path) -> None:
        self.user, self.password = user, password
        self.secret = self._load_secret(data_dir / ".api_secret")
        # fingerprint of the credentials: tokens minted under an old password stop working
        self.pwv = hmac.new(self.secret, f"{user}:{password}".encode(), "sha256").hexdigest()[:16]
        self.sessions_file = data_dir / ".api_sessions.json"
        self.sessions: dict[str, dict] = self._load_sessions()
        self._failures: dict[str, deque] = defaultdict(deque)

    # ---------------------------------------------------------- storage
    @staticmethod
    def _load_secret(path: Path) -> bytes:
        if env := os.getenv("API_SECRET"):
            return env.encode()
        try:
            return bytes.fromhex(path.read_text().strip())
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            path.write_text(key.hex())
            os.chmod(path, 0o600)
            return key

    def _load_sessions(self) -> dict:
        try:
            sessions = json.loads(self.sessions_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            log.error("Could not read %s: %s", self.sessions_file, e)
            return {}
        now = time.time()
        return {sid: s for sid, s in sessions.items() if s.get("pwv") == self.pwv and s["expires_at"] > now}

    def _save(self) -> None:
        tmp = self.sessions_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.sessions, indent=1))
            os.chmod(tmp, 0o600)
            tmp.replace(self.sessions_file)
        except OSError as e:
            log.error("Could not save app sessions to %s: %s", self.sessions_file, e)

    # ---------------------------------------------------------- credentials
    def check_password(self, username: str, password: str) -> bool:
        ok_user = secrets.compare_digest(username.encode(), self.user.encode())
        ok_pass = secrets.compare_digest(password.encode(), self.password.encode())
        return ok_user and ok_pass

    def check_rate(self, ip: str) -> None:
        q, now = self._failures[ip], time.time()
        while q and now - q[0] > LOGIN_WINDOW:
            q.popleft()
        if len(q) >= LOGIN_MAX_FAILURES:
            retry = int(LOGIN_WINDOW - (now - q[0])) + 1
            raise HTTPException(429, "Too many failed logins, try again later", headers={"Retry-After": str(retry)})

    def failed(self, ip: str) -> None:
        self._failures[ip].append(time.time())
        log.warning("Failed app login from %s", ip)

    # ---------------------------------------------------------- tokens
    def _access(self, sid: str, typ: str = "access", seconds: int = ACCESS_SECONDS) -> str:
        now = int(time.time())
        return jwt.encode({"sub": self.user, "sid": sid, "typ": typ, "pwv": self.pwv, "iat": now, "exp": now + seconds},
                          self.secret, algorithm="HS256")

    def _issue(self, sid: str) -> dict:
        refresh = secrets.token_urlsafe(32)
        s = self.sessions[sid]
        s["prev_hash"], s["refresh_hash"] = s.get("refresh_hash"), _hash(refresh)
        s["expires_at"] = time.time() + REFRESH_SECONDS
        self._save()
        return {"token_type": "Bearer", "access_token": self._access(sid), "expires_in": ACCESS_SECONDS,
                "refresh_token": refresh, "refresh_expires_in": REFRESH_SECONDS, "session_id": sid}

    def login(self, device_name: str, ip: str) -> dict:
        sid = secrets.token_urlsafe(9)
        now = time.time()
        self.sessions[sid] = {"device_name": device_name[:80] or "App", "created_at": now, "last_used_at": now,
                              "last_ip": ip, "pwv": self.pwv, "expires_at": now + REFRESH_SECONDS}
        log.info("App login: session %s (%s) from %s", sid, device_name, ip)
        return self._issue(sid)

    def refresh(self, token: str, ip: str) -> dict:
        h, now = _hash(token), time.time()
        for sid, s in list(self.sessions.items()):
            if secrets.compare_digest(s.get("refresh_hash") or "", h):
                if s["expires_at"] < now or s.get("pwv") != self.pwv:
                    self.revoke(sid)
                    break
                s["last_used_at"], s["last_ip"] = now, ip
                return self._issue(sid)
            if s.get("prev_hash") and secrets.compare_digest(s["prev_hash"], h):
                # an already-rotated token came back: someone else has a copy, kill the session
                log.warning("Refresh token reuse on session %s (%s) from %s: revoking", sid, s["device_name"], ip)
                self.revoke(sid)
                break
        raise HTTPException(401, "Invalid or expired refresh token")

    def revoke(self, sid: str) -> bool:
        found = self.sessions.pop(sid, None) is not None
        if found:
            self._save()
        return found

    def verify(self, token: str, allow: tuple[str, ...] = ("access",)) -> dict | None:
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        if claims.get("typ") not in allow or claims.get("pwv") != self.pwv or claims.get("sid") not in self.sessions:
            return None
        return claims

    def stream_token(self, sid: str) -> str:
        return self._access(sid, "stream", STREAM_SECONDS)


# -------------------------------------------------------------- endpoints
class LoginBody(BaseModel):
    username: str
    password: str
    device_name: str = Field("App", description="Shown in the session list, e.g. 'Ruben's iPhone'")


class RefreshBody(BaseModel):
    refresh_token: str


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def auth_router(store: TokenStore | None) -> APIRouter:
    r = APIRouter(prefix="/auth", tags=["auth"])

    def _store() -> TokenStore:
        if not store:
            raise HTTPException(503, "Login is not configured: set DASHBOARD_USER and DASHBOARD_PASS")
        return store

    def claims(request: Request) -> dict:
        """The verified access token of this request (the middleware already checked it)."""
        s = _store()
        token = request.headers.get("authorization", "").partition(" ")[2].strip()
        c = s.verify(token)
        if not c:
            raise HTTPException(401, "Bearer access token required", headers={"WWW-Authenticate": "Bearer"})
        return c

    @r.post("/login", summary="Log in with the dashboard user, get access + refresh tokens")
    async def login(body: LoginBody, request: Request):
        s, ip = _store(), _ip(request)
        s.check_rate(ip)
        if not s.check_password(body.username, body.password):
            s.failed(ip)
            raise HTTPException(401, "Wrong username or password")
        return s.login(body.device_name, ip)

    @r.post("/refresh", summary="Swap a refresh token for new tokens (the old refresh token stops working)")
    async def refresh(body: RefreshBody, request: Request):
        return _store().refresh(body.refresh_token, _ip(request))

    @r.post("/logout", summary="End this app session")
    async def logout(c: dict = Depends(claims)):
        _store().revoke(c["sid"])
        return {"ok": True}

    @r.get("/stream-token", summary="15-min read-only token for ?token= on camera streams, clips and the WebSocket")
    async def stream_token(c: dict = Depends(claims)):
        return {"token": _store().stream_token(c["sid"]), "expires_in": STREAM_SECONDS}

    @r.get("/me", summary="Who is logged in and on which session")
    async def me(c: dict = Depends(claims)):
        s = _store().sessions[c["sid"]]
        return {"username": c["sub"], "session_id": c["sid"], "device_name": s["device_name"],
                "access_expires_at": c["exp"]}

    @r.get("/sessions", summary="All app sessions (installs) that are logged in")
    async def sessions(c: dict = Depends(claims)):
        return [{"id": sid, "device_name": s["device_name"], "created_at": s["created_at"],
                 "last_used_at": s["last_used_at"], "last_ip": s.get("last_ip"), "current": sid == c["sid"]}
                for sid, s in _store().sessions.items()]

    @r.delete("/sessions/{sid}", summary="Log out another device")
    async def revoke(sid: str, c: dict = Depends(claims)):
        if not _store().revoke(sid):
            raise HTTPException(404, "No such session")
        return {"ok": True}

    return r
