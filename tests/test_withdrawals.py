import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.database.store import now
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.services.finance import format_calculation, money
from app.services.withdrawals import draft_calculation, estimate_withdrawal, withdrawal_snapshot


def snapshot(ident="SBP", fee=6, minimum_fee=60, minimum=500, maximum=79000):
    """Synthetic fixtures, never application defaults."""
    return {
        "checked_at": now(),
        "source": "test fixture",
        "provider": {
            "id": ident,
            "name": ident,
            "fee": fee,
            "min_fee_amount": minimum_fee,
            "limits": {"outgoing": {"min": minimum, "max": maximum}},
        },
    }


def test_percentage_minimum_and_limits_are_separate():
    value = snapshot()
    assert estimate_withdrawal(500, value).fee == 60
    quote = estimate_withdrawal(2000, value)
    assert quote.fee == 120 and quote.net == 1880
    assert quote.rule.estimated and quote.rule.source
    for amount in [499, 79001]:
        quote = estimate_withdrawal(amount, value)
        assert quote.net is None and quote.fee is None and quote.unknown


def test_usdt_extra_one_usdt_uses_fresh_live_exchange_snapshot():
    value = snapshot("USDT", 4, None, 1000, 700000)
    assert estimate_withdrawal(2000, value).net is None
    value["exchange_rate"] = {"pair": "USDT_RUB", "rate": "100.00", "checked_at": now()}
    quote = estimate_withdrawal(2000, value)
    assert quote.fee == 180 and quote.net == 1820
    value["exchange_rate"]["checked_at"] = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    assert estimate_withdrawal(2000, value).net is None


def test_fee_minimum_is_applied_after_extra_not_twice():
    value = snapshot("USDT", 4, 200, 1000, 700000)
    value["exchange_rate"] = {"pair": "USDT_RUB", "rate": "100", "checked_at": now()}
    assert estimate_withdrawal(2000, value).fee == 200  # max(80+100,200), not 200+100


@pytest.mark.parametrize(
    "patch",
    [{"fee": None}, {"fee": "NaN"}, {"fee": 101}, {"limits": {}}, {"id": "LOCAL"}, {"id": "NEW_UNKNOWN"}],
)
def test_missing_or_unsupported_conditions_cannot_generate_fake_net(patch):
    value = snapshot()
    value["provider"].update(patch)
    quote = estimate_withdrawal(1000, value)
    assert quote.net is None and quote.unknown


def test_stale_provider_snapshot_and_impossible_fee():
    value = snapshot()
    value["checked_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    assert estimate_withdrawal(1000, value).net is None
    value = snapshot(minimum=1, minimum_fee=100)
    assert estimate_withdrawal(50, value).net is None


def test_sale_calculator_keeps_cost_placement_and_withdrawal_distinct():
    payload = {
        "price": 2000,
        "fee_multiplier": "0.1",
        "fee_checked_at": now(),
        "cost": "500",
        "withdrawal": snapshot(),
    }
    result = draft_calculation(payload, placement=20)
    assert result.after_platform == 1800
    assert result.withdrawal_fee == 108
    assert result.after_withdrawal == 1692
    assert result.expected == 1672 and result.profit == 1172
    assert "Оценка" in format_calculation(result)
    payload.pop("cost")
    assert draft_calculation(payload, placement=20).profit is None
    assert draft_calculation(payload).expected is None  # publication price is unknown


def test_single_sale_below_withdrawal_minimum_does_not_promise_receipt():
    result = draft_calculation(
        {"price": 500, "fee_multiplier": "0.1", "fee_checked_at": now(), "withdrawal": snapshot()},
        placement=0,
    )
    assert result.after_platform == 450
    assert result.after_withdrawal is None and result.expected is None
    assert "диапазона" in " ".join(result.unknown)


def test_huge_cost_is_rejected_without_decimal_context_crash():
    with pytest.raises(ValueError):
        money("1e9999")


async def test_exchange_rate_read_only_contract_and_validation():
    def respond(request):
        assert request.method == "GET"
        assert json.loads(request.url.params["variables"]) == {"pair": "USDT_RUB"}
        assert "query getExchangeRates" in request.url.params["query"]
        return httpx.Response(200, json={"data": {"getExchangeRates": "102.45"}})

    account = SafeAccount("token=private", client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        assert await Playerok(account).call("get_exchange_rate") == {"pair": "USDT_RUB", "rate": "102.45"}
    finally:
        account.close()


async def test_unavailable_exchange_rate_preserves_real_provider_without_fallback():
    class API:
        async def call(self, method, **kwargs):
            if method == "get_transaction_providers":
                return [snapshot("USDT")["provider"]]
            raise APIError("EXCHANGE_RATE_UNAVAILABLE")

    value = await withdrawal_snapshot(API(), "USDT")
    assert value["provider"]["id"] == "USDT"
    assert value["exchange_error"] == "EXCHANGE_RATE_UNAVAILABLE"
    assert "exchange_rate" not in value
    assert estimate_withdrawal(1000, value).net is None


@pytest.mark.parametrize("missing", ["fee", "minFeeAmount", "limits"])
def test_missing_raw_financial_field_is_not_treated_as_explicit_null(missing):
    provider = {
        "id": "SBP",
        "fee": 6,
        "minFeeAmount": None,
        "limits": {"outgoing": {"min": 500, "max": 79000}},
    }
    del provider[missing]

    def respond(request):
        return httpx.Response(200, json={"data": {"transactionProviders": [provider]}})

    account = SafeAccount("token=private", client=httpx.Client(transport=httpx.MockTransport(respond)))
    try:
        with pytest.raises(APIError, match="UPSTREAM_FINANCIAL_DATA_INCOMPLETE"):
            account.request(
                "get", account.base_url + "/graphql", {}, {"operationName": "transactionProviders"}
            )
    finally:
        account.close()
