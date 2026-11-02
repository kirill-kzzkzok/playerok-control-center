# ruff: noqa: F811
"""Final seller paths: real Dispatcher and isolated mocked external transports."""

from tests.test_ux_redesign import buttons, ux  # noqa: F401


async def test_real_chat_schema_names_peer_without_ids_or_extra_requests(ux):
    r = ux
    original = r.api.call

    async def call(method, **kwargs):
        if method == "get_chats":
            return {
                "chats": [
                    {
                        "id": "private-chat-uuid",
                        "users": [
                            {"id": "seller", "username": "Owner"},
                            {"id": "buyer", "username": "<myxa1345>"},
                        ],
                        "last_message": {"text": "Товар в наличии?", "created_at": "2026-11-01T00:15:00Z"},
                        "unread_messages_counter": 2,
                    },
                    {"id": "unknown-chat-uuid", "users": []},
                ],
                "page_info": {"has_next_page": False},
            }
        return await original(method, **kwargs)

    r.api.call = call
    await r.ui.click("chats")
    labels = buttons(r.session.calls[-1])
    assert "myxa1345" in labels["chat:private-chat-uuid"]
    assert "Товар в наличии?" in labels["chat:private-chat-uuid"]
    assert "Собеседник не указан" in labels["chat:unknown-chat-uuid"]
    assert "uuid" not in " ".join(labels.values())
    await r.ui.click("chat:private-chat-uuid")
    assert "&lt;myxa1345&gt;" in r.session.calls[-1].text
    assert "Переписка с покупателем" in r.session.calls[-1].text
    assert "03:00" in r.session.calls[-1].text  # fixture message UTC -> Moscow
    assert not r.store.rows("SELECT * FROM outbox")


async def test_order_history_russian_timeline_and_readable_summary(ux):
    r = ux
    await r.ui.click("order:paid")
    summary = r.session.calls[-1]
    assert "od:history:paid:0" in buttons(summary)
    assert "PAID" not in summary.text and "политик" not in summary.text.lower()
    await r.ui.click("od:note:paid")
    await r.ui.message("Проверить доставку")
    confirm = next(k for k in buttons(r.session.calls[-1]) if k.startswith("od:apply:"))
    await r.ui.click(confirm)
    await r.ui.click("od:history:paid:0")
    shown = r.session.calls[-1]
    assert "LOCAL_NOTE" not in shown.text
    assert "PAID" not in shown.text
    assert "Заметка продавца" in shown.text
    await r.ui.click("order:paid")
    assert "ID:" not in r.session.calls[-1].text
    await r.ui.click("od:details:paid")
    assert "ID:" in r.session.calls[-1].text
    assert not r.store.rows("SELECT * FROM outbox")


async def test_statistics_groups_all_reports_without_overloaded_menu(ux):
    r = ux
    await r.ui.click("stats:7")
    shown = r.session.calls[-1]
    assert "Средний чек" in shown.text and "Прибыль" in shown.text
    labels = buttons(shown)
    assert {"📈 Графики", "🏆 Лучшие товары", "💰 Финансы", "📋 Подробный отчёт"} <= set(labels.values())
    for action in ("sr:7:charts", "sr:7:top", "sr:7:finance"):
        await r.ui.click("stats:7")
        await r.ui.click(action)
        labels = buttons(r.session.calls[-1])
        back = (
            "stats:7" if "stats:7" in labels else next(key for key in labels if key.startswith("nav:back:"))
        )
        assert len(buttons(r.session.calls[-1])) < 9
        await r.ui.click(back)
        assert "7 дн." in r.session.last_panel.text
    assert not r.store.rows("SELECT * FROM outbox")


async def test_auto_replies_short_menu_rules_and_settings(ux):
    r = ux
    await r.ui.click("ar:menu")
    shown = r.session.calls[-1]
    assert "runtime" not in shown.text and "regex" not in shown.text
    assert "Правил: 0" in shown.text
    assert {"ar:new", "ar:list", "ar:settings", "ar:history:0"} <= set(buttons(shown))
    await r.ui.click("ar:new")
    for value in ("Привет", "Здравствуйте!", "600", "20"):
        await r.ui.message(value)
    commit = next(k for k in buttons(r.session.calls[-1]) if k.startswith("ar:commit:"))
    await r.ui.click(commit)
    assert "версия" not in r.session.calls[-1].text
    await r.ui.click("ar:list")
    rule = next(k for k in buttons(r.session.calls[-1]) if k.startswith("ar:show:"))
    await r.ui.click(rule)
    assert "Если покупатель напишет" in r.session.calls[-1].text
    assert not r.store.rows("SELECT * FROM outbox")


