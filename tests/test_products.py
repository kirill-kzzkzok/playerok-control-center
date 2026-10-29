from io import BytesIO

import pytest
from PIL import Image

from app.services.products import (
    Products,
    ValidationError,
    category_form,
    integer_price,
    save_image,
    validate_field,
)


def test_form_uses_real_options_and_seller_fields():
    form = category_form(
        {
            "options": [
                {"field": "server", "group": "Сервер", "type": "SELECTOR", "value": "EU", "label": "Europe"}
            ]
        },
        [
            {"id": "login", "label": "Логин", "required": True, "type": "ITEM_DATA", "input_type": "INPUT"},
            {"id": "buyer", "type": "OBTAINING_DATA"},
        ],
    )
    assert len(form) == 2
    assert validate_field(form[0], "EU") == "EU"
    with pytest.raises(ValidationError):
        validate_field(form[0], "FAKE")
    with pytest.raises(ValidationError):
        validate_field(form[1], "")


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "12.34", "abc", "1000000001"])
def test_invalid_prices(value):
    with pytest.raises(ValidationError):
        integer_price(value)


def test_numeric_limits():
    field = {"kind": "number", "required": True, "options": [{"value_range_limit": {"min": 3, "max": 8}}]}
    assert validate_field(field, "5") == "5"
    for value in ["NaN", "2", "9", "abc"]:
        with pytest.raises(ValidationError):
            validate_field(field, value)


def test_unknown_schema_fails_closed():
    with pytest.raises(ValidationError):
        category_form({}, [{"id": "x", "required": True, "type": "ITEM_DATA", "input_type": "UNKNOWN"}])


def test_image_validation(tmp_path):
    stream = BytesIO()
    Image.new("RGB", (30, 30), "purple").save(stream, "PNG")
    path = save_image(stream.getvalue(), tmp_path)
    with Image.open(path) as image:
        assert image.format == "JPEG"
    with pytest.raises(ValidationError):
        save_image(b"not an image", tmp_path)
    with pytest.raises(ValidationError):
        save_image(b"x" * 20, tmp_path, 10)


async def test_free_quote_requires_explicit_zero():
    class Fake:
        async def call(self, *args, **kwargs):
            return [{"id": "free", "type": "DEFAULT"}, {"id": "premium", "type": "PREMIUM", "price": 20}]

    with pytest.raises(ValidationError):
        await Products(None, Fake()).free_priority("x", 100)
