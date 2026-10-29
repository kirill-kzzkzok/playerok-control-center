import uuid
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from app.database.store import Conflict


class ValidationError(ValueError):
    pass


def integer_price(value):
    try:
        price = Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        raise ValidationError("Введите цену числом") from None
    if not price.is_finite() or price <= 0 or price != price.to_integral_value() or price > 1_000_000_000:
        raise ValidationError("Требуется положительная целая цена в рублях")
    return int(price)


def category_form(category, data_fields):
    groups = defaultdict(list)
    for option in category.get("options") or []:
        if not option.get("field"):
            raise ValidationError("API вернул атрибут без field")
        groups[option["field"]].append(option)
    fields = []
    for name, options in groups.items():
        if any(o.get("multiple") for o in options):
            kind = "multi"
        else:
            types = {o.get("type") for o in options}
            if types <= {"SELECTOR", "SWITCH"}:
                kind = "choice"
            elif types == {"RANGE"}:
                kind = "number"
            else:
                raise ValidationError("Неподдерживаемый тип атрибута категории")
        fields.append(
            {
                "key": name,
                "label": options[0].get("group") or name,
                "kind": kind,
                "required": True,
                "options": options,
            }
        )
    for field in data_fields:
        if field.get("type") != "ITEM_DATA":
            continue  # Buyer's obtaining fields must never be requested as seller data.
        input_type = field.get("input_type")
        if input_type not in {"INPUT", "TEXT", "TEXTAREA", "NUMBER", "INTEGER"}:
            raise ValidationError("Неподдерживаемый тип обязательной схемы: публикация заблокирована")
        fields.append(
            {
                "key": field["id"],
                "label": field.get("label") or field["id"],
                "kind": "number" if input_type in {"NUMBER", "INTEGER"} else "text",
                "required": bool(field.get("required")),
                "data_field": True,
            }
        )
    return fields


def validate_field(field, value):
    text = str(value).strip()
    if not text:
        if field["required"]:
            raise ValidationError("Поле обязательно")
        return ""
    if field["kind"] in {"choice", "multi"}:
        options = {str(o["value"]): o["value"] for o in field["options"]}
        if field["kind"] == "multi":
            selected = (
                [str(v).strip() for v in value]
                if isinstance(value, list)
                else [v.strip() for v in text.split(",")]
            )
        else:
            selected = [text]
        if any(v not in options for v in selected):
            raise ValidationError("Выберите значение из списка API")
        return [options[v] for v in selected] if field["kind"] == "multi" else options[selected[0]]
    if field["kind"] == "number":
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ValidationError("Введите число") from None
        if not number.is_finite():
            raise ValidationError("Недопустимое число")
        for option in field.get("options", []):
            limits = option.get("value_range_limit") or {}
            if limits.get("min") is not None and number < Decimal(str(limits["min"])):
                raise ValidationError("Число меньше минимума API")
            if limits.get("max") is not None and number > Decimal(str(limits["max"])):
                raise ValidationError("Число больше максимума API")
        return text
    if len(text) > 10_000:
        raise ValidationError("Слишком длинное значение")
    return text


def validate_draft(payload, form):
    for key in ["category_id", "name", "description"]:
        if not str(payload.get(key) or "").strip():
            raise ValidationError(f"Не заполнено поле {key}")
    if not payload.get("obtaining_type_id") and not payload.get("obtaining_not_required"):
        raise ValidationError("Способ получения не выбран")
    integer_price(payload.get("price", ""))
    values = payload.get("fields", {})
    for field in form:
        validate_field(field, values.get(field["key"], ""))
    if payload.get("discount") is not None:
        raise ValidationError("Механизм установки скидки не подтверждён")


def save_image(data, directory, max_size=10_000_000):
    if not data or len(data) > max_size:
        raise ValidationError("Файл пуст или превышает локальный лимит 10 МБ")
    try:
        with Image.open(BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValidationError("Поддерживаются JPEG, PNG и WEBP")
            if image.width * image.height > 25_000_000:
                raise ValidationError("Слишком большое разрешение")
            image.verify()
        with Image.open(BytesIO(data)) as image:
            image = image.convert("RGB")
            path = Path(directory) / (uuid.uuid4().hex + ".jpg")
            path.parent.mkdir(parents=True, exist_ok=True)
            image.save(path, "JPEG", quality=92)
            return str(path.resolve())
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValidationError("Повреждённое или неподдерживаемое изображение") from None


class Products:
    def __init__(self, store, api):
        self.store, self.api = store, api

    async def schema(self, category_id, obtaining_type_id):
        category = await self.api.call("get_game_category", id=category_id)
        obtaining = []
        async for page in self.api.pages(
            "get_game_category_obtaining_types", "obtaining_types", game_category_id=category_id
        ):
            obtaining.extend(page)
        if obtaining and obtaining_type_id not in {o["id"] for o in obtaining}:
            raise ValidationError("Способ получения не принадлежит текущей категории")
        if not obtaining and obtaining_type_id is not None:
            raise ValidationError("У категории отсутствуют способы получения")
        fields = []
        async for page in self.api.pages(
            "get_game_category_data_fields",
            "data_fields",
            game_category_id=category_id,
            obtaining_type_id=obtaining_type_id,
        ):
            fields.extend(page)
        return category_form(category, fields)

    async def queue_create(self, ident, owner, revision):
        draft = self.store.get_draft(ident, owner)
        form = await self.schema(draft["payload"]["category_id"], draft["payload"]["obtaining_type_id"])
        validate_draft(draft["payload"], form)
        with self.store.transaction() as db:
            r = db.execute(
                "UPDATE drafts SET state='QUEUED',revision=revision+1 WHERE id=? AND owner_id=? AND revision=? AND state='DRAFT'",
                (ident, owner, revision),
            )
            if r.rowcount != 1:
                raise Conflict("Черновик уже отправлен или изменён")
            operation = self.store.enqueue(f"create:{ident}", "create", ident, {"owner": owner}, db)
        return operation

    async def priority_preview(self, item_id, price):
        statuses = await self.api.call(
            "get_item_priority_statuses", item_id=item_id, item_price=integer_price(price)
        )
        if not isinstance(statuses, list):
            raise ValidationError("Цена размещения не получена")
        return statuses

    async def free_priority(self, item_id, price):
        statuses = await self.priority_preview(item_id, price)
        for status in statuses:
            if (
                status.get("type") == "DEFAULT"
                and status.get("price") is not None
                and Decimal(str(status["price"])) == 0
            ):
                return status
        raise ValidationError("Бесплатность размещения не подтверждена API")
