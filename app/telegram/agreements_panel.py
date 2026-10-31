"""Optional read-only seller terms inspector; never initiates acceptance."""

import json
import secrets

from app.services.products_errors import ValidationError
from app.telegram.formatting import html_pages
from app.telegram.keyboards import keyboard


class AgreementsPanel:
    def __init__(self, store, api, products):
        self.store, self.api, self.products = store, api, products

    async def callback(self, action, state):
        parts = action.split(":")
        if len(parts) < 3:
            raise ValidationError("Просмотр недоступен")
        data = await state.get_data()
        if parts[1] == "list":
            scopes = {}
            account = self.store.setting("playerok:bound_account_id")
            for row in self.store.rows("SELECT value FROM settings WHERE key LIKE 'agreements:journal:%'"):
                value = json.loads(row["value"])
                if value["account_id"] == account:
                    scopes[(value["category_id"], value["obtaining_type_id"])] = value
            values = sorted(scopes.values(), key=lambda v: v["checked_at"], reverse=True)
            page = int(parts[2])
            if page < 0 or (values and page * 10 >= len(values)):
                raise ValidationError("Страница условий недоступна")
            nonce = secrets.token_hex(4)
            await state.set_state(None)
            await state.set_data({"agreement_scopes": values, "agreement_view": nonce})
            rows = [
                [
                    (
                        f"Категория {value['category_id'][:12]} · {value['obtaining_type_id'] or 'общая'}",
                        f"agv:{nonce}:scope:{i}",
                    )
                ]
                for i, value in enumerate(values)
                if page * 10 <= i < (page + 1) * 10
            ]
            if (page + 1) * 10 < len(values):
                rows.append([("Далее", f"agv:list:{page + 1}")])
            if page:
                rows.append([("Назад", f"agv:list:{page - 1}")])
            rows.append([("К диагностике", "diagnostics")])
            return (
                "📄 Условия продавца · необязательный просмотр\nВыберите ранее проверенную категорию. Чтение не принимает условия."
                + ("\nИстории пока нет." if not values else ""),
                keyboard(rows),
            )
        if parts[1] != data.get("agreement_view"):
            raise ValidationError("Просмотр устарел; откройте условия из диагностики")
        if len(parts) != 4 or parts[2] not in {"scope", "page"}:
            raise ValidationError("Просмотр недоступен")
        index = int(parts[3])
        if parts[2] == "scope":
            values = data.get("agreement_scopes", [])
            if not 0 <= index < len(values):
                raise ValidationError("Категория недоступна")
            value = values[index]
            details = await self.products.schema_details(
                value["category_id"], value["obtaining_type_id"], auto_agreements=False
            )
            documents = details["conditions"]["documents"]
            pages = [p for d in documents for p in html_pages(d["title"] + "\n" + d["text"])] or [
                "Playerok не предоставил текстов."
            ]
            await state.update_data(agreement_pages=pages)
            index = 0
        else:
            pages = data.get("agreement_pages", [])
        if not 0 <= index < len(pages):
            raise ValidationError("Страница недоступна")
        rows = [[("К диагностике", "diagnostics")]]
        if index + 1 < len(pages):
            rows.append([("Далее", f"agv:{parts[1]}:page:{index + 1}")])
        if index:
            rows.append([("Назад", f"agv:{parts[1]}:page:{index - 1}")])
        return f"📄 Только просмотр · {index + 1}/{len(pages)}\n" + pages[index], keyboard(rows)
