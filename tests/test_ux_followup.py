"""Seller journeys through the production Dispatcher; no live transports."""

import pytest

from tests.test_ux_redesign import buttons
from tests.test_ux_redesign import ux as ux_fixture


@pytest.fixture(name="ux")
async def _ux(tmp_path):
    async for value in ux_fixture.__wrapped__(tmp_path):
        yield value


async def test_edit_timeout_does_not_commit_invisible_route(ux):
    from aiogram.exceptions import TelegramNetworkError

    from tests.test_telegram_transport import production_dispatcher

    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    original = ux.session.make_request

    async def fail(bot, method, timeout=None):
        if method.__api_method__ == "editMessageText":
            raise TelegramNetworkError(method=method, message="timeout")
        return await original(bot, method, timeout)

    ux.session.make_request = fail
    await ux.ui.click("items")
    panel = ux.store.setting("navigation:123:7")
    assert panel["route"] == "home"
    assert panel["history"] == []
    ux.session.make_request = original
    await ux.ui.click("stats:30")
    back = next(k for k in buttons(ux.session.last_panel) if k.startswith("nav:back:"))
    await ux.ui.click(back)
    assert ux.store.setting("navigation:123:7")["route"] == "home"
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_start_exposes_explicit_menu_recovery_after_unknown_send(ux):
    from aiogram.exceptions import TelegramNetworkError

    from tests.test_telegram_transport import production_dispatcher

    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    await ux.ui.click("st:7:time")
    original = ux.session.make_request
    failed = False

    async def fail(bot, method, timeout=None):
        nonlocal failed
        if method.__api_method__ == "sendMessage" and not failed:
            failed = True
            raise TelegramNetworkError(method=method, message="timeout")
        return await original(bot, method, timeout)

    ux.session.make_request = fail
    await ux.ui.click("stats:7")
    assert ux.store.setting("navigation:123:7")["sending"]
    await ux.ui.message("/start")
    action = next(k for k in buttons(ux.session.last_panel) if k.startswith("nav:recover:"))
    await ux.ui.click(action)
    assert not ux.store.setting("navigation:123:7").get("sending")
    assert set(buttons(ux.session.last_panel)) == {
        "items",
        "orders",
        "chats",
        "promotions",
        "stats:7",
        "settings",
    }
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_start_recovers_visible_menu_even_when_home_text_has_not_changed(ux):
    before = ux.store.setting("navigation:123:7")["id"]
    sends = sum(call.__api_method__ == "sendMessage" for call in ux.session.calls)
    await ux.ui.message("/start")
    assert ux.store.setting("navigation:123:7")["id"] != before
    assert sum(call.__api_method__ == "sendMessage" for call in ux.session.calls) == sends + 1
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_repeated_failed_placement_read_keeps_one_error_banner(ux, monkeypatch):
    from app.playerok.adapter import APIError
    from app.telegram.launch_panel import LaunchPanel

    async def failed_read(*args):
        error = APIError("CONNECTION_NOT_VERIFIED")
        error.read_only = True
        raise error

    monkeypatch.setattr(LaunchPanel, "callback", failed_read)
    text = "Запуск продаж"
    markup = ux.session.last_panel.reply_markup
    for _ in range(3):
        await ux.ui.click("launch:item:lot", reply_markup=markup, text=text)
        text = ux.session.last_panel.text
    assert text.count("Бот перепроверяет подключение Playerok") == 1
    assert "Запуск продаж" in text
    assert buttons(ux.session.last_panel) == {
        button.callback_data: button.text for row in markup.inline_keyboard for button in row
    }
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_rank_proposes_related_offers_without_entering_numbers(ux):
    from app.database.store import now
    from app.services.orders import Orders

    ux.store.set_setting("playerok:account_id", "seller")
    for i in range(3):
        Orders(ux.store).ingest(
            {
                "id": str(i),
                "status": "CONFIRMED",
                "direction": "OUT",
                "created_at": now(),
                "item": {
                    "id": "offer" + str(i),
                    "name": "Call of Duty",
                    "price": 90,
                    "user": {"id": "seller"},
                    "category": {"id": "games", "name": "Games"},
                },
            }
        )
    await ux.ui.click("sd:7:bestsellers")
    suggestion = next(k for k in buttons(ux.session.last_panel) if k.startswith("sg:7:suggest:"))
    await ux.ui.click(suggestion)
    confirmation = next(k for k in buttons(ux.session.last_panel) if k.startswith("sg:7:commit:"))
    await ux.ui.click(confirmation)
    await ux.ui.click("sd:7:bestsellers")
    assert ux.session.last_panel.text.count("Call of Duty") == 1
    assert "Заказов: 3" in ux.session.last_panel.text
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_item_schedule_starts_with_single_participation_choice(ux):
    from app.services.account_binding import bind_account

    bind_account(ux.store, ux.api.account.id)
    ux.api.value["raw_price"] = 100
    original = ux.api.call

    async def call(method, **kwargs):
        if method == "get_item_priority_statuses":
            return [
                {"id": "premium", "type": "PREMIUM", "price": 19, "period": 7},
                {"id": "free", "type": "DEFAULT", "price": 0},
            ]
        return await original(method, **kwargs)

    ux.api.call = call
    await ux.ui.click("promo:item:lot")
    labels = buttons(ux.session.last_panel)
    assert not any(":quote:" in key for key in labels)
    assert not any(key.endswith(":target:default") for key in labels)
    assert sum(":target:" in key for key in labels) == 1
    assert any(":more:" in key for key in labels)
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_second_timeout_during_explicit_recovery_never_commits_home(ux):
    from aiogram.exceptions import TelegramNetworkError

    from tests.test_telegram_transport import production_dispatcher

    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    await ux.ui.click("st:7:time")
    original = ux.session.make_request

    async def fail(bot, method, timeout=None):
        if method.__api_method__ == "sendMessage":
            raise TelegramNetworkError(method=method, message="timeout")
        return await original(bot, method, timeout)

    ux.session.make_request = fail
    await ux.ui.click("stats:7")
    await ux.ui.message("/start")
    action = next(k for k in buttons(ux.session.last_panel) if k.startswith("nav:recover:"))
    await ux.ui.click(action)
    panel = ux.store.setting("navigation:123:7")
    assert panel["sending"]
    assert panel.get("route") != "home"
    assert panel["pending_view"]["route"] == "home"
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_visible_routes_execute_real_dispatcher_and_context_back(ux, tmp_path):
    import json
    import os
    from pathlib import Path

    from app.database.store import now
    from app.services.account_binding import bind_account
    from app.services.orders import Orders
    from tests.test_withdrawals import snapshot

    bind_account(ux.store, ux.api.account.id)
    ux.api.value["raw_price"] = 100
    original = ux.api.call

    async def call(method, **kwargs):
        if method == "get":
            return {"id": "seller", "username": "FixtureOwner", "profile": {}, "is_blocked": False}
        if method == "get_transaction_providers":
            return [snapshot(minimum=1)["provider"]]
        if method == "get_item_priority_statuses":
            return [{"id": "premium", "type": "PREMIUM", "price": 19, "period": 7}]
        return await original(method, **kwargs)

    ux.api.call = call
    Orders(ux.store).ingest(
        {
            "id": "paid",
            "direction": "OUT",
            "status": "PAID",
            "created_at": now(),
            "item": {"id": "lot", "name": "Product", "price": 100, "user": {"id": "seller"}},
        }
    )
    paths = [
        (["items"], "Мои товары"),
        (["items", "item:lot"], "Опубликован"),
        (["items", "item:lot", "iv:edit:lot"], "Что изменить"),
        (["items", "item:lot", "iv:delivery:lot"], "выдач"),
        (["items", "item:lot", "iv:edit:lot", "iv:other:lot"], "Другие действия"),
        (["items", "item:lot", "finance:lot"], "Прогноз будущей продажи"),
        (["items", "item:lot", "finance:lot", "fin:options:lot"], "Параметры прогноза"),
        (["chats", "chat:chat"], "Переписка"),
        (["orders", "order:paid"], "Оплачен"),
        (["settings", "sp:connections", "gft:settings"], "Настройки подарков"),
        (["settings", "gft:limits:settings"], "лимит"),
        (["settings", "sp:connections", "profile"], "Профиль"),
        (["settings", "providers"], "Способы вывода"),
        (["settings"], "Настройки"),
        (["stats:7"], "Статистика"),
        (["stats:7", "sr:7:charts"], "Графики"),
        (["stats:7", "sr:7:top"], "Лучшие товары"),
        (["stats:7", "sr:7:finance"], "Финансы"),
        (["settings", "tools", "tools:operations"], "История и результаты"),
        (["settings", "tools", "tools:delivery"], "История выдачи"),
        (["settings", "tools", "tools:data"], "Диагностика и данные"),
        (["promotions"], "Автопродвижение"),
        (["promotions", "promo:catalog"], "продвижения"),
        (["promotions", "promo:catalog", "payp:item:lot"], "Продвижение товара"),
        (["promotions", "promo:catalog", "payp:item:lot", "payp:auto:lot"], "Автоподнятие"),
    ]
    executed, seen, reports = set(), set(), []
    for path, expected in paths:
        await ux.ui.message("/start")
        for action in path:
            visible = buttons(ux.session.last_panel)
            seen.update(visible)
            assert action in visible, (path, action, visible)
            await ux.ui.click(action)
            assert "❗" not in (getattr(ux.session.last_panel, "text", "") or ""), path
            executed.add(action)
        method = ux.session.last_panel
        assert expected.casefold() in method.text.casefold(), (path, method.text)
        seen.update(buttons(method))
        panel = ux.store.setting("navigation:123:7")
        back = next((key for key in buttons(method) if key.startswith("nav:back:")), None)
        parent = panel["history"][-1]["route"] if back else None
        if back:
            await ux.ui.click(back)
            executed.add(back)
            assert ux.store.setting("navigation:123:7")["route"] == parent
        reports.append({"path": path, "asserted_screen": expected, "confirmed_back": parent})
    assert not ux.store.rows("SELECT * FROM outbox")
    assert all(method.startswith("get") for method, _ in ux.api.calls)
    report = {
        "mode": "REAL_DISPATCHER_MOCK_TRANSPORT",
        "journeys": reports,
        "executed_callbacks": sorted(executed),
        "visible_but_not_executed": sorted(seen - executed),
        "outbox_operations": 0,
        "scope": "Unexecuted callbacks are not certified by this test; money/FSM-specific branches have separate tests.",
    }
    path = Path(os.environ.get("PCC_JOURNEY_OUTPUT", tmp_path / "journeys.json"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


async def test_lost_edit_is_independently_observed_after_restart(ux):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.types import InlineKeyboardMarkup

    from tests.test_telegram_transport import production_dispatcher

    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    await ux.ui.click("stats:7")
    original = ux.session.make_request

    async def fail(bot, method, timeout=None):
        if method.__api_method__ == "editMessageText":
            raise TelegramNetworkError(method=method, message="timeout")
        return await original(bot, method, timeout)

    ux.session.make_request = fail
    await ux.ui.click("sr:7:top")
    panel = ux.store.setting("navigation:123:7")
    assert panel["route"] == "stats:7" and panel["editing"]
    markup = InlineKeyboardMarkup.model_validate(panel["pending_markup"])
    action = next(
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if (b.callback_data or "").startswith("nav:back:")
    )
    ux.session.make_request = original
    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    await ux.ui.click(action, reply_markup=markup)
    assert ux.store.setting("navigation:123:7")["route"] == "stats:7"
    assert not ux.store.setting("navigation:123:7")["editing"]
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_restart_after_confirmed_send_before_old_delete_preserves_view(ux):
    import asyncio

    from tests.test_telegram_transport import production_dispatcher

    await ux.ui.click("st:7:time")
    original = ux.session.make_request

    async def cancel(bot, method, timeout=None):
        if method.__api_method__ == "deleteMessage":
            raise asyncio.CancelledError
        return await original(bot, method, timeout)

    ux.session.make_request = cancel
    with pytest.raises(asyncio.CancelledError):
        await ux.ui.click("stats:7")
    panel = ux.store.setting("navigation:123:7")
    assert panel["route"] == "stats:7" and panel["kind"] == "text"
    assert not panel["sending"] and not panel["editing"]
    sends = sum(m.__api_method__ == "sendMessage" for m in ux.session.calls)
    action = next(k for k in buttons(ux.session.last_panel) if k.startswith("nav:back:"))
    ux.session.make_request = original
    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    await ux.ui.click(action)
    assert ux.store.setting("navigation:123:7")["route"] == "st:7:time"
    assert sum(m.__api_method__ == "sendMessage" for m in ux.session.calls) == sends
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_promotion_picker_survives_category_input_and_returns_to_promotion(ux):
    original = ux.api.call

    async def call(method, **kwargs):
        if method == "get_games":
            return {"games": [{"id": "game", "name": "Game"}], "page_info": {"has_next_page": False}}
        if method == "get_game":
            return {"id": "game", "categories": [{"id": "category", "name": "Category"}]}
        return await original(method, **kwargs)

    ux.api.call = call
    await ux.ui.click("promo:catalog")
    await ux.ui.click("itemfilters")
    await ux.ui.click("il:pickcategory")
    assert (await ux.state.get_data()).get("promotion_picker") is True
    await ux.ui.message("Game")
    pick = next(k for k in buttons(ux.session.last_panel) if k.startswith("icp:") and ":pick:" in k)
    await ux.ui.click(pick)
    pick = next(k for k in buttons(ux.session.last_panel) if k.startswith("icp:") and ":pick:" in k)
    await ux.ui.click(pick)
    assert (await ux.state.get_data()).get("promotion_picker") is True
    assert "payp:item:lot" in buttons(ux.session.last_panel)
    assert "item:lot" not in buttons(ux.session.last_panel)
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_advanced_promotion_keeps_real_quote_choices_and_inheritance(ux):
    from app.services.account_binding import bind_account

    bind_account(ux.store, ux.api.account.id)
    ux.api.value["raw_price"] = 100
    original = ux.api.call

    async def call(method, **kwargs):
        if method == "get_item_priority_statuses":
            return [{"id": "premium", "type": "PREMIUM", "price": 19, "period": 7}]
        return await original(method, **kwargs)

    ux.api.call = call
    await ux.ui.click("promo:item:lot")
    more = next(k for k in buttons(ux.session.last_panel) if ":more:" in k)
    await ux.ui.click(more)
    labels = buttons(ux.session.last_panel)
    assert any(":quote:" in key and "19" in value for key, value in labels.items())
    inherit = next(k for k in labels if k.endswith(":target:default"))
    await ux.ui.click(inherit)
    assert "Автоподнятие товара" in ux.session.last_panel.text
    assert not ux.store.rows("SELECT * FROM promotion_targets")
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_start_recovers_without_any_known_panel_and_old_button_is_inert(ux):
    from aiogram.exceptions import TelegramNetworkError

    from tests.test_telegram_transport import production_dispatcher

    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    await ux.ui.click("profile")
    close = next(k for k in buttons(ux.session.last_panel) if k.startswith("nav:close:"))
    await ux.ui.click(close)
    assert ux.store.setting("navigation:123:7")["id"] is None
    original = ux.session.make_request
    failed = False

    async def fail_once(bot, method, timeout=None):
        nonlocal failed
        if method.__api_method__ == "sendMessage" and not failed:
            failed = True
            raise TelegramNetworkError(method=method, message="timeout")
        return await original(bot, method, timeout)

    ux.session.make_request = fail_once
    await ux.ui.message("/start")
    assert ux.store.setting("navigation:123:7")["sending"]
    await ux.ui.message("/start")
    action = next(k for k in buttons(ux.session.last_panel) if k.startswith("nav:recover:"))
    ident = ux.store.setting("navigation:123:7")["recovery_id"]
    await ux.ui.click(action, message_id=ident)
    assert set(buttons(ux.session.last_panel)) == {
        "items",
        "orders",
        "chats",
        "promotions",
        "stats:7",
        "settings",
    }
    await ux.ui.click("finance:lot")
    await ux.ui.click("fin:cost:lot")
    assert await ux.state.get_state() == "finance:cost"
    await ux.ui.click(action, message_id=ident)
    assert await ux.state.get_state() == "finance:cost"
    assert not ux.store.rows("SELECT * FROM outbox")


async def test_start_recovers_after_media_edit_applied_but_ack_lost(ux):
    from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

    from tests.test_telegram_transport import production_dispatcher

    ux.ui.dp = production_dispatcher(ux.store, ux.api)
    original = ux.session.make_request
    is_photo = False

    async def strict(bot, method, timeout=None):
        nonlocal is_photo
        if method.__api_method__ == "editMessageText" and is_photo:
            raise TelegramBadRequest(
                method=method, message="Bad Request: there is no text in the message to edit"
            )
        result = await original(bot, method, timeout)
        if method.__api_method__ == "editMessageMedia":
            is_photo = True
            raise TelegramNetworkError(method=method, message="ACK lost after applying media")
        return result

    ux.session.make_request = strict
    await ux.ui.click("st:7:time")
    assert ux.store.setting("navigation:123:7")["editing"]
    await ux.ui.message("/start")
    action = next(k for k in buttons(ux.session.calls[-1]) if k.startswith("nav:recover:"))
    await ux.ui.click(action)
    assert set(buttons(ux.session.last_panel)) == {
        "items",
        "orders",
        "chats",
        "promotions",
        "stats:7",
        "settings",
    }
    assert not ux.store.rows("SELECT * FROM outbox")
