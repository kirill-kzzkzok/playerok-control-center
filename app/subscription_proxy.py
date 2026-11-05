"""Telegram-only VLESS/REALITY tunnel, using the existing local proxy contract."""

import asyncio
import base64
import json
import os
import socket
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx

from app.localvpn import PlayerokProxy, ProxyUnavailable, active_proxy
from app.security.logging import event


def parse_nodes(body):
    if not body.lstrip().startswith(b"vless://"):
        body = base64.b64decode(b"".join(body.split()), validate=True)
    nodes = []
    supported = {"type", "security", "encryption", "flow", "sni", "fp", "pbk", "sid"}
    for line in body.decode().splitlines():
        try:
            uri = urlsplit(line.strip())
            values = parse_qs(uri.query, keep_blank_values=True)
            if values.keys() - supported or any(len(value) != 1 for value in values.values()):
                continue
            q = {key: value[0] for key, value in values.items()}
            if (
                uri.scheme != "vless"
                or not uri.hostname
                or not uri.port
                or uri.password is not None
                or q.get("type") != "tcp"
                or q.get("security") != "reality"
                or q.get("encryption", "none") != "none"
                or q.get("flow", "") not in {"", "xtls-rprx-vision"}
            ):
                continue
            nodes.append(
                {
                    "type": "vless",
                    "server": uri.hostname,
                    "server_port": uri.port,
                    "uuid": str(UUID(uri.username)),
                    "flow": q.get("flow", ""),
                    "tls": {
                        "enabled": True,
                        "server_name": q["sni"],
                        "utls": {"enabled": True, "fingerprint": q.get("fp", "chrome")},
                        "reality": {"enabled": True, "public_key": q["pbk"], "short_id": q.get("sid", "")},
                    },
                }
            )
        except (KeyError, ValueError, AttributeError, TypeError):
            continue
        if len(nodes) == 8:
            break
    return nodes


class SubscriptionProxy(PlayerokProxy):
    def __init__(self, settings, *, recover_on_start=False):
        super().__init__(settings, recover_on_start=recover_on_start)
        self.generation = 0
        self.files = None

    @property
    def ready(self):
        return bool(self.process and self.process.returncode is None and self.state.get("state") == "READY")

    async def confirm(self):
        if not self.ready:
            raise ProxyUnavailable()
        return self.generation

    async def load_nodes(self):
        url = urlsplit(self.settings.subscription)
        if url.scheme != "https" or not url.hostname or url.username is not None:
            raise ValueError("Invalid Telegram proxy subscription URL")
        async with (
            asyncio.timeout(15),
            httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client,
            client.stream("GET", self.settings.subscription) as response,
        ):
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 64_000:
                    raise ValueError("Telegram proxy subscription too large")
        return parse_nodes(body)

    async def launch(self, node):
        with socket.socket() as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind(("127.0.0.1", 1080))
        path = Path(self.files.name) / "route.json"
        path.write_text(
            json.dumps(
                {
                    "log": {"disabled": True},
                    "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": 1080}],
                    "outbounds": [node],
                }
            ),
            encoding="utf-8",
        )
        self.process = await asyncio.create_subprocess_exec(
            str(self.settings.engine),
            "run",
            "-c",
            str(path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        async with asyncio.timeout(3):
            while self.process.returncode is None:
                try:
                    _, writer = await asyncio.open_connection("127.0.0.1", 1080)
                    writer.close()
                    await writer.wait_closed()
                    path.unlink()
                    return
                except OSError:
                    await asyncio.sleep(0.1)
        raise ProxyUnavailable()

    async def probe(self):
        try:
            async with (
                asyncio.timeout(8),
                httpx.AsyncClient(proxy="http://127.0.0.1:1080", trust_env=False, timeout=6) as client,
            ):
                result = await client.get("https://api.telegram.org/")
            return 200 <= result.status_code < 400
        except (OSError, httpx.HTTPError):
            return False

    async def stop_engine(self):
        if self.process is not None:
            if self.process.returncode is None:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
            await self.process.wait()
            self.process = None

    async def recover(self):
        self.state = {"state": "RECOVERING"}
        await self.stop_engine()
        try:
            nodes = await self.load_nodes()
        except (OSError, ValueError, httpx.HTTPError):
            nodes = []
        for node in nodes:
            try:
                await self.launch(node)
                if await self.probe() and self.process.returncode is None:
                    self.generation += 1
                    self.state = {"state": "READY", "generation": self.generation, "telegramAvailable": True}
                    event("INFO", "Telegram: прокси подписки подключён")
                    return
            except (OSError, ProxyUnavailable):
                pass
            await self.stop_engine()
        self.state = {"state": "DEGRADED"}

    async def _watch(self):
        while True:
            await asyncio.sleep(30)
            if self.ready:
                if await self.probe():
                    continue
                event("WARNING", "Telegram: восстанавливаю прокси подписки", level=30)
            await self.recover()

    async def __aenter__(self):
        if not self.settings.engine or not self.settings.engine.is_file():
            raise ProxyUnavailable()
        self.files = TemporaryDirectory(prefix="pcc-telegram-proxy-")
        try:
            if not self.recover_on_start:
                await self.recover()
                if not self.ready:
                    raise ProxyUnavailable()
            self.token = active_proxy.set(self)
            self.monitor = asyncio.create_task(self._run())
            return self
        except BaseException:
            await self.close()
            raise

    async def _run(self):
        if not self.ready:
            await self.recover()
        await self._watch()

    async def close(self):
        if self.monitor:
            self.monitor.cancel()
            await asyncio.gather(self.monitor, return_exceptions=True)
            self.monitor = None
        await self.stop_engine()
        if self.files:
            self.files.cleanup()
            self.files = None
        self.state = {"state": "STOPPED"}
        if self.token is not None:
            active_proxy.reset(self.token)
            self.token = None
