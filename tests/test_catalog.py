from datetime import UTC, datetime

import pytest

from app.database.store import Store, server_time
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


def test_order_list_uses_backfilled_aware_server_time_instead_of_ingest_time(tmp_path):
    store = Store(tmp_path / "chronology.sqlite")
    try:
        orders = Orders(store)

        def ingest(ident, created):
            orders.ingest(
                {
                    "id": ident,
                    "direction": "OUT",
                    "status": "PAID",
                    "created_at": created,
                    "item": {"id": "p", "price": 100},
                }
            )

        ingest("newer", None)
        ingest("older", None)
        ingest("newer", "2026-10-29T10:00:00Z")
        ingest("older", "2024-01-01T10:00:00+00:00")
        ingest("offset", "2026-10-29T12:30:00+03:00")
        ingest("unknown", None)
        ingest("naive", "2099-01-01T00:00:00")
        assert [row["id"] for row in order_page(store, page_size=2)["rows"]] == ["newer", "offset"]
        assert order_page(store, 1, page_size=2)["rows"][0]["id"] == "older"
        for malformed in ["invalid", "2026-10-29T10:00:00", None]:
            ingest("newer", malformed)
            result = order_page(store, page_size=2)
            assert result["rows"][0]["id"] == "newer"
            assert result["rows"][0]["server_created_at"] == "2026-10-29T10:00:00.000000+00:00"
        # Two equivalent instants tie by stable local receive/id ordering.
        ingest("same", "2026-10-29T13:00:00+03:00")
        assert {row["id"] for row in order_page(store, page_size=2)["rows"]} == {"same", "newer"}
    finally:
        store.close()


@pytest.mark.parametrize(
    "value", [None, 123, "", "bad", "2026-10-30", "2026-10-30T10:00:00", "0001-01-01T00:00:00+01:00"]
)
def test_server_time_rejects_unknown_naive_or_overflow(value):
    assert server_time(value) is None


def test_server_time_canonical_precision_matches_equal_offsets():
    assert server_time("2026-10-30T10:00:00Z") == server_time("2026-10-30T13:00:00.000000+03:00")
    assert server_time("2026-10-30T10:00:00.1Z") > server_time("2026-10-30T10:00:00Z")


def test_html_limit_never_cuts_entities_or_emoji():
    assert escaped("<" * 100, 6) == "&lt;"
    assert escaped("😀😀", 3) == "😀"
    assert escaped("abc&def", 6) == "abc"


def test_multi_choice_roundtrips_saved_values():
    field = {"kind": "multi", "required": True, "options": [{"value": "a"}, {"value": "b"}]}
    result = validate_field(field, "a,b")
    assert validate_field(field, result) == ["a", "b"]
