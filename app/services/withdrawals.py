"""Read-only withdrawal estimates. This module has no withdrawal mutation."""

from copy import deepcopy
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from app.database.store import now
from app.playerok.adapter import APIError
from app.services.finance import FeeRule, calculate, checked_time, money, quote_fresh, sale_rule_from_api
from app.services.products_errors import ValidationError
from playerokapi.enums import TransactionProviderDirections

SOURCE = "https://playerok.com/wallet/withdraw"


def provider_record(provider):
    if not isinstance(provider, dict) or not isinstance(provider.get("id"), str):
        raise ValidationError("API не вернул идентификатор способа вывода")
    return {key: deepcopy(provider.get(key)) for key in ("id", "name", "fee", "min_fee_amount", "limits")}


@dataclass(frozen=True)
class WithdrawalEstimate:
    amount: Decimal
    fee: Decimal | None
    net: Decimal | None
    rule: FeeRule | None
    unknown: tuple[str, ...]
    minimum_amount: Decimal | None = None
    maximum_amount: Decimal | None = None
    provider_id: str | None = None


def estimate_withdrawal(amount, snapshot):
    amount = money(amount)
    provider = snapshot.get("provider") or {}
    if provider.get("id") == "LOCAL":
        return WithdrawalEstimate(
            amount, None, None, None, ("Баланс Playerok не является внешним способом вывода",)
        )
    if provider.get("id") not in {"SBP", "BANK_CARD_RU", "BANK_CARD", "YMONEY", "USDT"}:
        return WithdrawalEstimate(amount, None, None, None, ("Правила этого способа вывода не подтверждены",))
    if not quote_fresh(snapshot.get("checked_at"), max_age=3600):
        return WithdrawalEstimate(amount, None, None, None, ("Нужны свежие параметры способа вывода",))
    try:
        percent = Decimal(str(provider.get("fee")))
        minimum_fee = (
            money(provider["min_fee_amount"]) if provider.get("min_fee_amount") is not None else Decimal(0)
        )
        outgoing = (provider.get("limits") or {}).get("outgoing") or {}
        minimum = money(outgoing["min"])
        maximum = money(outgoing["max"])
        if not percent.is_finite() or not 0 <= percent <= 100 or maximum < minimum:
            raise ValueError
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return WithdrawalEstimate(amount, None, None, None, ("Не получены ставка или лимиты вывода",))
    if amount < minimum or amount > maximum:
        return WithdrawalEstimate(
            amount, None, None, None, (f"Сумма вывода вне диапазона {minimum}–{maximum} ₽",), minimum, maximum
        )
    extra = Decimal(0)
    if provider.get("id") == "USDT":
        exchange = snapshot.get("exchange_rate") or {}
        if exchange.get("pair") != "USDT_RUB" or not quote_fresh(exchange.get("checked_at"), max_age=60):
            return WithdrawalEstimate(
                amount, None, None, None, ("Нет свежего курса для доплаты 1 USDT",), minimum, maximum
            )
        try:
            extra = money(exchange["rate"])
            if extra <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return WithdrawalEstimate(
                amount, None, None, None, ("Не подтверждён курс USDT/RUB",), minimum, maximum
            )
    rule = FeeRule(
        percent,
        minimum_fee,
        SOURCE,
        snapshot["checked_at"],
        "OUT provider.fee/100; max(percentage + 1 USDT for USDT, minFeeAmount); settlement rounding unverified",
        estimated=True,
        extra_fee_amount=extra,
        net_floor_cents=True,
    )
    fee = rule.charge(amount)
    if fee > amount:
        return WithdrawalEstimate(
            amount, None, None, rule, ("Комиссия превышает сумму вывода",), minimum, maximum
        )
    return WithdrawalEstimate(amount, fee, amount - fee, rule, (), minimum, maximum, provider.get("id"))


async def withdrawal_methods(api):
    providers = await api.call("get_transaction_providers", direction=TransactionProviderDirections.OUT)
    return [provider_record(provider) for provider in providers if provider.get("id") != "LOCAL"]


async def withdrawal_snapshot(api, provider_id):
    providers = await withdrawal_methods(api)
    provider = next((value for value in providers if value["id"] == provider_id), None)
    if provider is None:
        raise ValueError("Способ вывода больше не доступен в Playerok")
    snapshot = {"provider": provider, "checked_at": now(), "source": SOURCE}
    if provider_id == "USDT":
        try:
            exchange = await api.call("get_exchange_rate")
            snapshot["exchange_rate"] = {**exchange, "checked_at": now(), "source": SOURCE}
        except APIError as error:
            snapshot["exchange_error"] = error.code
    return snapshot


def draft_calculation(payload, withdrawal=None, placement=None):
    checked_at = payload.get("fee_checked_at")
    rule = (
        sale_rule_from_api(payload.get("fee_multiplier"), checked_at)
        if quote_fresh(checked_at, max_age=3600)
        else None
    )
    base = calculate(payload["price"], platform=rule, placement=placement, cost=payload.get("cost"))
    snapshot = payload.get("withdrawal") if withdrawal is None else withdrawal
    if not snapshot or base.after_platform is None:
        return base
    quote = estimate_withdrawal(base.after_platform, snapshot)
    if quote.unknown:
        return replace(
            base,
            unknown=tuple(reason for reason in base.unknown if reason != "комиссия выбранного способа вывода")
            + quote.unknown,
        )
    return calculate(
        payload["price"], platform=rule, withdrawal=quote.rule, placement=placement, cost=payload.get("cost")
    )


def format_withdrawal(quote, name):
    lines = [f"💰 Вывод · {name}", f"Сумма до вывода: {quote.amount} ₽"]
    if quote.unknown:
        lines.extend(["Точный расчёт недоступен", *quote.unknown])
    else:
        lines.extend(
            [
                "Оценка · фактическое округление ещё не сверено",
                f"Комиссия: {quote.fee} ₽",
                f"К получению: {quote.net} ₽"
                + (" (рублёвый эквивалент USDT)" if quote.provider_id == "USDT" else ""),
            ]
        )
    if quote.rule:
        lines.append("Данные комиссии: " + checked_time(quote.rule.checked_at))
    return "\n".join(lines)
