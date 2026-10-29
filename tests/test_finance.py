from decimal import Decimal

import pytest

from app.services.finance import FeeRule, calculate, discount_percent, format_calculation, money, quote_fresh


def rule(percent, minimum=0, estimated=False):
    return FeeRule(
        Decimal(str(percent)),
        Decimal(str(minimum)),
        "test fixture (not real rates)",
        "2026-10-29T00:00:00+00:00",
        "synthetic contract",
        estimated,
    )


def test_decimal_and_distinct_costs():
    result = calculate("100.05", rule(10), rule(5), placement="2.00", cost="20")
    assert result.platform_fee == Decimal("10.01")
    assert result.after_platform == Decimal("90.04")
    assert result.withdrawal_fee == Decimal("4.50")
    assert result.expected == Decimal("83.54")
    assert result.profit == Decimal("63.54")


def test_no_fabricated_fees_or_profit():
    result = calculate(500)
    assert result.expected is None and result.profit is None
    assert len(result.unknown) == 3
    assert "Точный расчёт недоступен" in format_calculation(result)
    result = calculate(500, rule(10), rule(0), 0)
    assert result.expected == 450 and result.profit is None
    assert "Прибыль" not in format_calculation(result)


def test_minimum_fee_and_premium_once():
    result = calculate(100, rule(1, 4), rule(0), 5)
    assert result.expected == 91
    # The next sale incurs no previous, one-time placement cost.
    assert calculate(100, rule(1, 4), rule(0), 0).expected == 96


def test_estimates_labelled():
    assert "Оценка" in format_calculation(calculate(100, rule(10, estimated=True), rule(0), 0))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "foo"])
def test_invalid_money(value):
    with pytest.raises(ValueError):
        money(value)


def test_rate_requires_evidence():
    with pytest.raises(ValueError):
        FeeRule(Decimal(10), Decimal(0), "", "", "")
    with pytest.raises(ValueError):
        rule(101)


def test_discount_math_is_not_discount_publication():
    assert discount_percent(500, 400) == 20
    with pytest.raises(ValueError):
        discount_percent(100, 200)


def test_stale_and_naive_quotes_rejected():
    assert not quote_fresh("2020-01-01T00:00:00+00:00")
    assert not quote_fresh("2026-10-29T00:00:00")
    assert not quote_fresh("invalid")
