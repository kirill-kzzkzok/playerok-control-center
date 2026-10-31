"""One pinned Bot API route, with payload-free request evidence and no fallback."""

import logging
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

request_evidence = ContextVar("telegram_request_evidence", default=None)


class TelegramNetworkSession(AiohttpSession):
    def __init__(self, *, diagnostics=False, localvpn=False, **kwargs):
        super().__init__(**kwargs)
        origin = urlsplit(self.api.base)
        self.origin = (origin.scheme, origin.hostname, origin.port)
        self.route = "Direct" if origin.hostname == "api.telegram.org" else "Worker"
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
            self._session = ClientSession(
                connector=self._connector_type(**self._connector_init),
                trace_configs=[trace],
            )
            self._should_reset_connector = False
        return self._session

    async def _request_start(self, session, context, params):
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
            "hostname": None,
            "route": self.route,
            "started": datetime.now(UTC).isoformat(),
            "http": None,
        }
        return state, request_evidence.set(state), time.monotonic()

    def _finish(self, state, token, started, outcome):
        request_evidence.reset(token)
        state.update(seconds=round(time.monotonic() - started, 3), outcome=outcome)
        self.results[state["method"]] = state
        previous = self._reported.get(state["method"])
        # Keep routine polling quiet. Log first success, changes/recovery, and
        # repeated failures at most once a minute unless diagnostics is explicit.
        key = (outcome, state.get("blocked"), state["http"])
        if (
            self.diagnostics
            or previous is None
            or previous[0] != key
            or (outcome != "OK" and time.monotonic() - previous[1] >= 60)
        ):
            event(
                "CHECK" if outcome == "OK" else "WARNING",
                f"Telegram {state['method']}: {self.route} — {outcome} "
                f"host={state['hostname'] or self.origin[1]} start={state['started']} "
                f"elapsed={state['seconds']}s http={state['http']}"
                + f" connection={state.get('connection')} chunked={state.get('chunked')}"
                + f" bytes={state.get('request_bytes', 0)} cause={state.get('network_error', '-')}"
                + (" " + state["blocked"] if state.get("blocked") else ""),
                level=logging.INFO if outcome == "OK" else logging.WARNING,
            )
            self._reported[state["method"]] = (key, time.monotonic())

    async def make_request(self, bot, method, timeout=None):
        state, token, started = self._begin(method.__api_method__)
        outcome = "CANCELLED"
        try:
            result = await super().make_request(bot, method, timeout)
            outcome = "OK"
            return result
        except (ProxyError, ProxyConnectionError, ProxyTimeoutError) as error:
            outcome = type(error).__name__
            raise TelegramNetworkError(method=method, message="Local SOCKS transport unavailable") from None
        except Exception as error:
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
            raise TelegramNetworkError(
                method=GetFile(file_id="download"), message="Local SOCKS download unavailable"
            ) from None
        except Exception as error:
            outcome = type(error).__name__
            raise
        finally:
            self._finish(state, token, started, outcome)
