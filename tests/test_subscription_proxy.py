import base64
from types import SimpleNamespace

import pytest

from app.localvpn import NetworkSettings, ProxyUnavailable, active_proxy, direct_playerok
from app.playerok.adapter import SafeAccount
from app.subscription_proxy import SubscriptionProxy, parse_nodes


def test_subscription_accepts_only_supported_vless_routes():
    valid = (
        b"vless://00000000-0000-0000-0000-000000000001@example.test:443"
        b"?security=reality&type=tcp&pbk=example&sni=example.test&flow=xtls-rprx-vision"
    )
    body = base64.b64encode(valid + b"\n" + valid + b"&type=ws\n" + valid.replace(b"reality", b"none"))
    nodes = parse_nodes(body)
    assert len(nodes) == 1 and nodes[0]["tls"]["reality"]["enabled"]


async def test_failed_nodes_never_mark_ready_and_switch_fences_old_gift_route(monkeypatch):
    proxy = SubscriptionProxy(NetworkSettings(subscription="https://example.test/private"))
    calls = []

    async def load():
        return ["first", "second"]

    async def launch(node):
        calls.append(node)
        proxy.process = SimpleNamespace(returncode=None)
        with pytest.raises(ProxyUnavailable):
            await proxy.confirm()

    async def probe():
        return calls[-1] == "second"

    async def stop():
        proxy.process = None

    for method, fake in [("load_nodes", load), ("launch", launch), ("probe", probe), ("stop_engine", stop)]:
        monkeypatch.setattr(proxy, method, fake)
    await proxy.recover()
    first = await proxy.confirm()
    await proxy.recover()
    assert await proxy.confirm() == first + 1
    assert calls == ["first", "second"] * 2


def test_playerok_http_uses_direct_route_even_if_telegram_is_down(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:12345")
    proxy = SubscriptionProxy(NetworkSettings(subscription="https://example.test/private"))
    token = active_proxy.set(proxy)
    try:
        assert not proxy.ready and direct_playerok()
        account = SafeAccount("token=test")
        try:
            assert not account.client.trust_env
        finally:
            account.close()
    finally:
        active_proxy.reset(token)
