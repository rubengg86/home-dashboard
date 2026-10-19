"""SmartThings authentication: OAuth with automatic refresh, or a plain personal access token.

OAuth (recommended): access tokens last 24 h, refresh tokens 29 days if unused. Every refresh
returns a new refresh token, so refreshing well before expiry keeps the login alive as long as
the dashboard runs. Tokens are stored in .smartthings_tokens.json (mode 600, git-ignored).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from dotenv import dotenv_values

log = logging.getLogger("tapo.smartthings.auth")

AUTHORIZE_URL = "https://api.smartthings.com/oauth/authorize"
TOKEN_URL = "https://api.smartthings.com/oauth/token"

REFRESH_BEFORE_EXPIRY = 60 * 60       # refresh when less than 1 h of access-token life is left
CHECK_EVERY = 10 * 60                 # background check interval


class NeedsLogin(Exception):
    """No usable credentials. The user must connect SmartThings in the dashboard."""


class PatAuth:
    mode = "pat"

    def __init__(self, token: str, env_file: Path) -> None:
        self.token, self.env_file = token.strip(), env_file

    def status(self) -> dict:
        return {"mode": self.mode, "connected": bool(self.token), "auto_renew": False}

    async def get_token(self) -> str:
        if not self.token:
            raise NeedsLogin("No SMARTTHINGS_TOKEN set")
        return self.token

    async def on_unauthorized(self) -> bool:
        new = (dotenv_values(self.env_file).get("SMARTTHINGS_TOKEN") or "").strip()
        if new and new != self.token:
            log.info("Picked up a new SMARTTHINGS_TOKEN from .env")
            self.token = new
            return True
        return False

    async def start(self) -> None: ...
    async def stop(self) -> None: ...


class OAuth:
    mode = "oauth"

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str,
                 scopes: str, store: Path) -> None:
        self.client_id, self.client_secret = client_id.strip(), client_secret.strip()
        self.redirect_uri, self.scopes, self.store = redirect_uri.strip(), scopes, store
        self.tokens: dict = self._load()
        self.last_error: str | None = None
        self._lock = asyncio.Lock()
        self._http = httpx.AsyncClient(timeout=20)
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------- storage
    def _load(self) -> dict:
        try:
            return json.loads(self.store.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:  # noqa: BLE001
            log.error("Could not read %s: %s (check the file's owner/permissions)", self.store, e)
            return {}

    def _save(self, resp: dict) -> None:
        """Keeps the new tokens in memory and on disk; sets last_error (else clears it) if the disk write fails."""
        now = time.time()
        self.tokens = {
            "access_token": resp["access_token"],
            # SmartThings rotates refresh tokens; keep the old one only if none came back
            "refresh_token": resp.get("refresh_token") or self.tokens.get("refresh_token"),
            "expires_at": now + int(resp.get("expires_in", 86400)),
            "refreshed_at": now,
            "scope": resp.get("scope"),
        }
        tmp = self.store.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.tokens, indent=2))
            os.chmod(tmp, 0o600)
            tmp.replace(self.store)  # atomic: a crash never leaves a half-written token file
            self.last_error = None
        except OSError as e:
            # keeps working until the next restart, then asks to log in again: make that visible
            self.last_error = f"Login works but can't be saved ({e}); it will be lost on restart"
            log.error("Could not save SmartThings tokens to %s: %s", self.store, e)

    # ---------------------------------------------------------- public
    def status(self) -> dict:
        exp = self.tokens.get("expires_at")
        return {
            "mode": self.mode,
            "connected": bool(self.tokens.get("refresh_token")),
            "auto_renew": True,
            "access_expires_in_min": round((exp - time.time()) / 60) if exp else None,
            "last_refresh": self.tokens.get("refreshed_at"),
            "error": self.last_error,
        }

    def authorize_url(self) -> str:
        return AUTHORIZE_URL + "?" + urlencode({
            "client_id": self.client_id, "response_type": "code",
            "redirect_uri": self.redirect_uri, "scope": self.scopes,
        })

    async def exchange(self, code_or_url: str) -> None:
        """Accepts the bare code or the full URL SmartThings redirected to."""
        code = code_or_url.strip()
        if "code=" in code:
            q = parse_qs(urlsplit(code).query) or parse_qs(code)
            code = (q.get("code") or [""])[0]
        if not code:
            raise ValueError("No authorization code found")
        async with self._lock:
            resp = await self._token_request({
                "grant_type": "authorization_code", "code": code,
                "redirect_uri": self.redirect_uri, "client_id": self.client_id,
            })
            self._save(resp)
        log.info("SmartThings connected via OAuth (access token valid %d min)",
                 self.status()["access_expires_in_min"])

    async def get_token(self) -> str:
        if not self.tokens.get("refresh_token"):
            raise NeedsLogin("SmartThings not connected yet: use 'Connect SmartThings' on the dashboard")
        if self.tokens.get("expires_at", 0) - time.time() < REFRESH_BEFORE_EXPIRY:
            await self.refresh()
        return self.tokens["access_token"]

    async def on_unauthorized(self) -> bool:
        try:
            await self.refresh(force=True)
            return True
        except NeedsLogin:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("Refresh after 401 failed: %s", e)
            return False

    async def refresh(self, force: bool = False) -> None:
        async with self._lock:  # one refresh at a time; others reuse the result
            if not force and self.tokens.get("expires_at", 0) - time.time() >= REFRESH_BEFORE_EXPIRY:
                return
            rt = self.tokens.get("refresh_token")
            if not rt:
                raise NeedsLogin("SmartThings not connected")
            try:
                resp = await self._token_request({
                    "grant_type": "refresh_token", "refresh_token": rt, "client_id": self.client_id})
            except PermissionError as e:
                # refresh token revoked / expired (unused > 29 days): user has to log in again
                self.tokens.pop("refresh_token", None)
                self.last_error = str(e)
                raise NeedsLogin(f"SmartThings login expired, connect again ({e})") from e
            self._save(resp)
            log.info("SmartThings access token refreshed")

    async def _token_request(self, data: dict) -> dict:
        r = await self._http.post(TOKEN_URL, data=data, auth=(self.client_id, self.client_secret),
                                  headers={"Accept": "application/json"})
        if r.status_code in (400, 401):
            raise PermissionError(f"{r.status_code} {r.text[:200]}")
        r.raise_for_status()
        return r.json()

    # ---------------------------------------------------------- background
    async def start(self) -> None:
        self._task = asyncio.create_task(self._keepalive())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._http.aclose()

    async def _keepalive(self) -> None:
        """Refresh ahead of expiry even when nothing is polling, so the refresh token never ages out."""
        while True:
            try:
                if self.tokens.get("refresh_token"):
                    await self.refresh()
            except asyncio.CancelledError:
                raise
            except NeedsLogin as e:
                log.warning("%s", e)
            except Exception as e:  # noqa: BLE001  (network blip: try again next round)
                self.last_error = str(e) or type(e).__name__
                log.warning("SmartThings refresh failed, will retry: %s", self.last_error)
            await asyncio.sleep(CHECK_EVERY)
