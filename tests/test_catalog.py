from datetime import UTC, datetime

import pytest

from app.database.store import Store
from app.services.catalog import order_page
from app.services.orders import Orders
from app.services.products import ValidationError, validate_field
from app.telegram.formatting import escaped


def test_order_pagination_filters_and_safe_search(tmp_path):
    store = Store(tmp_path / "catalog.sqlite")
    try:
        orders = Orders(store)
        for i in range(27):
            orders.ingest(
                {
                    "id": str(i),
                    "status": "PAID" if i % 2 else "ROLLED_BACK",
                    "direction": "OUT",
                    "has_problem": i == 3,
                    "item": {"id": "p", "name": "literal_% name", "price": 100},
                    "created_at": datetime.now(UTC).isoformat(),
                }
            )
        first = order_page(store)
        second = order_page(store, 1)
        assert first["total"] == 27 and first["has_next"]
        assert not {r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]}
        assert order_page(store, filter_name="PAID")["total"] == 13
        assert order_page(store, filter_name="PROBLEM")["total"] == 1
        assert order_page(store, search="_%")["total"] == 27
        assert order_page(store, search="' OR 1=1 --")["total"] == 0
        with pytest.raises(ValidationError):
            order_page(store, -1)
    finally:
        store.close()


def test_html_limit_never_cuts_entities_or_emoji():
    assert escaped("<" * 100, 6) == "&lt;"
    assert escaped("😀😀", 3) == "😀"
    assert escaped("abc&def", 6) == "abc"


def test_multi_choice_roundtrips_saved_values():
    field = {"kind": "multi", "required": True, "options": [{"value": "a"}, {"value": "b"}]}
    result = validate_field(field, "a,b")
    assert validate_field(field, result) == ["a", "b"]
