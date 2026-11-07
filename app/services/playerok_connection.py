"""Fresh viewer verification using the existing API, binding and mutation gates."""

import asyncio
import time

from app.database.store import now
from app.playerok.adapter import APIError
from app.security.logging import event
from app.services.account_binding import AccountBindingError, bind_account

REASONS = {
    "AUTH_REJECTED": "Сервер отклонил авторизацию Playerok (HTTP401)",
    "SESSION_EXPIRED": "Авторизация Playerok истекла",
    "ACCOUNT_UNAVAILABLE": "Playerok подтвердил блокировку аккаунта",
    "ACCOUNT_MISMATCH": "Аккаунт не соответствует привязке базы; записи заблокированы",
    "UNAUTHORIZED_OR_CHALLENGE": "Playerok отказал в доступе (HTTP 403). Бот перепроверит подключение автоматически",
    "NETWORK_ERROR": "Временная сетевая ошибка; авторизация не признана недействительной",
    "CHECK_TIMEOUT": "Время проверки истекло; состояние авторизации неизвестно",
    "CHECK_FAILED": "Не удалось достоверно проверить аккаунт",
    "CHECK_IN_PROGRESS": "Проверка уже выполняется",
    "RATE_LIMITED": "Playerok ограничил частоту запросов",
    "HTTP_500": "Внутренняя ошибка Playerok (HTTP500); авторизация не проверена",
    "HTTP_502": "Временная ошибка шлюза Playerok (HTTP502)",
    "HTTP_503": "Playerok временно недоступен (HTTP503)",
    "HTTP_504": "Шлюз Playerok не дождался ответа (HTTP504)",
    "NON_JSON_RESPONSE": "Playerok вернул неожиданный ответ; требуется повторная проверка",
    "UPSTREAM_SCHEMA_ERROR": "Ответ Playerok не соответствует ожидаемой схеме",
    "GRAPHQL_ERROR_OR_INCOMPLETE": "Playerok не подтвердил полные данные аккаунта",
}

TEMPORARY_READ_FAILURES = {
    "NETWORK_ERROR",
    "CHECK_TIMEOUT",
    "RATE_LIMITED",
    "HTTP_500",
    "HTTP_502",
    "HTTP_503",
    "HTTP_504",
}


