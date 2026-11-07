"""Startup backup must not block the real Dispatcher or outlive its Store."""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot
from aiogram.types import WebhookInfo

from app.config import Config
from app.database.store import Store
from tests.test_playerok_connection import API
from tests.test_telegram_integration import Interaction, TelegramSession


@pytest.mark.parametrize("check_overlap", [False, True])
async def test_slow_backup_does_not_delay_polling_or_start_and_is_drained_on_shutdown(
    tmp_path, monkeypatch, check_overlap
):
    from app import __main__ as application

    monkeypatch.chdir(tmp_path)
    store = Store(tmp_path / "startup.sqlite")
    store.claim_first_owner(7)
    api = API()
    api.account.writes_enabled = api.account.automation_enabled = False
    session = TelegramSession()
    bot = Bot("123:" + "x" * 35, session=session)
    overlapping = []
    if check_overlap:
        me = bot.me

        async def observe_me():
            await asyncio.sleep(0)
            overlapping.append(api.started.is_set())
            return await me()

        monkeypatch.setattr(bot, "me", observe_me)
    monkeypatch.setattr(
        bot,
        "get_webhook_info",
        AsyncMock(return_value=WebhookInfo(url="", has_custom_certificate=False, pending_update_count=0)),
    )
    monkeypatch.setattr(application, "Store", lambda _: store)
    monkeypatch.setattr(application, "SafeAccount", lambda *a, **k: api.account)
    monkeypatch.setattr(application, "Playerok", lambda _: api)
    monkeypatch.setattr(application, "AiohttpSession", lambda **k: session)
    monkeypatch.setattr(application, "Bot", lambda *a, **k: bot)
    original_start = application.Supervisor.start

    def start(self, name, factory, stop):
        if name == "upload-cleanup":
            return original_start(self, name, factory, stop)
        return asyncio.create_task(stop.wait())

    monkeypatch.setattr(application.Supervisor, "start", start)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_backup(value, directory):
        entered.set()
        assert release.wait(10)
        value.set_setting("test:backup-finished", True)
        finished.set()
        return {"status": "OK"}

    monkeypatch.setattr("app.services.backup_retention.attempt_startup_backup", slow_backup)
    ready, finish_polling = asyncio.Event(), asyncio.Event()
    close_store = Mock(wraps=store.close)
    monkeypatch.setattr(store, "close", close_store)

    async def polling(dispatcher, *args, **kwargs):
        await Interaction(bot, dispatcher).message("/start")
        assert session.last_panel.text
        ready.set()
        await finish_polling.wait()
        dispatcher._stop_signal = asyncio.Event()
        dispatcher._stop_signal.set()

    monkeypatch.setattr(application.Dispatcher, "start_polling", polling)
    config = Config(
        "123:" + "x" * 35, "token=test", frozenset({7}), database_path=store.path, realtime_enabled=False
    )
    started = time.perf_counter()
    running = asyncio.create_task(application.run_connected(config))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        if check_overlap:
            assert overlapping == [True]
        elapsed = time.perf_counter() - started
        assert elapsed < 3
        print(f"Controlled polling and /start: {elapsed:.3f}s; backup still pending")
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set() and not finished.is_set()
        assert not store.rows("SELECT * FROM outbox")
        finish_polling.set()
        await asyncio.sleep(0.05)
        assert not running.done()
        close_store.assert_not_called()
    finally:
        finish_polling.set()
        release.set()
        await asyncio.wait_for(running, 5)
        await bot.session.close()
    assert finished.is_set()
    close_store.assert_called_once()


async def test_repeated_cleanup_cancellation_waits_for_backup_thread(tmp_path, monkeypatch):
    from app.workers.cleanup import UploadCleanup

    store = Store(tmp_path / "cleanup.sqlite")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def backup(value, directory):
        entered.set()
        assert release.wait(5)
        value.set_setting("test:backup-finished", True)
        finished.set()
        return {"status": "OK"}

    monkeypatch.setattr("app.services.backup_retention.attempt_startup_backup", backup)
    store.set_setting("backups:retention", {"status": "ERROR"})
    task = asyncio.create_task(UploadCleanup(store).run(asyncio.Event()))
    try:
        for _ in range(100):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set() and store.setting("test:backup-finished")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        store.close()


def test_application_import_defers_chart_engine_until_chart_is_requested():
    import subprocess
    import sys
    from pathlib import Path

    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-X",
            "utf8",
            "-c",
            "import sys; import app.__main__; assert 'matplotlib' not in sys.modules",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-1000:]
