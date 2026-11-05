from types import SimpleNamespace

import pytest

from app.playerok.adapter import APIError
from app.services.playerok_connection import PlayerokConnection
from app.telegram.home_panel import HomePanel
from tests.test_playerok_connection import rig as rig_fixture

rig = rig_fixture


@pytest.mark.parametrize("total", [1450, None])
async def test_home_uses_confirmed_total_without_sum_or_network(rig, total):
    store, api = rig
    original = api.call

    async def read(method, **kwargs):
        result = await original(method, **kwargs)
        result["profile"] = {"balance": {"value": total, "available": 1100, "frozen": 350}}
        return result

    api.call = read
    connection = PlayerokConnection(store, api)
    await connection.check(cooldown=0)
    requests = list(api.calls)
    text, _ = HomePanel(store, api).home(owner=7)
    assert ("Баланс Playerok: 1 450" in text) if total else ("Баланс Playerok: нет данных" in text)
    assert "Доступно: 1 100" in text and "Заморожено: 350" in text
    assert api.calls == requests
    saved = store.setting("playerok:balance:seller")
    assert saved["at"] and saved["value"] == total
    saved["at"] = "2020-01-01T00:00:00+00:00"
    store.set_setting("playerok:balance:seller", saved)
    api.error = APIError("HTTP_500")
    await connection.check(cooldown=0)
    text, _ = HomePanel(store, api).home(owner=7)
    assert "устарели" in text and "2020" in text
    assert store.setting("playerok:balance:seller") == saved
    foreign = SimpleNamespace(account=SimpleNamespace(id="other"))
    assert "1 450" not in HomePanel(store, foreign).home(owner=7)[0]
