"""Official reversible listing pause; no publication, tariff or deletion fallback."""

import re
import threading
import time

import httpx

from app.playerok.adapter import APIError


def visibility_request(account, ident, operation):
    if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", ident):
        raise APIError("VISIBILITY_ITEM_INVALID")
    paths = {"discontinueItem": "discontinue", "republishItem": "republish"}
    if operation not in paths:
        raise APIError("VISIBILITY_OPERATION_FORBIDDEN")
    with account._lock:
        if (
            not account.writes_enabled
            or account._permitted != operation
            or account._permit_thread != threading.get_ident()
        ):
            raise APIError("WRITE_NOT_AUTHORIZED")
        time.sleep(max(0, 0.3 - (time.monotonic() - account._last_request)))
        if account._dispatch_guard:
            account._dispatch_guard()
        if (
            not account.writes_enabled
            or account._permitted != operation
            or account._permit_thread != threading.get_ident()
            or getattr(account, "connection_verified", False) is not True
            or getattr(account, "verified_account_id", None) != account.id
            or not account.id
        ):
            raise APIError("CONNECTION_NOT_VERIFIED")
        if not account._dispatch_start:
            raise APIError("VISIBILITY_INTENT_REQUIRED")
        account._dispatch_start()
        try:
            response = account.client.post(
                f"https://playerok.com/rest-api/public/item/{ident}/{paths[operation]}",
                headers={
                    "cookie": "; ".join(f"{k}={v}" for k, v in account.cookies.items()),
                    "origin": account.base_url,
                    "referer": account.base_url + "/",
                    "user-agent": account.user_agent,
                    "accept": "application/json",
                },
                follow_redirects=False,
            )
        except httpx.HTTPError:
            raise APIError("VISIBILITY_RESULT_UNKNOWN", uncertain=True) from None
        finally:
            account._last_request = time.monotonic()
    if response.status_code not in {200, 201, 204}:
        raise APIError("VISIBILITY_RESULT_UNKNOWN", uncertain=True)
    return {"accepted": True}
