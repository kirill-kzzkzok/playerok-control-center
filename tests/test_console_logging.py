import io
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.security.logging import ConsoleFormatter, SecretFilter, ansi_supported
from tests.test_unified_delivery_controls import shop as shop_fixture

shop = shop_fixture


def test_live_console_file_rotation_no_duplicate_or_private_data(tmp_path):
    environment = {**os.environ, "PYTHONPATH": str(Path.cwd()), "PCC_LOG_LEVEL": "DEBUG", "NO_COLOR": "1"}
    script = """
import logging
from app.security.logging import configure_logging,event
configure_logging(['dummy-token-secret','dummy-api-hash'])
configure_logging(['dummy-token-secret','dummy-api-hash'])
event('ORDER','Пакет 🧸 ×2, 🌹 ×1',order='sale-1',operation='operation-1',position=1)
logging.getLogger('pcc.shop').debug('username @Realbuyer api_hash=dummy-api-hash token=dummy-token-secret access_hash=987654321 session_string=private-string C:/private/account.session')
event('SUCCESS','Доставка подтверждена',order='sale-1',position=1)
logging.getLogger('aiogram.event').info('polling-noise')
for h in logging.getLogger().handlers:
    if hasattr(h,'maxBytes'):
        h.maxBytes=100
        h.backupCount=2
for i in range(12):
    logging.warning('Safe rotation event number %d',i)
logging.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert output.count("Пакет 🧸 ×2, 🌹 ×1") == 1
    assert "🛒 ЗАКАЗ" in output and "✅ УСПЕХ" in output and "[DEBUG" not in output
    assert "sale-1" not in output
    assert "operation_id=" not in output and "gift_position=" not in output
    assert "polling-noise" not in output and "\x1b" not in output
    logs = list((tmp_path / "logs").glob("control_center.log*"))
    assert len(logs) == 3
    combined = output + "\n" + "".join(p.read_text(encoding="utf-8") for p in logs)
    for value in [
        "Realbuyer",
        "dummy-token-secret",
        "dummy-api-hash",
        "987654321",
        "private-string",
        "account.session",
    ]:
        assert value not in combined


def test_console_color_fallback_moscow_clock_and_control_character_sanitizing():
    stream = io.StringIO()
    assert ansi_supported(stream) is False
    record = logging.LogRecord("shop", logging.ERROR, "", 1, "@Realbuyer\nsecret=private\x1b[31m", (), None)
    record.created = 0
    record.order_id = "@Realbuyer\ninvalid"
    record.operation_id = "sensitive-token"
    SecretFilter(["sensitive-token"]).filter(record)
    rendered = ConsoleFormatter(color=False).format(record)
    assert rendered.startswith("[03:00:00]") and "\n" not in rendered and "\x1b" not in rendered
    assert "private" not in rendered and "sensitive-token" not in rendered
    colored = ConsoleFormatter(color=True).format(record)
    assert colored.startswith("\x1b[31m") and colored.endswith("\x1b[0m")


def test_debug_dict_private_fields_and_jwt_are_also_redacted():
    record = logging.LogRecord(
        "shop",
        logging.DEBUG,
        "",
        1,
        {
            "access_hash": 987654321,
            "session_string": "private session value",
            "api_hash": "unlisted-secret-hash",
            "cookie": "private cookie string",
            "token": "unlisted-token-secret",
        },
        (),
        None,
    )
    SecretFilter().filter(record)
    rendered = ConsoleFormatter().format(record)
    for value in [
        "987654321",
        "private session value",
        "unlisted-secret-hash",
        "private cookie string",
        "unlisted-token-secret",
    ]:
        assert value not in rendered


def test_windows_launcher_runs_foreground_once_and_keeps_error_visible():
    source = Path("start.bat").read_text(encoding="utf-8").lower()
    assert source.count(" -m app") == 1 and "python.exe -u -m app" in source
    assert "pythonunbuffered=1" in source and "chcp 65001" in source
    assert "pause" in source and "if errorlevel 1" in source
    assert "start " not in source and "pythonw" not in source


def test_log_file_failure_is_reported_once_without_record_payload(tmp_path, capsys):
    from app.security.logging import ResilientFileHandler

    handler = ResilientFileHandler(tmp_path / "log.txt")
    record = logging.LogRecord("shop", logging.ERROR, "", 1, "private buyer message", (), None)
    handler.handleError(record)
    handler.handleError(record)
    handler.close()
    text = capsys.readouterr().err
    assert text.count("Журнал недоступен") == 1
    assert "private buyer" not in text


def test_raw_telegram_exception_is_classified_before_any_log_sink():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageText

    error = TelegramBadRequest(
        method=EditMessageText(text="private buyer text", chat_id=7, message_id=1),
        message="can't parse entities private buyer text",
    )
    record = logging.LogRecord(
        "aiogram.dispatcher",
        logging.ERROR,
        "",
        1,
        "Unhandled Telegram: %s",
        (error,),
        (type(error), error, None),
    )
    SecretFilter().filter(record)
    assert "private buyer text" not in record.getMessage()
    assert "ENTITY_PARSE_ERROR" in record.getMessage()


def test_periodic_checks_stay_in_existing_debug_file_only(tmp_path):
    script = """
