"""Pure Decimal calculations. A rate is usable only with explicit provenance."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation


def money(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Некорректная денежная сумма") from None
    if not result.is_finite() or result < 0:
        raise ValueError("Сумма должна быть конечной и неотрицательной")
    return result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FeeRule:
    percent: Decimal
    minimum: Decimal
    source: str
    checked_at: str
    basis: str
    estimated: bool = True
    rounding: str = ROUND_HALF_UP
    net_floor_rubles: bool = False

    def __post_init__(self):
        percent = Decimal(str(self.percent))
        if not percent.is_finite() or not 0 <= percent <= 100 or money(self.minimum) < 0:
            raise ValueError("Недопустимая ставка")
        if not self.source or not self.checked_at or not self.basis:
            raise ValueError("Для ставки требуется источник, дата и основание")

    def charge(self, amount):
        if self.net_floor_rubles:
            amount = money(amount)
            net = (amount * (1 - Decimal(str(self.percent)) / 100)).quantize(Decimal(1), rounding=ROUND_FLOOR)
            return amount - net
        charge = max(money(amount) * Decimal(str(self.percent)) / 100, money(self.minimum))
        return charge.quantize(Decimal("0.01"), rounding=self.rounding)


def sale_rule_from_api(multiplier, checked_at, source="https://playerok.com/sell"):
    """Website module 96644 uses floor(price * (1 - feeMultiplier)).

    This is a frontend estimate, not a verified settlement guarantee.
    No default multiplier: missing data remains unknown.
    """
    if multiplier is None:
        return None
    ratio = Decimal(str(multiplier))
    if not ratio.is_finite() or not 0 <= ratio <= 1:
        raise ValueError("Множитель комиссии вне диапазона 0–1")
    return FeeRule(
        ratio * 100,
        Decimal(0),
        source,
        checked_at,
        "API feeMultiplier; public web module 96644 net ROUND_FLOOR to whole RUB",
        estimated=True,
        net_floor_rubles=True,
    )


def selected_fee_multiplier(category_multiplier, obtaining_multiplier):
    """Public sell model eR: selected obtaining type overrides category rate.

    The site's global fallback is deliberately not used when API values are absent.
    """
    value = obtaining_multiplier if obtaining_multiplier is not None else category_multiplier
    if value is None:
        return None
    ratio = Decimal(str(value))
    if not ratio.is_finite() or not 0 <= ratio <= 1:
        raise ValueError("Множитель комиссии вне диапазона 0–1")
    return str(ratio)


@dataclass(frozen=True)
class Calculation:
    sale: Decimal
    platform_fee: Decimal | None
    after_platform: Decimal | None
    withdrawal_fee: Decimal | None
    after_withdrawal: Decimal | None
    placement_expense: Decimal | None
    cost: Decimal | None
    expected: Decimal | None
    profit: Decimal | None
    unknown: tuple[str, ...]
    estimated: bool
    sources: tuple[FeeRule, ...]


def calculate(sale, platform=None, withdrawal=None, placement=None, cost=None):
    sale = money(sale)
    unknown = []
    fee = platform.charge(sale) if platform else None
    if fee is None:
        unknown.append("комиссия продажи")
    after_platform = sale - fee if fee is not None else None
    withdrawal_fee = (
        withdrawal.charge(after_platform)
        if withdrawal and after_platform is not None and after_platform >= 0
        else None
    )
    if withdrawal is None:
        unknown.append("комиссия выбранного способа вывода")
    after_withdrawal = after_platform - withdrawal_fee if withdrawal_fee is not None else None
    placement = money(placement) if placement is not None else None
    if placement is None:
        unknown.append("стоимость размещения")
    cost = money(cost) if cost is not None else None
    expected = (
        after_withdrawal - placement if after_withdrawal is not None and placement is not None else None
    )
    profit = expected - cost if expected is not None and cost is not None else None
    rules = tuple(rule for rule in (platform, withdrawal) if rule is not None)
    return Calculation(
        sale,
        fee,
        after_platform,
        withdrawal_fee,
        after_withdrawal,
        placement,
        cost,
        expected,
        profit,
        tuple(unknown),
        any(rule.estimated for rule in rules),
        rules,
    )


def discount_percent(old, new):
    old, new = money(old), money(new)
    if old <= 0 or new >= old:
        raise ValueError("Старая цена должна превышать новую")
    return ((old - new) / old * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def quote_fresh(checked_at, max_age=60):
    try:
        checked = datetime.fromisoformat(checked_at)
        if checked.tzinfo is None:
            return False
        age = (datetime.now(UTC) - checked).total_seconds()
        return 0 <= age <= max_age
    except (ValueError, TypeError):
        return False


def format_calculation(result):
    title = (
        "Точный расчёт недоступен"
        if result.unknown
        else "Оценка"
        if result.estimated
        else "Расчёт по подтверждённым условиям"
    )
    lines = [f"💰 {title}", f"Цена продажи: {result.sale} ₽"]
    if result.estimated:
        lines.append("Известные составляющие рассчитаны как оценка.")
    for label, value in [
        ("Комиссия площадки", result.platform_fee),
        ("После площадки", result.after_platform),
        ("Комиссия вывода", result.withdrawal_fee),
        ("После вывода", result.after_withdrawal),
        ("Разовое размещение", result.placement_expense),
        ("Себестоимость", result.cost),
        ("Ожидаемая сумма после расходов", result.expected),
        ("Прибыль", result.profit),
    ]:
        if value is not None:
            lines.append(f"{label}: {value} ₽")
    if result.unknown:
        lines.append("Неизвестно: " + ", ".join(result.unknown))
    return "\n".join(lines)
