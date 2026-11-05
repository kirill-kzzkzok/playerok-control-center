"""One pinned Bot API route, with payload-free request evidence and no fallback."""

import logging
import secrets
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from urllib.parse import urlsplit

from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import GetFile
from aiohttp import ClientConnectionError, ClientSession, TraceConfig
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from app.localvpn import ProxyUnavailable, require_proxy
from app.security.logging import event
from app.telegram.errors import telegram_error
from app.telegram.system_tunnel import HappRoute
from app.telemetry import record

request_evidence = ContextVar("telegram_request_evidence", default=None)


class TelegramNetworkSession(AiohttpSession):
    def __init__(self, *, diagnostics=False, localvpn=False, system_tunnel=False, **kwargs):
        super().__init__(**kwargs)
        origin = urlsplit(self.api.base)
        self.origin = (origin.scheme, origin.hostname, origin.port)
        self.route = "Direct" if origin.hostname == "api.telegram.org" else "Worker"
        self.happ_guard = HappRoute() if system_tunnel else None
        self.happ_generation = None
        self.localvpn = localvpn
        if localvpn:
            if kwargs.get("proxy") != "socks5://127.0.0.1:1080" or origin.hostname != "api.telegram.org":
                raise ValueError("Invalid localVPN route")
            self.route = "SOCKS5 127.0.0.1:1080"
        self.diagnostics = diagnostics
        self.results = {}
        self._reported = {}

    async def create_session(self):
        # AiohttpSession has no trace_configs constructor argument. Retain its
        # connector/TLS settings, form builder, response parser and request logic.
        if self._should_reset_connector:
            await self.close()
        if self._session is None or self._session.closed:
            trace = TraceConfig()
            trace.on_request_start.append(self._request_start)
            trace.on_request_end.append(self._request_end)
            trace.on_request_redirect.append(self._redirect)
            trace.on_request_exception.append(self._request_exception)
            trace.on_request_headers_sent.append(self._headers_sent)
            trace.on_request_chunk_sent.append(self._chunk_sent)
            trace.on_connection_reuseconn.append(self._reuse)
            trace.on_connection_create_start.append(self._connecting)
            trace.on_connection_create_end.append(self._connected)
            connector_args = dict(self._connector_init)
            if self.happ_guard:
                connector_args["socket_factory"] = self.happ_guard.socket
            self._session = ClientSession(
                connector=self._connector_type(**connector_args),
                trace_configs=[trace],
            )
            self._should_reset_connector = False
        return self._session

    async def _request_start(self, session, context, params):
        if self.happ_guard:
            self.happ_guard.check()
        if self.localvpn:
            try:
                require_proxy()
            except ProxyUnavailable:
                raise ClientConnectionError("Playerok SOCKS service is not ready") from None
        state = request_evidence.get()
        origin = (params.url.scheme, params.url.host, params.url.port)
        expected_port = self.origin[2] or (443 if self.origin[0] == "https" else 80)
        if origin != (self.origin[0], self.origin[1], expected_port):
            if state is not None:
                state["blocked"] = "ROUTE_MISMATCH"
            raise ClientConnectionError("Bot API route mismatch")
        if state is not None:
            state["hostname"] = params.url.host

    async def _request_end(self, session, context, params):
        state = request_evidence.get()
        if state is not None:
            state["http"] = params.response.status

    async def _request_exception(self, session, context, params):
        state = request_evidence.get()
        if state is not None:
            state["network_error"] = type(params.exception).__name__

    async def _headers_sent(self, session, context, params):
        state = request_evidence.get()
        if state is not None:
            state["chunked"] = params.headers.get("Transfer-Encoding", "").lower() == "chunked"

    async def _chunk_sent(self, session, context, params):
        state = request_evidence.get()
        if state is not None:
            state["request_bytes"] = state.get("request_bytes", 0) + len(params.chunk)

    async def _reuse(self, session, context, params):
        state = request_evidence.get()
        if state is not None:
            state["connection"] = "reused"

    async def _connecting(self, session, context, params):
        state = request_evidence.get()
        if state is not None:
            state["connection"] = "new"

    async def _connected(self, session, context, params):
        state = request_evidence.get()
        if state is not None:
            state["connected"] = True

    async def _redirect(self, session, context, params):
        state = request_evidence.get()
        if state is not None:
            state["http"] = params.response.status
            state["blocked"] = "REDIRECT_BLOCKED"
        # Never forward the token/body to a redirect target, including Telegram.
        raise ClientConnectionError("Bot API redirects are disabled")

    def _begin(self, method):
        state = {
            "method": method,
            "request_id": secrets.token_hex(4),
            "hostname": None,
            "route": self.route,
            "started": datetime.now(UTC).isoformat(),
            "http": None,
        }
        return state, request_evidence.set(state), time.monotonic()

    def _finish(self, state, token, started, outcome):
        request_evidence.reset(token)
        state.update(seconds=round(time.monotonic() - started, 3), outcome=outcome)
        record("telegram", time.monotonic() - started)
        self.results[state["method"]] = state
        # Structured evidence remains available in results; no URL/payload or raw errors.
        logging.getLogger(__name__).debug(
            "Telegram request_id=%s method=%s route=%s host=%s seconds=%s http=%s connection=%s outcome=%s cause=%s retry_after=%s",
            state["request_id"],
            state["method"],
            self.route,
            state["hostname"] or self.origin[1],
            state["seconds"],
            state["http"],
            state.get("connection"),
            outcome,
            state.get("error_code", state.get("blocked", "-")),
            state.get("retry_after", 0),
        )
        code = state.get("error_code") or state.get("blocked") or outcome
        # Expected no-op and outer ACK cancellation aren't connection outages.
        if outcome in {"OK", "CANCELLED"} or code == "MESSAGE_NOT_MODIFIED":
            if outcome == "OK":
                for key in tuple(self._reported):
                    if key[0] != state["method"]:
                        continue
                    previous = self._reported.pop(key)
                    if key[1] in {
                        "NETWORK_ERROR",
                        "TIMEOUT",
                        "PROXY_UNAVAILABLE",
                        "DNS_ERROR",
                    }:
                        event("INFO", f"Telegram-соединение восстановлено · {previous[1]} неудачных попыток")
            return
        key = (state["method"], code)
        clock = time.monotonic()
        previous = self._reported.get(key)
        count = (previous[1] if previous else 0) + 1
        if previous is None or clock - previous[0] >= 30:
            if previous:
                text = f"Проблема Telegram сохраняется · {code} · {count} повторов"
            else:
                names = {
                    "PROXY_UNAVAILABLE": "Telegram-прокси недоступен",
                    "TIMEOUT": "Telegram не ответил вовремя",
                    "NETWORK_ERROR": "Ошибка соединения с Telegram",
                }
                text = names.get(code, "Не удалось выполнить запрос Telegram") + " · " + code
            names = {
                "getUpdates": "получение обновлений",
                "answerCallbackQuery": "подтверждение кнопки",
                "editMessageText": "обновление панели",
                "editMessageMedia": "обновление изображения",
                "sendMessage": "отправка сообщения",
                "sendPhoto": "отправка изображения",
                "downloadFile": "загрузка файла",
            }
            text += " · " + names.get(state["method"], "запрос API")
            if code == "RATE_LIMITED":
                text += f" · ожидание {state.get('retry_after', 0)} с"
            if code == "UNKNOWN_TELEGRAM_ERROR":
                text += " · диагностика " + state["request_id"]
            event("WARNING", text, level=logging.WARNING)
            self._reported[key] = (clock, count)
        else:
            self._reported[key] = (previous[0], count)

    async def make_request(self, bot, method, timeout=None):
        state, token, started = self._begin(method.__api_method__)
        outcome = "CANCELLED"
        try:
            if self.happ_guard:
                try:
                    generation = self.happ_guard.check()
                except OSError:
                    from app.telegram.errors import TelegramRequestNotStarted

                    await self.close()
                    raise TelegramRequestNotStarted(
                        method=method, message="Happ tunnel unavailable"
                    ) from None
                if self.happ_generation != generation:
                    await self.close()
                    self.happ_generation = generation
            if self.localvpn:
                try:
                    require_proxy()
                except ProxyUnavailable:
                    from app.telegram.errors import TelegramRequestNotStarted

                    raise TelegramRequestNotStarted(
                        method=method, message="Local SOCKS admission unavailable"
                    ) from None
            result = await super().make_request(bot, method, timeout)
            outcome = "OK"
            return result
        except (ProxyError, ProxyConnectionError, ProxyTimeoutError) as error:
            outcome = type(error).__name__
            state["error_code"] = "PROXY_UNAVAILABLE"
            raise TelegramNetworkError(method=method, message="Local SOCKS transport unavailable") from None
        except Exception as error:
            state["error_code"] = telegram_error(error)
            state["retry_after"] = getattr(error, "retry_after", 0)
            cause = state.get("network_error", "")
            if cause in {"ClientConnectorDNSError"}:
                state["error_code"] = "DNS_ERROR"
            elif cause in {"ConnectionTimeoutError", "SocketTimeoutError", "ServerTimeoutError"}:
                state["error_code"] = "TIMEOUT"
            if (
                isinstance(error, TelegramNetworkError)
                and "Playerok SOCKS service is not ready" in error.message
            ):
                state["error_code"] = "PROXY_UNAVAILABLE"
            outcome = type(error).__name__
            raise
        finally:
            self._finish(state, token, started, outcome)

    async def stream_content(self, url, **kwargs):
        state, token, started = self._begin("downloadFile")
        outcome = "CANCELLED"
        try:
            async for chunk in super().stream_content(url, **kwargs):
                yield chunk
            outcome = "OK"
        except (ProxyError, ProxyConnectionError, ProxyTimeoutError) as error:
            outcome = type(error).__name__
            state["error_code"] = "PROXY_UNAVAILABLE"
            raise TelegramNetworkError(
                method=GetFile(file_id="download"), message="Local SOCKS download unavailable"
            ) from None
        except Exception as error:
            state["error_code"] = telegram_error(error)
            state["retry_after"] = getattr(error, "retry_after", 0)
            cause = state.get("network_error", "")
            if cause in {"ClientConnectorDNSError"}:
                state["error_code"] = "DNS_ERROR"
            elif cause in {"ConnectionTimeoutError", "SocketTimeoutError", "ServerTimeoutError"}:
                state["error_code"] = "TIMEOUT"
            if (
                isinstance(error, TelegramNetworkError)
                and "Playerok SOCKS service is not ready" in error.message
            ):
                state["error_code"] = "PROXY_UNAVAILABLE"
            outcome = type(error).__name__
            raise
        finally:
            self._finish(state, token, started, outcome)
