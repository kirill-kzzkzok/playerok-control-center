import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.strategy import FSMStrategy

from app.config import Config, ConfigurationError
from app.database.store import Store
from app.gifts.settings import GiftSettings
from app.gifts.worker import GiftWorker
from app.playerok.adapter import Playerok, SafeAccount
from app.playerok.thread_calls import begin_close_calls, close_calls
from app.security.access import OwnerMiddleware
from app.security.instance import InstanceLock
from app.security.logging import configure_logging, event
from app.services.interface_cleanup import InterfaceCleanup
from app.services.orders import Orders
from app.services.playerok_connection import PlayerokConnection
from app.services.products import Products
from app.telegram.panel_tracking import PanelTracking
from app.telegram.router import create_router
from app.telegram.storage import SQLiteStorage
from app.workers.auto_replies import AutoReplyWorker
from app.workers.cleanup import UploadCleanup
from app.workers.delivery_chains import ChainWorker
from app.workers.enrollment import EnrollmentWorker
from app.workers.notifications import Notifications
from app.workers.outbox import Executor, OutboxWorker
from app.workers.promotions import PromotionWorker
from app.workers.realtime import Realtime
from app.workers.supervisor import Supervisor
from app.workers.sync import Synchronizer
from app.workers.transactions import TransactionWorker

logger = logging.getLogger(__name__)


async def outbox_loop(worker, stop):
    while not stop.is_set():
        processed = await worker.once() if worker.executor.api.account.writes_enabled else False
        if not processed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                continue


async def run(config):
    event("INFO", "Запуск Playerok Control Center")
    store = Store(config.database_path)
    account = SafeAccount(config.playerok_header, writes_enabled=config.writes_enabled)
    account.automation_enabled = config.automation_enabled
    api = Playerok(account)
    session = AiohttpSession(timeout=35)
    if config.use_proxy:
        session.api = TelegramAPIServer.from_base(config.telegram_proxy)
    bot = Bot(config.telegram_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    session.middleware(PanelTracking(store))
    tasks = []
    stop = asyncio.Event()
    supervisor = Supervisor(store)

    async def when_connected(factory):
        while not stop.is_set():
            if getattr(api, "connection", None) and api.connection.state["status"] == "CONNECTED":
                return await factory()
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass

    try:
        for owner in config.owner_ids:
            store.execute("INSERT OR IGNORE INTO administrators VALUES(?)", (owner,))
        recovered = store.recover()
        InterfaceCleanup(store).recover()
        if recovered:
            logger.warning("Recovered %s operations as UNCERTAIN", recovered)
        event("CHECK", "Проверка Telegram Bot...")
        await bot.get_me()
        event("SUCCESS", "Telegram Bot подключён")
        connection = PlayerokConnection(store, api)
        await connection.check(cooldown=0)
        store.backup(
            Path("data/backups") / (datetime.now(UTC).strftime("startup-%Y%m%dT%H%M%S") + ".sqlite3")
        )
        orders = Orders(store)
        products = Products(store, api)
        sync = Synchronizer(store, api, orders)
        dispatcher = Dispatcher(
            storage=SQLiteStorage(store),
            fsm_strategy=FSMStrategy.USER_IN_CHAT,
            events_isolation=SimpleEventIsolation(),
        )
        middleware = OwnerMiddleware(config.owner_ids, store, config.first_start_claim)
        dispatcher.message.outer_middleware(middleware)
        dispatcher.callback_query.outer_middleware(middleware)
        dispatcher.include_router(create_router(store, api, products, sync))
        tasks.append(supervisor.start("upload-cleanup", lambda: UploadCleanup(store).run(stop), stop))
        tasks.append(supervisor.start("notifications", lambda: Notifications(store, bot).run(stop), stop))
        tasks.append(
            supervisor.start(
                "account-transactions",
                lambda: when_connected(lambda: TransactionWorker(store, api).run(stop)),
                stop,
            )
        )
        tasks.append(
            supervisor.start(
                "promotion-dry-run",
                lambda: when_connected(lambda: PromotionWorker(store, api).run(stop)),
                stop,
            )
        )
        tasks.append(supervisor.start("playerok-sync", lambda: when_connected(lambda: sync.run(stop)), stop))
        if config.realtime_enabled:
            tasks.append(
                supervisor.start(
                    "playerok-realtime",
                    lambda: when_connected(lambda: Realtime(store, account, sync).run(stop)),
                    stop,
                )
            )
        if config.writes_enabled:
            if config.automation_enabled:
                tasks.append(
                    supervisor.start("auto-replies", lambda: AutoReplyWorker(store, api).run(stop), stop)
                )
                tasks.append(
                    supervisor.start(
                        "future-order-enrollment", lambda: EnrollmentWorker(store, api).run(stop), stop
                    )
                )
                tasks.append(
                    supervisor.start("delivery-chains", lambda: ChainWorker(store, api).run(stop), stop)
                )
                settings = getattr(api, "gift_settings", None) or GiftSettings.load()
                tasks.append(
                    supervisor.start(
                        "telegram-gifts",
                        lambda: GiftWorker(store, api, settings, api.gift_sender).run(stop),
                        stop,
                    )
                )
            worker = OutboxWorker(store, Executor(store, api, products))
            tasks.append(supervisor.start("outbox", lambda: outbox_loop(worker, stop), stop))
        logger.info(
            "Telegram panel started; writes=%s; first_start_claim=%s",
            config.writes_enabled,
            config.first_start_claim,
        )
        event(
            "INFO",
            "Бот готов к работе"
            if connection.state["status"] == "CONNECTED"
            else "Telegram-панель доступна для диагностики; операции записи и автовыдача заблокированы",
        )
        # Never delete webhook or drop pending updates silently.
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ConfigurationError("У бота активен webhook. Отключите его явно перед polling.")
        await dispatcher.start_polling(
            bot, allowed_updates=["message", "callback_query"], close_bot_session=False, polling_timeout=20
        )
    finally:
        event("INFO", "Остановка приложения: новые операции запрещены, ожидание текущих запросов")
        stop.set()
        begin_close_calls(api)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Cancelling a waiter cannot interrupt synchronous HTTP or its thread-side guards.
        await close_calls(api)
        if getattr(api, "gift_sender", None):
            await api.gift_sender.close()
        await bot.session.close()
        account.close()
        store.close()
        event("INFO", "Telegram Bot и Playerok отключены. Приложение остановлено")


def main():
    try:
        config = Config.load()
        configure_logging([config.telegram_token, config.playerok_header, GiftSettings.load().api_hash])
        config.require_owner()
        with InstanceLock(str(config.database_path) + ".lock"):
            asyncio.run(run(config))
    except KeyboardInterrupt:
        return 0
    except (ConfigurationError, RuntimeError) as error:
        logger.error("%s", str(error) if isinstance(error, ConfigurationError) else type(error).__name__)
        return 1
    except Exception as error:  # noqa: BLE001 -- final boundary logs type only to protect credentials
        # No URL, raw response, or library exception traceback in user-facing logs.
        logger.error("Startup/runtime failed: %s", type(error).__name__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
