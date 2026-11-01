"""Forecast uses selected withdrawal; unknown spending is never a zero cost."""

from tests.test_targeted_ux import financial as financial_fixture

financial = financial_fixture


async def test_percentage_withdrawal_estimate_is_included_in_estimated_profit(financial):
    r = financial
    await r.ui.message("/start")
    await r.ui.click("providers")
    await r.ui.click("fin:select:SBP")
    await r.ui.click("finance:item")
    assert "Оценка прибыли после процентной комиссии: ≈ 105.96 ₽" in r.session.last_panel.text
    assert "Чистая прибыль:" not in r.session.last_panel.text
    assert "минимальной комиссии" in r.session.last_panel.text


async def test_item_card_shows_cost_and_nonfabricated_profit_without_extra_api_queries(financial):
    r = financial
    await r.ui.message("/start")
    before = len(r.api.calls)
    await r.ui.click("item:item")
    assert "Себестоимость: 20.00 ₽" in r.session.last_panel.text
    assert "До вывода и размещения: ≈ 114.00 ₽" in r.session.last_panel.text
    assert len(r.api.calls) - before == 1
