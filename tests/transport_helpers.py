"""Native permit helper for isolated HTTP contract tests; never installed with the app."""

import asyncio

import httpx


async def mutate(account, operation, method, **kwargs):
    if not isinstance(account.client._transport, httpx.MockTransport) or any(
        transport is not None and not isinstance(transport, httpx.MockTransport)
        for transport in account.client._mounts.values()
    ):
        raise TypeError("TEST_HELPER_REQUIRES_MOCK_TRANSPORT")

    def run():
        with account.permit(operation):
            return getattr(account, method)(**kwargs)

    return await asyncio.to_thread(run)
