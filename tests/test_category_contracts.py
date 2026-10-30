import json
from decimal import Decimal

import httpx
import pytest

from app.playerok.adapter import SafeAccount
from app.services.finance import calculate, sale_rule_from_api, selected_fee_multiplier
from app.services.products import (
    ValidationError,
    category_form,
    listing_price,
    listing_text,
    validate_draft,
    validate_field,
)
from tests.transport_helpers import mutate


def test_unstructured_data_requires_private_comment_without_fabricated_attribute():
    form = category_form({"options": []}, [])
    assert form == [
        {
            "key": "__comment",
            "label": "Данные для покупателя после покупки",
            "kind": "text",
            "required": True,
            "comment": True,
            "min_length": 2,
            "max_length": 2048,
        }
    ]
    payload = {
        "category_id": "c",
        "obtaining_type_id": None,
        "obtaining_not_required": True,
        "name": "Test product",
        "description": "Test description",
        "price": 100,
        "fields": {},
    }
    with pytest.raises(ValidationError):
        validate_draft(payload, form)
    payload["fields"]["__comment"] = "Private buyer data"
    validate_draft(payload, form)
    for value in ["x", "x" * 2049]:
        with pytest.raises(ValidationError):
            validate_field(form[0], value)


def test_structured_seller_fields_do_not_add_unstructured_comment():
    form = category_form(
        {}, [{"id": "data", "type": "ITEM_DATA", "input_type": "TEXTAREA", "required": True}]
    )
    assert len(form) == 1 and form[0]["key"] == "data" and not form[0].get("comment")


async def test_create_item_serializes_real_comment_input():
    requests = []

    def respond(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["variables"]["input"]["comment"] == "Private buyer data"
        assert body["variables"]["input"]["attributes"] == {}
        return httpx.Response(200, json={"data": {"createItem": {"id": "isolated", "__typename": "MyItem"}}})

    account = SafeAccount(
        "token=private", writes_enabled=True, client=httpx.Client(transport=httpx.MockTransport(respond))
    )
    try:
        await mutate(
            account,
            "createItem",
            "create_item",
            game_category_id="category",
            obtaining_type_id=None,
            name="Test product",
            price=100,
            description="Test description",
            options={},
            data_fields=[],
            attachments=[],
            comment="Private buyer data",
        )
        assert len(requests) == 1
    finally:
        account.close()


@pytest.mark.parametrize(
    "category,obtaining,expected",
    [
        (None, None, None),
        ("0.1", None, "0.1"),
        (None, "0.2", "0.2"),
        ("0.1", "0.2", "0.2"),
        ("0.2", "0.1", "0.1"),
        ("0.2", 0, "0"),
    ],
)
def test_selected_type_fee_overrides_category_without_default(category, obtaining, expected):
    assert selected_fee_multiplier(category, obtaining) == expected


def test_website_income_rounding_is_explicit_estimate():
    rule = sale_rule_from_api("0.1", "2026-10-29T00:00:00+00:00")
    calculation = calculate("100.05", platform=rule)
    assert calculation.after_platform == Decimal("90.00")
    assert calculation.platform_fee == Decimal("10.05")
    assert calculation.estimated
    assert calculation.expected is None  # withdrawal and placement are still unknown


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "x" * 9),
        ("name", "x" * 73),
        ("description", "x" * 9),
        ("description", "x" * 2049),
        ("name", "😀" * 37),
    ],
)
def test_current_frontend_text_limits_and_utf16_length(field, value):
    with pytest.raises(ValidationError):
        listing_text(value, field)


def test_current_frontend_text_boundary_values_are_accepted():
    assert listing_text(" x" * 36, "name") == (" x" * 36).strip()
    assert listing_text("x" * 2048, "description") == "x" * 2048


@pytest.mark.parametrize("value,role", [(89, None), (100001, None), (9, "OFFICIAL_SELLER"), (89, "USER")])
def test_price_constraints_are_enforced_before_mutation(value, role):
    with pytest.raises(ValidationError):
        listing_price(value, role)


def test_price_boundary_depends_on_verified_account_role():
    assert listing_price(90, "USER") == 90
    assert listing_price(10, "OFFICIAL_SELLER") == 10
    assert listing_price(100000, "USER") == 100000
