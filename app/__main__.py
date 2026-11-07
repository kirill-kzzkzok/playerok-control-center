import asyncio
import logging
import sys
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.fsm.strategy import FSMStrategy

from app.config import Config, ConfigurationError
from app.database.store import Store
from app.gifts.settings import GiftSettings
from app.gifts.worker import GiftWorker
from app.localvpn import SOCKS_URL, NetworkSettings, PlayerokProxy, ProxyUnavailable, open_proxy
from app.playerok.adapter import Playerok, SafeAccount
from app.playerok.thread_calls import begin_close_calls, close_calls
from app.security.access import OwnerMiddleware
from app.security.instance import InstanceLock
from app.security.logging import configure_logging, event
from app.services.interface_cleanup import InterfaceCleanup
from app.services.orders import Orders
from app.services.playerok_connection import REASONS, PlayerokConnection
from app.services.products import Products
from app.telegram.callback_ack import install_callback_ack
from app.telegram.panel_tracking import PanelTracking
from app.telegram.router import create_router
from app.telegram.storage import SQLiteStorage
from app.telegram.transport import TelegramNetworkSession as AiohttpSession
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
    # One durable worker, independent bounded preflight lanes. Actual Playerok
    # mutations still share the transport fence; paid reads must not occupy the
    # stock/text scheduler lane. Stars purchases never overlap each other.
    async with asyncio.TaskGroup() as group:
        group.create_task(outbox_lane(worker, stop, kinds=("gift_send",)))
        group.create_task(outbox_lane(worker, stop, kinds=("premium_publish", "premium_bump")))
        group.create_task(
            outbox_lane(worker, stop, excluded_kinds=("gift_send", "premium_publish", "premium_bump"))
        )


async def outbox_lane(worker, stop, *, kinds=None, excluded_kinds=()):
    while not stop.is_set():
        account = worker.executor.api.account
        processed = (
            await worker.once(kinds=kinds, excluded_kinds=excluded_kinds)
            if account.writes_enabled
            else (
                await worker.once(kinds=("agreements",), excluded_kinds=excluded_kinds)
                if getattr(account, "auto_accept_agreements", False)
                and getattr(account, "connection_verified", False)
                and (kinds is None or "agreements" in kinds)
                else False
            )
        )
        if not processed:
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                continue


async def run(config, *, network_diagnostics=False, instance_lock=None):
    if getattr(config, "telegram_transport", "legacy") == "localvpn":
        async with open_proxy(NetworkSettings.load(), PlayerokProxy, recover_on_start=True):
            return await run_connected(
                config, network_diagnostics=network_diagnostics, instance_lock=instance_lock
            )
    return await run_connected(config, network_diagnostics=network_diagnostics, instance_lock=instance_lock)


async def wait_for_telegram(bot, stop, *, verified=False, retry_at=0):
    """Retry safe startup reads without holding independent Playerok workers."""
    failures = 0
    while not stop.is_set():
        delay = max(0, retry_at - time.monotonic())
        if delay:
            try:
                await asyncio.wait_for(stop.wait(), delay)
                return False
            except TimeoutError:
                pass
        try:
            if not verified:
                await bot.me()
                verified = True
            webhook = await bot.get_webhook_info()
        except TelegramRetryAfter as error:
            retry_at = time.monotonic() + max(1, error.retry_after)
        except (TelegramNetworkError, TimeoutError, OSError):
            failures += 1
            retry_at = time.monotonic() + min(30, 2 ** min(failures, 5))
        else:
            if webhook.url:
                raise ConfigurationError("У бота активен webhook. Отключите его явно перед polling.")
            return True
    return False


