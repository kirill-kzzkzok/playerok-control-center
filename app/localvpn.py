"""Private subprocess control of localVPN's independent Playerok tunnel.

Only safe status JSON crosses this pipe. No node catalogue, Telegram credentials,
Shadowsocks configuration or passwords are read by Python.
"""

import asyncio
import json
import os
import subprocess
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

from app.security.logging import event

SOCKS_URL = "socks5://127.0.0.1:1080"
DEFAULT_EXE = Path("C:/Users/user/Documents/Codex/2026-10-19/pa/outputs/playerok-proxy/localvpn-playerok.exe")
active_proxy = ContextVar("playerok_localvpn", default=None)


class ProxyUnavailable(RuntimeError):
    def __init__(self):
        super().__init__("Playerok Proxy недоступен; прямой fallback запрещён")


@dataclass(frozen=True)
class NetworkSettings:
    mode: str = "localvpn"
    executable: Path = DEFAULT_EXE

    @classmethod
    def load(cls, path=".env"):
        env = {**dotenv_values(path, interpolate=False), **os.environ}
        mode = (env.get("TELEGRAM_TRANSPORT") or "localvpn").strip().lower()
        if mode not in {"localvpn", "worker", "direct", "legacy"}:
            raise ValueError("TELEGRAM_TRANSPORT: localvpn / worker / direct / legacy")
        exe = Path(env.get("LOCALVPN_PLAYEROK_EXE") or DEFAULT_EXE)
        if not exe.is_absolute() or exe.name.lower() != "localvpn-playerok.exe":
            raise ValueError("LOCALVPN_PLAYEROK_EXE must be an absolute service executable path")
        return cls(mode, exe)


def mtproto_proxy(mode):
    if mode != "localvpn":
        return None
    return {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080, "rdns": True}


def require_proxy():
    proxy = active_proxy.get()
    if proxy is None or not proxy.ready:
        raise ProxyUnavailable()
    return proxy


class PlayerokProxy:
    def __init__(self, settings=None):
        self.settings = settings or NetworkSettings.load()
        self.process = None
        self.lock = asyncio.Lock()
        self.state = {"state": "STOPPED"}
        self.observed = 0.0
        self.monitor = None
        self.token = None

    @property
    def ready(self):
        return bool(
            self.process
            and self.process.returncode is None
            and self.state.get("state") == "READY"
            and self.state.get("telegramAvailable") is True
            and time.monotonic() - self.observed < 8
        )

    async def command(self, command, server=None):
        async with self.lock:
            if not self.process or self.process.returncode is not None:
                self.state = {"state": "DEGRADED", "error": "service_exited"}
                raise ProxyUnavailable()
            try:
                async with asyncio.timeout(95):
                    payload = {"command": command}
                    if server is not None:
                        payload["server"] = server
                    self.process.stdin.write(json.dumps(payload).encode() + b"\n")
                    await self.process.stdin.drain()
                    raw = await self.process.stdout.readline()
                    value = json.loads(raw)
                    if (
                        not isinstance(value, dict)
                        or value.get("address") != SOCKS_URL
                        or value.get("state")
                        not in {"STARTING", "READY", "DEGRADED", "RECOVERING", "STOPPED"}
                    ):
                        raise ValueError("invalid service status")
            except asyncio.CancelledError:
                self.state = {"state": "DEGRADED", "error": "control_cancelled"}
                self.process.stdin.close()
                raise
            except (TimeoutError, OSError, ValueError, asyncio.IncompleteReadError):
                self.state = {"state": "DEGRADED", "error": "control_unavailable"}
                # A late reply must never be consumed as a different command's
                # answer. Close admission and the pipe instead of retrying it.
                if self.process.stdin:
                    self.process.stdin.close()
                raise ProxyUnavailable() from None
            self.state = value
            self.observed = time.monotonic()
            return value

    async def confirm(self):
        result = await self.command("status")
        if not self.ready:
            raise ProxyUnavailable()
        return result["generation"]

    async def _watch(self):
        previous = self.state.get("state")
        while True:
            await asyncio.sleep(2)
            try:
                await self.command("status")
            except ProxyUnavailable:
                pass
            current = self.state.get("state")
            if current != previous:
                ready = current == "READY" and self.ready
                labels = {
                    "STARTING": "Telegram-прокси запускается",
                    "DEGRADED": "Telegram-прокси недоступен · новые запросы заблокированы",
                    "RECOVERING": "Telegram-прокси восстанавливает маршрут",
                    "STOPPED": "Telegram-прокси остановлен",
                }
                text = (
                    "Telegram-прокси: проверенный маршрут восстановлен"
                    if ready
                    else labels.get(current, "Telegram-прокси требует проверки")
                )
                event("INFO" if ready else "WARNING", text, level=20 if ready else 30)
                previous = current

    async def __aenter__(self):
        event("CHECK", "Playerok Proxy: запуск")
        if not self.settings.executable.is_file():
            raise ProxyUnavailable()
        self.process = await asyncio.create_subprocess_exec(
            str(self.settings.executable),
            "--service",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=8192,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            await self.command("start")
            if not self.ready:
                event("WARNING", "Playerok Proxy: " + str(self.state.get("error", "not_ready")), level=30)
                raise ProxyUnavailable()
            self.token = active_proxy.set(self)
            self.monitor = asyncio.create_task(self._watch())
            event("SUCCESS", "SOCKS5 127.0.0.1:1080 готов; HTTPS/TLS и Telegram endpoint подтверждены")
            event("CHECK", "Telegram Bot identity / polling / callback: NOT TESTED")
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        self.state = {"state": "STOPPED"}
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=12)
            except TimeoutError:
                # Terminate only this exact owned service process. Closing its
                # non-inherited Job handle terminates its own sing-box child.
                if self.process.returncode is None:
                    self.process.kill()
                await self.process.wait()
            self.process = None
        if self.token is not None:
            active_proxy.reset(self.token)
            self.token = None

    async def __aexit__(self, *args):
        await self.close()
