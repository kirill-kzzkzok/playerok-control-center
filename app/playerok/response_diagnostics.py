"""Bounded, credential-redacted response facts; never store successful account bodies."""

import hashlib
import re
from urllib.parse import urlsplit


def redacted(value, secrets=(), limit=800):
    text = str(value)
    for secret in sorted((value for value in secrets if value), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[REDACTED_JWT]", text)
    text = re.sub(r"(?i)(cookie|authorization|token)\s*[=:]\s*[^\s;,<]+", r"\1=[REDACTED]", text)
    return text[:limit]


def response_facts(response, operation, secrets=(), *, include_messages=True):
    request = response.request
    url = urlsplit(str(request.url))
    result = {
        "method": request.method,
        "endpoint": f"{url.scheme}://{url.hostname}{url.path}",
        "operation": operation,
        "http_status": response.status_code,
        "content_type": redacted(response.headers.get("content-type", ""), secrets, 120),
        "server": redacted(response.headers.get("server", ""), secrets, 120),
        "challenge_header": redacted(response.headers.get("cf-mitigated", ""), secrets, 120),
        "response_bytes": len(response.content),
        "response_sha256": hashlib.sha256(response.content).hexdigest(),
    }
    try:
        body = response.json()
    except ValueError:
        if not include_messages:
            result["response_excerpt"] = "Body omitted for mutation privacy"
            return result
        text = response.content[:65_536].decode("utf-8", errors="replace")
        text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        result["response_excerpt"] = redacted(re.sub(r"\s+", " ", text).strip(), secrets)
        return result
    result["json_object"] = isinstance(body, dict)
    if not isinstance(body, dict):
        return result
    result["data_object"] = isinstance(body.get("data"), dict)
    errors = body.get("errors")
    if isinstance(errors, list):
        result["graphql_error_count"] = len(errors)
        result["errors"] = [
            {
                "message": redacted(error.get("message", ""), secrets)
                if include_messages
                else "Body omitted for mutation privacy",
                "code": redacted((error.get("extensions") or {}).get("code", ""), secrets, 120),
            }
            for error in errors[:5]
            if isinstance(error, dict) and isinstance(error.get("extensions") or {}, dict)
        ]
    if response.status_code >= 400 and include_messages:
        for key in ("message", "detail", "error"):
            if isinstance(body.get(key), str):
                result[key] = redacted(body[key], secrets)
    return result