import logging
from app.security.logging import configure_logging,event
configure_logging()
event('CHECK','Баланс личного Telegram: 135 Stars; каталог обновлён')
event('ORDER','Новый оплаченный заказ',order='sale-1')
event('WARNING','Результат отправки неизвестен',order='sale-1',level=30)
logging.getLogger('pcc.shop').debug('Playerok method=get_chat_messages code=HTTP_500')
logging.shutdown()
"""
    result = subprocess.run(
        [sys.executable, "-u", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(Path.cwd()), "PCC_LOG_LEVEL": "INFO"},
        capture_output=True,
        encoding="utf-8",
        timeout=20,
        check=False,
    )
    assert result.returncode == 0
    assert "каталог обновлён" not in result.stdout
    assert "Новый оплаченный заказ" in result.stdout and "Результат отправки неизвестен" in result.stdout
    debug = (tmp_path / "logs/control_center.log").read_text(encoding="utf-8")
    assert "каталог обновлён" in debug and "HTTP_500" in debug


@pytest.mark.parametrize(
    "mode,value",
    [
        ("unique_stock", None),
        ("fixed_text", "private-delivery-secret"),
        ("telegram_gifts", "gift: heart"),
    ],
)
async def test_business_logs_follow_durable_delivery_once_after_restart(shop, caplog, mode, value):
    from app.gifts.worker import GiftWorker
    from app.services.orders import Orders
    from app.workers.delivery_chains import ChainWorker
    from app.workers.enrollment import EnrollmentWorker
    from tests.test_logical_faults import reopen
    from tests.test_universal_delivery_e2e import setup_ui

    r = await shop(gift=mode == "telegram_gifts")
    await setup_ui(r, mode, value)
    caplog.set_level(logging.INFO)
    order = r.api.order("logged-sale")
    Orders(r.store).ingest(order)
    Orders(r.store).ingest(order)
    for _ in range(8):
        await EnrollmentWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    reopen(r)
    Orders(r.store).ingest(order)
    for _ in range(3):
        await EnrollmentWorker(r.store, r.api).once()
        await GiftWorker(r.store, r.api, r.api.gift_settings, r.api.gift_sender).once()
        ChainWorker(r.store, r.api).once()
        await r.worker.once()
    messages = [
        record.getMessage() for record in caplog.records if getattr(record, "order_id", None) == "logged-sale"
    ]
    kinds = [
        getattr(record, "event_type", "")
        for record in caplog.records
        if getattr(record, "order_id", None) == "logged-sale"
    ]
    for kind in ("🛒 ПОКУПКА", "⚙️ ВЫДАЧА", "🎁 ВЫДАНО"):
        assert kinds.count(kind) == 1
    assert not any(
        "private-delivery-secret" in message or "isolated-unit-" in message for message in messages
    )
    assert not any("Выполнение заказа подтверждено" in message for message in messages)
