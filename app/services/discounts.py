from decimal import ROUND_FLOOR, Decimal

from app.services.finance import discount_percent
from app.services.products import integer_price, listing_price
from app.services.products_errors import ValidationError


def discount_plan(item, sale, role=None):
    """Official edit form: normal sellers set price; existing rawPrice is immutable.

    The client accepts a sale price up to floor(rawPrice * .95), or resets to
    rawPrice. No fake old price and no rawPrice mutation are introduced here.
    """
    if item.get("raw_price") is None or item.get("price") is None:
        raise ValidationError("API не вернул базовую и текущую цену. Скидка недоступна.")
    base = integer_price(item["raw_price"])
    current = integer_price(item["price"])
    sale = listing_price(sale, role)
    maximum = int((Decimal(base) * Decimal("0.95")).quantize(Decimal(1), rounding=ROUND_FLOOR))
    minimum = 10 if role == "OFFICIAL_SELLER" else 90
    restore = sale == base
    if not restore and (maximum <= minimum or sale > maximum):
        raise ValidationError(
            f"Цена со скидкой должна быть от {minimum} до {maximum} ₽; для снятия скидки укажите {base} ₽"
        )
    return {
        "price": sale,
        "expected_price": current,
        "expected_raw_price": base,
        "discount_percent": "0" if restore else str(discount_percent(base, sale)),
        "restore": restore,
    }