async def test_operations_list_hides_technical_identifiers_but_details_preserve_them(ux):
    r = ux
    ident = r.store.enqueue("mock-only-unknown", "gift_send", "mock-order", {}, actor=7)
    r.store.execute("UPDATE outbox SET status='UNCERTAIN' WHERE id=?", (ident,))
    await r.ui.click("ops:list:ALL:0")
    label = buttons(r.session.calls[-1])["ops:show:" + ident]
    assert "UNCERTAIN" not in label and "gift_send" not in label and ident[:8] not in label
    assert "неизвестен" in label
    await r.ui.click("ops:show:" + ident)
    assert ident in r.session.calls[-1].text
    assert not r.api.calls


async def test_render_production_screen_cards_offline(ux, tmp_path):
    """Save true production text/keyboards as offline cards, never as Telegram screenshots."""
    import asyncio
    import html
    import os
    import re
    import subprocess
    import textwrap
    from pathlib import Path

    from PIL import Image, ImageDraw, ImageFont

    r = ux
    output = Path(os.environ.get("PCC_RENDER_OUTPUT", tmp_path / "renders"))
    output.mkdir(parents=True, exist_ok=True)
    font_path = Path("C:/Windows/Fonts/segoeui.ttf")
    font = ImageFont.truetype(str(font_path), 20) if font_path.exists() else ImageFont.load_default()
    small = ImageFont.truetype(str(font_path), 15) if font_path.exists() else font
    emoji_path = Path("C:/Windows/Fonts/seguiemj.ttf")
    emoji_fonts = (
        {size: ImageFont.truetype(str(emoji_path), size) for size in (15, 20)} if emoji_path.exists() else {}
    )

    def draw_line(draw, xy, text, face, fill):
        x, y = xy
        for character in text:
            if character in ("\ufe0f", "\ufe0e"):
                continue
            symbol = ord(character) >= 0x1F000 or 0x2300 <= ord(character) <= 0x2BFF
            selected = emoji_fonts.get(getattr(face, "size", 20), face) if symbol else face
            draw.text((x, y), character, font=selected, fill=fill)
            x += draw.textlength(character, font=selected)

    cards = []

    def save_card(name, text, markup):
        plain = html.unescape(re.sub("<[^>]*>", "", text or ""))
        lines = [part for line in plain.splitlines() for part in (textwrap.wrap(line, 53) or [""])]
        rows = markup.inline_keyboard if markup else []
        height = (
            105
            + len(lines) * 28
            + sum(
                max(1, max(len(textwrap.wrap(b.text, max(12, 47 // len(row)))) for b in row)) * 24 + 20
                for row in rows
            )
        )
        image = Image.new("RGB", (660, height), "#eef4f0")
        draw = ImageDraw.Draw(image)
        draw.text((25, 15), "OFFLINE · TEST TRANSPORT · НЕ TELEGRAM", font=small, fill="#50635b")
        draw.rounded_rectangle((15, 48, 645, 73 + len(lines) * 28), radius=14, fill="white")
        y = 62
        for line in lines:
            draw_line(draw, (30, y), line, font, "#172e25")
            y += 28
        y += 22
        for row in rows:
            width = 620 // len(row)
            rowlines = [textwrap.wrap(button.text, max(12, 47 // len(row))) or [""] for button in row]
            h = max(len(value) for value in rowlines) * 24 + 12
            for i, value in enumerate(rowlines):
                x = 20 + i * width
                draw.rounded_rectangle((x, y, x + width - 6, y + h), radius=7, fill="#d5e8dd")
                for j, line in enumerate(value):
                    draw_line(draw, (x + 9, y + 5 + j * 24), line, small, "#164d3b")
            y += h + 8
        image.save(output / (name + ".png"))
        cards.append(
            f'<article><h2>{html.escape(name)}</h2><img src="{html.escape(name)}.png" alt="{html.escape(name)}" /></article>'
        )
        assert len(text or "") <= 4096
        assert all(len((b.callback_data or "").encode()) <= 64 for row in rows for b in row)

    original = r.api.call

    async def call(method, **kwargs):
        if method == "get":
            return {"id": "seller", "username": "FixtureOwner", "profile": {}, "is_blocked": False}
        if method == "get_transaction_providers":
            from tests.test_withdrawals import snapshot

            return [snapshot(minimum=1)["provider"]]
        if method == "get_item_priority_statuses":
            return [{"id": "test-premium", "type": "PREMIUM", "price": 25, "period": 7}]
        return await original(method, **kwargs)

    r.api.call = call
    r.api.value["raw_price"] = 100
    r.api.value["fee_multiplier"] = "0.1"
    routes = [
        ("01_home", "home"),
        ("02_order", "order:paid"),
        ("03_order_history", "od:history:paid:0"),
        ("04_statistics", "stats:7"),
        ("05_autoreplies", "ar:menu"),
        ("06_chats", "chats"),
        ("07_conversation", "chat:chat"),
        ("08_product", "item:lot"),
        ("09_automation", "auto:item:lot"),
        ("10_gifts", "gft:home"),
        ("11_premium", "payp:item:lot"),
        ("12_operations", "ops:list:ALL:0"),
        ("13_delivery_result", "delivered:0"),
        ("14_net_forecast", "finance:lot"),
        ("15_withdrawal_preference", "providers"),
        ("16_auto_promotion", "promotions"),
        ("17_profile", "profile"),
        ("18_active_items", "items"),
        ("19_promotion_catalog", "promo:catalog"),
        ("20_item_schedule", "promo:item:lot"),
        ("21_advanced_schedule", "DYNAMIC_PROMOTION_MORE"),
    ]
    for name, route in routes:
        if route == "DYNAMIC_PROMOTION_MORE":
            route = next(key for key in buttons(r.session.last_panel) if ":more:" in key)
        await r.ui.click(route)
        method = r.session.last_panel
        assert "❗" not in (method.text or ""), route
        save_card(name, method.text, method.reply_markup)
    # Historical source classes run over the same isolated fixtures, not hand-written mockups.
    for name, file, cls in [
        ("before_order", "orders_panel", "OrdersPanel"),
        ("before_statistics", "statistics_panel", "StatisticsPanel"),
        ("before_autoreplies", "auto_replies_panel", "AutoRepliesPanel"),
    ]:
        if not os.environ.get("PCC_RENDER_OUTPUT"):
            continue  # Historical comparison is an explicit local artifact task, CI may be shallow.
        source = await asyncio.to_thread(
            subprocess.check_output, ["git", "show", "a65643b:app/telegram/" + file + ".py"], encoding="utf-8"
        )
        namespace = {"__name__": "offline_before_" + file}
        exec(compile(source, file, "exec"), namespace)  # noqa: S102 -- fixed trusted local historical commit, test-only fixture
        panel = namespace[cls](r.store, r.api) if cls != "StatisticsPanel" else namespace[cls](r.store)
        if cls == "OrdersPanel":
            text, markup = await panel.card(7, "paid")
        elif cls == "AutoRepliesPanel":
            text, markup = panel.menu(7)
        else:

            class Capture:
                async def answer(self, text, reply_markup=None):
                    return text, reply_markup

            text, markup = await panel.handle("stats:7", Capture(), owner=7)
        save_card(name, text, markup)
    (output / "index.html").write_text(
        '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Offline production UI acceptance</title><style>body{font:16px system-ui;background:#eaf0ec;margin:30px}main{display:flex;flex-wrap:wrap;gap:24px}article{max-width:660px}img{width:100%;max-width:660px}h1{font-size:22px}</style><h1>Достоверные offline-рендеры production-сообщений и клавиатур. Не снимки Telegram.</h1><p>Только временная SQLite, тестовые аккаунты и транспорты. Исходные пользовательские скриншоты в текущем вложении отсутствуют. BEFORE — исходный renderer a65643b на тех же безопасных фикстурах.</p><main>'
        + "".join(cards)
        + "</main></html>",
        encoding="utf-8",
    )
    assert not r.store.rows("SELECT * FROM outbox")
