from types import SimpleNamespace

from app.playerok.adapter import SafeAccount
from app.workers.realtime import Realtime


def test_playerok_http_does_not_adopt_ambient_windows_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    account = SafeAccount("token=isolated", writes_enabled=False)
    try:
        assert account.client._trust_env is False
    finally:
        account.close()


async def test_playerok_ws_does_not_adopt_ambient_windows_proxy(monkeypatch):
    arguments = []

    class WS:
        protocol = "graphql-transport-ws"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    class Session:
        def __init__(self, **kwargs):
            arguments.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def ws_connect(self, *args, **kwargs):
            return WS()

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setattr("app.workers.realtime.aiohttp.ClientSession", Session)
    realtime = Realtime(None, SimpleNamespace(cookies={"token": "isolated"}, user_agent="test"), None)
    async with realtime.connect():
        pass
    assert arguments[0]["trust_env"] is False
