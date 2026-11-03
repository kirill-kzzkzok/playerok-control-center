"""SOLD alone is not evidence that the server permits another placement."""

import pytest

from tests.test_quick_relist_status import shop as shop_fixture
from tests.test_ux_redesign import buttons

shop = shop_fixture


@pytest.mark.parametrize("permission,price", [(False, 100), (None, 100), (True, None)])
async def test_sold_card_requires_confirmed_publication_permission_and_price(shop, permission, price):
    r = shop
    r.old.update(may_be_published=permission, raw_price=price)
    await r.ui.click("item:old")
    labels = buttons(r.session.last_panel)
    assert "🔄 Перевыставить" not in labels.values()
    if permission is False:
        assert "relist:item:old" not in labels
        assert "не разрешил повторное размещение" in r.session.last_panel.text
    else:
        assert "Проверить возможность перевыставления" in labels.values()
    assert not r.api.created and not r.api.published and not r.remote.payments
