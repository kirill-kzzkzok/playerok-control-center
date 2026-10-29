import hashlib
import json

from app.services.products_errors import ValidationError


def fingerprint(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def agreement_record(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("id"), str)
        or not value["id"]
        or not isinstance(value.get("description"), str)
        or not value["description"].strip()
    ):
        raise ValidationError("Playerok вернул неполное соглашение")
    return {
        "id": value["id"],
        "description": value["description"],
        "game_category_id": value.get("game_category_id"),
        "game_category_obtaining_type_id": value.get("game_category_obtaining_type_id"),
    }


class Agreements:
    def __init__(self, api):
        self.api = api

    async def pending(self, category_id, obtaining_type_id):
        values = {}
        async for page in self.api.pages(
            "get_game_category_agreements", "agreements", game_category_id=category_id
        ):
            for value in page:
                record = agreement_record(value)
                if record["game_category_id"] not in {None, category_id}:
                    raise ValidationError("Соглашение не относится к выбранной категории")
                if record["game_category_obtaining_type_id"] not in {None, obtaining_type_id}:
                    continue
                if record["id"] in values and values[record["id"]] != record:
                    raise ValidationError("Противоречивые версии соглашения Playerok")
                values[record["id"]] = record
        return sorted(values.values(), key=lambda value: value["id"])

    async def snapshot(self, category, obtaining):
        pending = await self.pending(category["id"], obtaining.get("id"))
        documents = {}
        for scope, entry in [("category", category), ("obtaining", obtaining)]:
            instruction = entry.get("instruction_for_seller")
            if instruction:
                if not isinstance(instruction, str):
                    raise ValidationError("Неполная инструкция продавцу")
                documents[f"{scope}:instruction"] = {
                    "id": f"{scope}:instruction",
                    "title": "Инструкция продавцу",
                    "text": instruction,
                }
            for agreement in entry.get("agreements") or []:
                record = agreement_record(agreement)
                if record["game_category_id"] not in {None, category["id"]}:
                    raise ValidationError("Соглашение не относится к выбранной категории")
                if record["game_category_obtaining_type_id"] not in {None, obtaining.get("id")}:
                    continue
                document = {
                    "id": record["id"],
                    "title": "Условия Playerok",
                    "text": record["description"],
                }
                if record["id"] in documents and documents[record["id"]] != document:
                    raise ValidationError("Противоречивые версии соглашения Playerok")
                documents[record["id"]] = document
        for record in pending:
            document = {"id": record["id"], "title": "Условия Playerok", "text": record["description"]}
            if record["id"] in documents and documents[record["id"]] != document:
                raise ValidationError("Условия Playerok изменились во время загрузки")
            documents[record["id"]] = document
        documents = sorted(documents.values(), key=lambda value: value["id"])
        return {"documents": documents, "hash": fingerprint(documents), "pending": pending}


def require_conditions(payload, conditions):
    if conditions["pending"]:
        raise ValidationError("На Playerok есть непринятые условия категории. Откройте «Условия» в превью.")
    if conditions["documents"] and payload.get("conditions_ack") != conditions["hash"]:
        raise ValidationError("Условия или инструкция изменились. Прочитайте их повторно в превью.")
