"""Reuse pinned PlayerokAPI operations with a bounded, non-retrying transport."""

import asyncio
import json
import re
import threading
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from enum import Enum
from urllib.parse import urlsplit

import httpx

from playerokapi.account import Account
from playerokapi.misc import PERSISTED_QUERIES, QUERIES

ACCEPT_AGREEMENT_QUERY = "mutation acceptGameCategoryAgreement($input: AcceptGameCategoryAgreementInput!) { acceptGameCategoryAgreement(input: $input) { id } }"


class APIError(RuntimeError):
    def __init__(self, code, uncertain=False, retry_after=None):
        self.code, self.uncertain, self.retry_after = code, uncertain, retry_after
        super().__init__(code)  # Never include upstream body/URL/cookies.


def plain(value):
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    if hasattr(value, "__dict__"):
        return {k: plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return value


def parse_cookies(header):
    header = header.removeprefix("Cookie:").removeprefix("cookie:").strip()
    cookies = {}
    for part in header.split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            if key:
                cookies[key] = value
    if not cookies or "token" not in cookies:
        raise APIError("PLAYEROK_COOKIE_TOKEN_MISSING")
    return cookies


class SafeAccount(Account):
    def __new__(cls, *args, **kwargs):
        return object.__new__(cls)

    def __init__(self, header, writes_enabled=False, client=None):
        # Avoid upstream singleton, temporary certificate, TLS fingerprint spoofing and retries.
        self.cookies = parse_cookies(header)
        self.base_url = "https://playerok.com"
        self.user_agent = "PlayerokControlCenter/0.1"
        self.id = self.username = self.profile = None
        self._is_initiated = False
        self.writes_enabled = writes_enabled
        self.client = client or httpx.Client(timeout=20, follow_redirects=False, verify=True)
        self._lock = threading.RLock()
        self._permitted = None
        self._permit_thread = None
        self._last_request = 0.0

    @contextmanager
    def permit(self, operation):
        with self._lock:
            if not self.writes_enabled:
                raise APIError("WRITES_DISABLED")
            self._permitted = operation
            self._permit_thread = threading.get_ident()
            try:
                yield
            finally:
                self._permitted = None
                self._permit_thread = None

    def request(self, method, url, headers, payload=None, files=None, pass_304=False):
        if url != "https://playerok.com/graphql" or method not in {"get", "post"}:
            raise APIError("ENDPOINT_FORBIDDEN")
        operation_payload = (
            json.loads(payload["operations"]) if payload and "operations" in payload else payload
        )
        operation = (operation_payload or {}).get("operationName", "")
        read = operation in {*PERSISTED_QUERIES, "viewer", "getExchangeRates"}
        query = (operation_payload or {}).get("query")
        if read and query and not re.match(r"^\s*(?:query\b|\{)", query):
            raise APIError("MUTATION_DISGUISED_AS_QUERY")
        if method == "get" and not read:
            raise APIError("MUTATION_GET_FORBIDDEN")
        if not read:
            if operation not in {
                "createItem",
                "updateItem",
                "removeItem",
                "publishItem",
                "createChatMessage",
                "markChatAsRead",
                "updateDeal",
                "acceptGameCategoryAgreement",
            }:
                raise APIError("OPERATION_FORBIDDEN")
            if (
                not self.writes_enabled
                or self._permitted != operation
                or self._permit_thread != threading.get_ident()
            ):
                raise APIError("WRITE_NOT_AUTHORIZED")
            trusted = (
                ACCEPT_AGREEMENT_QUERY
                if operation == "acceptGameCategoryAgreement"
                else QUERIES.get(operation)
            )
            if not trusted or query != trusted:
                raise APIError("WRITE_QUERY_NOT_AUTHORIZED")
        request_headers = {
            "accept": "application/json",
            "origin": self.base_url,
            "referer": self.base_url + "/",
            "user-agent": self.user_agent,
            "apollo-require-preflight": "true",
            "x-apollo-operation-name": operation,
            "cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
        }
        with self._lock:
            if not read and (self._permitted != operation or self._permit_thread != threading.get_ident()):
                raise APIError("WRITE_NOT_AUTHORIZED")
            time.sleep(max(0, 0.3 - (time.monotonic() - self._last_request)))
            try:
                if method == "get":
                    response = self.client.get(url, params=payload, headers=request_headers)
                elif files:
                    response = self.client.post(url, data=payload, files=files, headers=request_headers)
                elif payload and "operations" in payload:
                    response = self.client.post(url, json=operation_payload, headers=request_headers)
                else:
                    response = self.client.post(url, json=payload, headers=request_headers)
            except httpx.HTTPError:
                raise APIError("NETWORK_ERROR", uncertain=not read) from None
            finally:
                self._last_request = time.monotonic()
                for attachment in (files or {}).values():
                    if hasattr(attachment[1], "close"):
                        attachment[1].close()
        if response.status_code in (401, 403):
            raise APIError("UNAUTHORIZED_OR_CHALLENGE", uncertain=not read)
        if response.status_code == 429:
            raise APIError(
                "RATE_LIMITED", uncertain=not read, retry_after=response.headers.get("retry-after")
            )
        if response.status_code != 200:
            raise APIError(f"HTTP_{response.status_code}", uncertain=not read)
        try:
            body = response.json()
        except ValueError:
            raise APIError("NON_JSON_RESPONSE", uncertain=not read) from None
        if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
            raise APIError("GRAPHQL_ERROR_OR_INCOMPLETE", uncertain=not read)
        if operation == "transactionProviders":
            providers = body["data"].get("transactionProviders")
            required = {"id", "fee", "minFeeAmount", "limits"}
            if not isinstance(providers, list) or any(
                not isinstance(provider, dict) or not required <= provider.keys() for provider in providers
            ):
                raise APIError("UPSTREAM_FINANCIAL_DATA_INCOMPLETE")
        if operation == "viewer" and not body["data"].get("viewer"):
            raise APIError("SESSION_EXPIRED")
        return response

    def accept_game_category_agreement(self, agreement_id):
        if not self.id or not self._is_initiated:
            raise APIError("SESSION_NOT_INITIALIZED")
        operation = "acceptGameCategoryAgreement"
        payload = {
            "operationName": operation,
            "query": ACCEPT_AGREEMENT_QUERY,
            "variables": {"input": {"agreementId": agreement_id, "userId": self.id}},
        }
        result = self.request("post", self.base_url + "/graphql", {}, payload).json()["data"].get(operation)
        if not isinstance(result, dict) or result.get("id") != agreement_id:
            raise APIError("AGREEMENT_RESULT_UNKNOWN", uncertain=True)
        return result

    def get_exchange_rate(self):
        payload = {
            "operationName": "getExchangeRates",
            "query": "query getExchangeRates($pair: ExchangePair!) { getExchangeRates(pair: $pair) }",
            "variables": json.dumps({"pair": "USDT_RUB"}),
        }
        result = (
            self.request("get", self.base_url + "/graphql", {}, payload)
            .json()["data"]
            .get("getExchangeRates")
        )
        try:
            rate = Decimal(str(result))
            if not rate.is_finite() or rate <= 0:
                raise ValueError
        except (ValueError, InvalidOperation):
            raise APIError("EXCHANGE_RATE_UNAVAILABLE") from None
        return {"pair": "USDT_RUB", "rate": str(rate)}

    def close(self):
        self.client.close()


class Playerok:
    def __init__(self, account):
        self.account = account
        self.lock = asyncio.Lock()

    async def call(self, method, **kwargs):
        if method not in {
            "get",
            "get_user",
            "get_games",
            "get_game",
            "get_game_category",
            "get_game_category_obtaining_types",
            "get_game_category_data_fields",
            "get_game_category_agreements",
            "get_item",
            "get_my_items",
            "get_items",
            "get_item_priority_statuses",
            "get_deals",
            "get_deal",
            "get_chats",
            "get_chat",
            "get_chat_messages",
            "get_transaction_providers",
            "get_transactions",
            "get_exchange_rate",
        }:
            raise APIError("READ_METHOD_FORBIDDEN")
        async with self.lock:
            try:
                result = await asyncio.to_thread(getattr(self.account, method), **kwargs)
                # get() returns Account itself; don't expose cookies/client in plain().
                if method == "get":
                    viewer = {
                        k: plain(getattr(result, k, None))
                        for k in (
                            "id",
                            "username",
                            "profile",
                            "created_at",
                            "can_publish_items",
                            "is_blocked",
                        )
                    }
                    if not viewer["id"]:
                        raise APIError("SESSION_EXPIRED")
                    return viewer
                normalized = plain(result)
                expected = (
                    list if method in {"get_item_priority_statuses", "get_transaction_providers"} else dict
                )
                if not isinstance(normalized, expected):
                    raise APIError("UPSTREAM_INCOMPLETE_RESPONSE")
                if expected is list and any(
                    not isinstance(entry, dict) or not entry.get("id") for entry in normalized
                ):
                    raise APIError("UPSTREAM_INCOMPLETE_RESPONSE")
                if method in {
                    "get_user",
                    "get_game",
                    "get_game_category",
                    "get_item",
                    "get_deal",
                    "get_chat",
                } and not normalized.get("id"):
                    raise APIError("UPSTREAM_INCOMPLETE_RESPONSE")
                return normalized
            except APIError:
                raise
            except (KeyError, TypeError, AttributeError, ValueError):
                raise APIError("UPSTREAM_SCHEMA_ERROR") from None

    async def pages(self, method, collection, **kwargs):
        cursor, seen = None, set()
        while True:
            page = await self.call(method, after_cursor=cursor, **kwargs)
            if not isinstance(page, dict) or collection not in page or not isinstance(page[collection], list):
                raise APIError("INCOMPLETE_PAGE")
            if any(not isinstance(entry, dict) or not entry.get("id") for entry in page[collection]):
                raise APIError("INCOMPLETE_PAGE_ENTRY")
            info = page.get("page_info")
            if not isinstance(info, dict) or not isinstance(info.get("has_next_page"), bool):
                raise APIError("INCOMPLETE_PAGE_INFO")
            next_cursor = info.get("end_cursor")
            if info["has_next_page"] and (
                not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen
            ):
                raise APIError("INVALID_PAGINATION_CURSOR")
            yield page[collection]
            if not info["has_next_page"]:
                break
            cursor = next_cursor
            seen.add(cursor)


def safe_media_url(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and (parsed.hostname == "playerok.com" or (parsed.hostname or "").endswith(".playerok.com"))
        and not parsed.username
    )