class PlayerokConnection:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.lock = asyncio.Lock()
        self.recheck = asyncio.Event()
        self.last_recheck_request = None
        self.last_started = None
        self.viewer = None
        self.invalidation_generation = 0
        self.viewer_task = None
        self.read_account_id = None
        self.desired_writes = bool(getattr(api.account, "writes_enabled", False))
        self.desired_automation = bool(getattr(api.account, "automation_enabled", False))
        self.state = {
            "status": "UNKNOWN",
            "reason": "CHECK_FAILED",
            "last_attempt": None,
            "last_success": store.setting("playerok:verification", {}).get("last_success"),
            "username": store.setting("playerok:verification", {}).get("username"),
        }
        self.disable()
        api.connection = self

    def disable(self):
        self.api.account.connection_verified = False
        self.api.account.writes_enabled = False
        self.api.account.automation_enabled = False

    @property
    def reads_allowed(self):
        return bool(
            self.read_account_id
            and self.api.account.id == self.read_account_id
            and self.store.setting("playerok:bound_account_id") == self.read_account_id
        )

    @property
    def awaiting_verification(self):
        return bool(
            self.reads_allowed
            and self.desired_writes
            and self.desired_automation
            and self.state.get("reason") in TEMPORARY_READ_FAILURES | {"CHECK_IN_PROGRESS"}
        )

    def invalidate(self, code, diagnostics=None):
        current = time.monotonic()
        if self.state["status"] == "CONNECTED" and (
            self.last_recheck_request is None or current - self.last_recheck_request >= 10
        ):
            self.last_recheck_request = current
            self.recheck.set()
        self.invalidation_generation += 1
        self.disable()
        if code not in TEMPORARY_READ_FAILURES:
            self.read_account_id = None
        if code == "UNAUTHORIZED_OR_CHALLENGE" and (diagnostics or {}).get("http_status") == 401:
            code = "AUTH_REJECTED"
        self.state.update(
            status="DISCONNECTED"
            if code in {"SESSION_EXPIRED", "ACCOUNT_UNAVAILABLE", "AUTH_REJECTED"}
            else "UNKNOWN",
            reason=code if code in REASONS else "CHECK_FAILED",
        )
        self.store.set_setting("playerok:verification", self.state)

    async def check(self, *, cooldown=5):
        if self.lock.locked() or (
            self.last_started is not None and time.monotonic() - self.last_started < cooldown
        ):
            return {**self.state, "busy": True}
        async with self.lock:
            previous = self.store.setting("playerok:verification", {})
            generation = self.invalidation_generation
            self.last_started = time.monotonic()
            self.disable()
            self.viewer = None
            self.state.update(status="UNKNOWN", last_attempt=now(), reason="CHECK_IN_PROGRESS")
            self.store.set_setting("playerok:verification", self.state)
            event("CHECK", "Проверка Playerok...", level=10)
            try:
                self.viewer_task = asyncio.current_task()
                try:
                    async with asyncio.timeout(25):
                        viewer = await self.api.call("get")
                finally:
                    self.viewer_task = None
                if generation != self.invalidation_generation:
                    return dict(self.state)
                ident = viewer.get("id")
                if not isinstance(ident, str) or not ident.strip() or ident != self.api.account.id:
                    raise APIError("CHECK_FAILED")
                if viewer.get("is_blocked") is True:
                    raise APIError("ACCOUNT_UNAVAILABLE")
                bind_account(self.store, ident)
            except AccountBindingError:
                self.read_account_id = None
                self.state.update(status="DISCONNECTED", reason="ACCOUNT_MISMATCH")
            except APIError as error:
                if generation != self.invalidation_generation:
                    return dict(self.state)
                if error.code not in TEMPORARY_READ_FAILURES:
                    self.read_account_id = None
                code = error.code if error.code in REASONS else "CHECK_FAILED"
                if (
                    error.code == "UNAUTHORIZED_OR_CHALLENGE"
                    and (error.diagnostics or {}).get("http_status") == 401
                ):
                    code = "AUTH_REJECTED"
                self.state.update(
                    status="DISCONNECTED"
                    if code in {"SESSION_EXPIRED", "ACCOUNT_UNAVAILABLE", "AUTH_REJECTED"}
                    else "UNKNOWN",
                    reason=code,
                )
            except TimeoutError:
                if generation != self.invalidation_generation:
                    return dict(self.state)
                self.state.update(status="UNKNOWN", reason="CHECK_TIMEOUT")
            except asyncio.CancelledError:
                self.last_started = time.monotonic()
                if generation == self.invalidation_generation:
                    self.state.update(status="UNKNOWN", reason="CHECK_FAILED")
                self.store.set_setting("playerok:verification", self.state)
                raise
            except Exception:  # noqa: BLE001 -- never expose raw account/request errors
                self.read_account_id = None
                self.state.update(status="UNKNOWN", reason="CHECK_FAILED")
            else:
                self.viewer = viewer
                balance = (viewer.get("profile") or {}).get("balance")
                if isinstance(balance, dict):
                    self.store.set_setting(
                        "playerok:balance:" + ident,
                        {**{key: balance.get(key) for key in ("value", "available", "frozen")}, "at": now()},
                    )
                username = (viewer.get("profile") or {}).get("username") or viewer.get("username") or ""
                self.state.update(
                    status="CONNECTED", reason=None, last_success=now(), username=str(username)[:80]
                )
                self.api.account.connection_verified = True
                self.read_account_id = ident
                self.api.account.verified_account_id = ident
                self.api.account.writes_enabled = self.desired_writes
                self.api.account.automation_enabled = self.desired_automation
            reason = self.state.get("reason")
            connected = self.state["status"] == "CONNECTED"
            level = 10
            if reason in TEMPORARY_READ_FAILURES:
                failures = min(3, previous.get("temporary_failures", 0) + 1)
                warned = bool(previous.get("temporary_warned"))
                level = 30 if failures == 3 and not warned else 10
                self.state.update(temporary_failures=failures, temporary_warned=warned or level == 30)
            else:
                self.state.pop("temporary_failures", None)
                self.state.pop("temporary_warned", None)
                if connected:
                    level = 20 if previous.get("temporary_warned") else 10
                elif reason != previous.get("reason"):
                    level = 30
            self.store.set_setting("playerok:verification", self.state)
            self.last_started = time.monotonic()
            self.store.set_setting("playerok:connection", "OK" if connected else reason)
            event(
                "SUCCESS" if connected else "WARNING",
                "Связь с Playerok восстановлена"
                if connected
                else REASONS.get(reason, REASONS["CHECK_FAILED"]),
                level=level,
            )
            return dict(self.state)

    async def run(self, stop):
        """Reverify identity after failure, never restore gates from cached success."""
        delay = 30
        stopped = asyncio.create_task(stop.wait())
        try:
            while not stop.is_set():
                requested = asyncio.create_task(self.recheck.wait())
                try:
                    done, _ = await asyncio.wait(
                        {stopped, requested}, timeout=delay, return_when=asyncio.FIRST_COMPLETED
                    )
                finally:
                    requested.cancel()
                    await asyncio.gather(requested, return_exceptions=True)
                if stop.is_set():
                    break
                self.recheck.clear()
                if self.state["status"] != "CONNECTED":
                    await self.check(cooldown=0 if requested in done else 5)
                    delay = 30 if self.state["status"] == "CONNECTED" else min(delay * 2, 300)
                else:
                    if self.last_started is None or time.monotonic() - self.last_started >= 300:
                        await self.check()
                    delay = 30
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