async def run_connected(config, *, network_diagnostics=False, instance_lock=None):
    event("INFO", "Запуск Playerok Control Center")
    store = Store(config.database_path)
    account = SafeAccount(config.playerok_header, writes_enabled=config.writes_enabled)
    account.automation_enabled = config.automation_enabled
    account.auto_accept_agreements = getattr(config, "auto_accept_agreements", False)
    api = Playerok(account)
    mode = getattr(config, "telegram_transport", "legacy")
    worker = mode == "worker" or (mode == "legacy" and config.use_proxy)
    session_args = {"api": TelegramAPIServer.from_base(config.telegram_proxy)} if worker else {}
    if mode == "localvpn":
        session_args.update(proxy=SOCKS_URL, localvpn=True)
    from app.telegram.system_tunnel import happ_required

    if happ_required(mode):
        session_args["system_tunnel"] = True
    session = AiohttpSession(timeout=35, diagnostics=network_diagnostics, **session_args)
    event(
        "CHECK",
        "Telegram Bot API: "
        + ("SOCKS5 127.0.0.1:1080" if mode == "localvpn" else "Cloudflare Worker" if worker else "Direct"),
    )
    event("CHECK", "getUpdates / answerCallbackQuery: NOT TESTED — ожидаются реальные запросы")
    bot = Bot(config.telegram_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    session.middleware(PanelTracking(store))
    tasks = []
    stop = asyncio.Event()
    supervisor = Supervisor(store)

    async def when_connected(factory):
        while not stop.is_set():
            if getattr(api, "connection", None) and api.connection.reads_allowed:
                return await factory()
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass

    try:
        for owner in config.owner_ids:
            store.execute("INSERT OR IGNORE INTO administrators VALUES(?)", (owner,))
        recovered = store.recover()
        if instance_lock is not None:
            EnrollmentWorker(store, api).recover_startup(instance_lock)
        InterfaceCleanup(store).recover()
        if recovered:
            logger.warning("Recovered %s operations as UNCERTAIN", recovered)
        event("CHECK", "Проверка Telegram Bot...")
        telegram_verified, telegram_retry_at = False, 0
        try:
            async with asyncio.timeout(2):
                await bot.me()
            telegram_verified = True
            event("SUCCESS", "Telegram Bot подключён")
        except TelegramRetryAfter as error:
            telegram_retry_at = time.monotonic() + max(1, error.retry_after)
        except (TelegramNetworkError, TimeoutError, OSError):
            telegram_retry_at = time.monotonic() + 1
        if not telegram_verified:
            event(
                "WARNING",
                "Telegram восстанавливает соединение; разрешённые операции Playerok продолжаются",
                level=30,
            )
        connection = PlayerokConnection(store, api)
        verification = await connection.check(cooldown=0)
        if verification["status"] == "CONNECTED":
            event("SUCCESS", "Playerok подключён. Аккаунт подтверждён")
        tasks.append(supervisor.start("playerok-connection-recovery", lambda: connection.run(stop), stop))
        orders = Orders(store)
        products = Products(store, api)
        from app.services.logical_products import LogicalProducts

        LogicalProducts(store, api).migrate_legacy()
        sync = Synchronizer(store, api, orders)
        dispatcher = Dispatcher(
            storage=SQLiteStorage(store),
            fsm_strategy=FSMStrategy.USER_IN_CHAT,
            events_isolation=SimpleEventIsolation(),
        )
        middleware = OwnerMiddleware(config.owner_ids, store, config.first_start_claim)
        install_callback_ack(dispatcher, middleware)
        dispatcher.message.outer_middleware(middleware)
        dispatcher.callback_query.outer_middleware(middleware)
        dispatcher.include_router(create_router(store, api, products, sync))
        tasks.append(
            supervisor.start(
                "upload-cleanup", lambda: UploadCleanup(store).run(stop, startup_backup=True), stop
            )
        )
        tasks.append(
            supervisor.start("notifications", lambda: Notifications(store, bot, api).run(stop), stop)
        )
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
        from app.services.product_readiness import ProductReadiness

        tasks.append(
            supervisor.start(
                "product-readiness",
                lambda: when_connected(lambda: ProductReadiness(store, api).run(stop)),
                stop,
            )
        )
        if config.realtime_enabled:
            tasks.append(
                supervisor.start(
                    "playerok-realtime",
                    lambda: when_connected(lambda: Realtime(store, account, sync).run(stop)),
                    stop,
                )
            )
        if config.writes_enabled and config.automation_enabled:
            from app.workers.logical_products import LogicalProductWorker

            tasks.append(
                supervisor.start(
                    "logical-products",
                    lambda: when_connected(lambda: LogicalProductWorker(store, api).run(stop)),
                    stop,
                )
            )
            tasks.append(
                supervisor.start("auto-replies", lambda: AutoReplyWorker(store, api).run(stop), stop)
            )
            tasks.append(
                supervisor.start(
                    "future-order-enrollment", lambda: EnrollmentWorker(store, api).run(stop), stop
                )
            )
            tasks.append(supervisor.start("delivery-chains", lambda: ChainWorker(store, api).run(stop), stop))
            settings = getattr(api, "gift_settings", None) or GiftSettings.load()
            tasks.append(
                supervisor.start(
                    "telegram-gifts",
                    lambda: GiftWorker(store, api, settings, api.gift_sender).run(stop),
                    stop,
                )
            )
        if config.writes_enabled or getattr(config, "auto_accept_agreements", False):
            worker = OutboxWorker(store, Executor(store, api, products))
            tasks.append(supervisor.start("outbox", lambda: outbox_loop(worker, stop), stop))
        if getattr(config, "auto_accept_agreements", False):
            from app.workers.agreements import AgreementResumer

            tasks.append(
                supervisor.start(
                    "agreements-resume",
                    lambda: AgreementResumer(store, api, products, dispatcher, bot).run(stop),
                    stop,
                )
            )
        logger.info(
            "Telegram panel started; writes=%s; first_start_claim=%s",
            config.writes_enabled,
            config.first_start_claim,
        )
        event(
            "INFO",
            "Бот готов к работе"
            if connection.state["status"] == "CONNECTED"
            else "Telegram-панель доступна для диагностики; операции записи и автовыдача заблокированы. "
            + REASONS.get(connection.state.get("reason"), REASONS["CHECK_FAILED"]),
        )
        # Webhook is checked before polling; no deletion or dropping updates.
        if await wait_for_telegram(bot, stop, verified=telegram_verified, retry_at=telegram_retry_at):

            async def polling():
                await dispatcher.start_polling(
                    bot,
                    allowed_updates=["message", "callback_query"],
                    close_bot_session=False,
                    polling_timeout=20,
                )
                requested = getattr(dispatcher, "_stop_signal", None)
                if requested is not None and requested.is_set():
                    stop.set()

            await supervisor.run("telegram-polling", polling, stop)
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
        configure_logging(
            [config.telegram_token, config.playerok_header, GiftSettings.load().api_hash],
            debug="--telegram-network-diagnostics" in sys.argv,
        )
        config.require_owner()
        with InstanceLock(str(config.database_path) + ".lock") as instance_lock:
            asyncio.run(
                run(
                    config,
                    network_diagnostics="--telegram-network-diagnostics" in sys.argv,
                    instance_lock=instance_lock,
                )
            )
    except KeyboardInterrupt:
        return 0
    except ProxyUnavailable:
        logger.error("Playerok Proxy не готов. Проверьте localVPN и порт1080. Прямое подключение запрещено.")
        return 1
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
