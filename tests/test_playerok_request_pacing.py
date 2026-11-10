"""Bounded request admission jitter does not repeat a failed request."""

import random

import httpx
import pytest

from app.playerok.adapter import APIError, SafeAccount


@pytest.mark.parametrize("gap", [0.3, 0.5])
def test_request_jitter_is_bounded_and_network_failure_is_not_retried(monkeypatch, gap):
    from app.playerok import adapter

    pauses, requests = [], []
    monkeypatch.setattr(
        random,
        "uniform",
        lambda low, high: gap if (low, high) == (0.3, 0.5) else pytest.fail("Unexpected jitter bounds"),
    )
    monkeypatch.setattr(adapter.time, "sleep", pauses.append)
    monkeypatch.setattr(adapter.time, "monotonic", lambda: 10.0)

    def respond(request):
        requests.append(request)
        raise httpx.ConnectError("simulated", request=request)

    account = SafeAccount("token=mock-only", client=httpx.Client(transport=httpx.MockTransport(respond)))
    account._last_request = 10.0
    try:
        with pytest.raises(APIError, match="NETWORK_ERROR"):
            account.request("get", "https://playerok.com/graphql", {}, {"operationName": "viewer"})
        assert pauses == [gap]
        assert len(requests) == 1
    finally:
        account.close()
