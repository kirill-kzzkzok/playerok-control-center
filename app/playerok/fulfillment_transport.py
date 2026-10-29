import re
import threading
import time

import httpx

from app.playerok.adapter import APIError

REST_BASE = "https://playerok.com/rest-api/public"
IDENT = r"[A-Za-z0-9_-]{1,100}"


def fulfillment_request(account, method, path, body=None):
    read = method == "get"
    allowed = (
        (
            path == "/official-providers"
            or re.fullmatch(rf"/deals/{IDENT}/fulfillment-report/(requirement|journal-entry)", path)
        )
        if read
        else method == "post" and re.fullmatch(rf"/deals/{IDENT}/fulfillment-report", path)
    )
    if not allowed:
        raise APIError("FULFILLMENT_ENDPOINT_FORBIDDEN")
    if read and body is not None:
        raise APIError("FULFILLMENT_READ_BODY_FORBIDDEN")
    if not read and (
        not isinstance(body, dict)
        or body.get("state") not in {"draft", "filled"}
        or set(body)
        != (
            {"state"}
            if body.get("state") == "draft"
            else {"state", "workerName", "telegram", "providerId", "supplierOrderNumber", "price", "currency"}
        )
    ):
        raise APIError("FULFILLMENT_BODY_FORBIDDEN")
    if not read and (
        not account.writes_enabled
        or account._permitted != "submitFulfillmentReport"
        or account._permit_thread != threading.get_ident()
    ):
        raise APIError("WRITE_NOT_AUTHORIZED")
    with account._lock:
        if not read and (
            not account.writes_enabled
            or account._permitted != "submitFulfillmentReport"
            or account._permit_thread != threading.get_ident()
        ):
            raise APIError("WRITE_NOT_AUTHORIZED")
        headers = {
            "cookie": "; ".join(f"{key}={value}" for key, value in account.cookies.items()),
            "origin": account.base_url,
            "referer": account.base_url + "/",
            "user-agent": account.user_agent,
            "accept": "application/json",
        }
        time.sleep(max(0, 0.3 - (time.monotonic() - account._last_request)))
        try:
            response = account.client.request(
                method, REST_BASE + path, headers=headers, json=body, follow_redirects=False
            )
        except httpx.HTTPError:
            raise APIError("FULFILLMENT_NETWORK_ERROR", uncertain=not read) from None
        finally:
            account._last_request = time.monotonic()
    if read and response.status_code == 404 and path.endswith("/journal-entry"):
        return {"state": "absent"}
    if response.status_code not in {200, 201, 204}:
        raise APIError(
            f"FULFILLMENT_HTTP_{response.status_code}",
            uncertain=not read,
            retry_after=response.headers.get("retry-after"),
        )
    if not read:
        return {"accepted": True}  # Acceptance alone never confirms SENT or a stored report.
    try:
        result = response.json()
    except ValueError:
        raise APIError("FULFILLMENT_RESPONSE_NOT_JSON") from None
    if not isinstance(result, dict):
        raise APIError("FULFILLMENT_RESPONSE_INCOMPLETE")
    return result
