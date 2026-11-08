import logging

import pytest

from app.security.logging import ConsoleFormatter
from app.services.paid_greeting import render
from app.services.shop_events import order_notice

ORDER = {
    "id": "sale/123",
    "item": {"name": "🎮 Steam <Казахстан> & доступ [A1B]", "price": 90},
    "user": {"username": "Juriksss"},
}


@pytest.mark.parametrize(
    "stage,title",
    [
        ("PAID", "НОВАЯ ПОКУПКА"),
        ("DELIVERED", "ТОВАР ВЫДАН"),
        ("SENT", "ЗАКАЗ ВЫПОЛНЕН"),
        ("CONFIRMED", "ЗАКАЗ ПОЛУЧЕН"),
        ("ROLLED_BACK", "ВОЗВРАТ ПОДТВЕРЖДЁН"),
    ],
)
def test_order_notice_keeps_full_title_and_safe_deal_link(stage, title):
    text = order_notice(ORDER, stage)
    assert title in text and "━━━━━━━━━━━━━━━━━━" in text
    assert "📦 🎮 Steam &lt;Казахстан&gt; &amp; доступ [A1B]" in text
    assert "👤 Juriksss" in text
    assert text.endswith('\n\n🔗 <a href="https://playerok.com/deal/sale%2F123">Открыть заказ</a>')


@pytest.mark.parametrize("rating", range(1, 6))
def test_review_renders_real_rating_and_omits_empty_text(rating):
    text = order_notice(ORDER, None, {"rating": rating, "text": ""})
    assert f"{'⭐' * rating} {rating}/5" in text
    assert "💬" not in text


def test_owner_console_has_no_category_or_order_id():
    record = logging.makeLogRecord(
        {"msg": "Покупка", "levelno": logging.INFO, "event_type": "🛒 ПОКУПКА", "order_id": "internal-sale"}
    )
    assert ConsoleFormatter(technical=False).format(record)[10:] == "ℹ️ Покупка"
    assert "order_id=internal-sale" in ConsoleFormatter().format(record)


def test_greeting_displays_public_username_but_not_unvalidated_data():
    text = render(
        {
            "item": {"name": "Товар"},
            "obtaining_fields": [
                {"id": "tg", "label": "Введите @username", "value": "@buyer"},
                {"id": "other", "label": "Данные", "value": "secret-value"},
                {"id": "bad", "label": "Введите @username", "value": "private@example.org"},
            ],
        }
    )
    assert "@username: @buyer" in text
    assert "secret-value" not in text and "private@example.org" not in text
