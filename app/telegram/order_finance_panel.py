import secrets

from app.database.store import Conflict
from app.services.finance import checked_time
from app.services.order_finance import OrderFinance, declared_amount, source_fingerprint
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard


class OrderFinancePanel:
    def __init__(self, store, api):
        self.store, self.api = store, api

    def service(self):
        account = getattr(self.api.account, "id", None) or self.store.setting("playerok:account_id")
        if not account:
            raise Conflict("Нет подтверждённого аккаунта для финансового учёта")
        return OrderFinance(self.store, account)

    def details(self, value):
        def shown(amount):
            return str(amount) + " ₽" if amount is not None else "не задано"

        receipt = value["receipt"]
        text = (
            "🧾 <b>Учёт заказа</b>\n<code>"
            + escaped(value["order_id"], 100)
            + "</code>"
            + "\nЗачисление за продажу: "
            + shown(value["credit"])
            + "\nЗавершено: "
            + checked_time(receipt["completed_at"] if receipt else None)
            + "\nСебестоимость всего заказа: "
            + shown(value["cost"])
            + "\nОстальные расходы: "
            + shown(value["other_expenses"])
            + f"\nРаспределено подтверждённых расходов: {value['allocated']} ₽ (источников: {len(value['allocations'])})"
            + "\nПрибыль по учёту владельца: "
            + shown(value["profit"])
        )
        settlement = value["settlement"]
        if settlement and settlement["reconciled"]:
            text += (
                f"\nЦена заказа: {settlement['gross']} ₽ · сверенная комиссия продажи: {settlement['fee']} ₽"
            )
            text += "\nПроверено равенство: цена − fee = зачисление. Комиссия уже удержана, повторно не вычитается."
        elif settlement:
            text += "\nКомиссия продажи не сверена: " + escaped(settlement["reason"], 250)
        if value["errors"]:
            text += "\n\nНе хватает данных:\n" + "\n".join(escaped(error, 300) for error in value["errors"])
        elif not value["reviewed"]:
            text += "\n\nПолнота расходов ещё не подтверждена или исходные суммы изменились."
        text += (
            "\n\nСебестоимость и прочие расходы — ваши исторические данные, не текущая цена товара. "
            "Распределённые PREMIUM/удержания выплаты не включайте повторно в остальные расходы. "
            "Это результат по указанным расходам, не банковская выписка и не аудированная прибыль."
        )
        return text

    def card(self, owner, ident):
        value = self.service().view(owner, ident)
        rows = [
            [("Себестоимость заказа", "pf:edit:cost:" + ident)],
            [("Остальные расходы", "pf:edit:other_expenses:" + ident)],
            [("Распределить PREMIUM/удержания", f"pf:sources:{ident}:0")],
            [("Мои распределения", f"pf:allocs:{ident}:0")],
            [("Очистить себестоимость", "pf:clear:cost:" + ident)],
            [("Очистить прочие расходы", "pf:clear:other_expenses:" + ident)],
        ]
        if not value["errors"]:
            rows.append([("Проверить полноту и итог", "pf:review:" + ident)])
        rows.append([("К заказу", "order:" + ident)])
        return self.details(value), keyboard(rows)

    async def confirm(self, state, pending, text):
        nonce = secrets.token_hex(6)
        await state.set_data({"profit_pending": pending, "profit_nonce": nonce})
        await state.set_state("profit:confirm")
        return text, keyboard(
            [[("Сохранить локально", "pf:commit:" + nonce)], [("Отмена", "pf:card:" + pending["order_id"])]]
        )

    async def callback(self, action, owner, state):
        parts = action.split(":")
        command, ident = parts[1:3]
        service = self.service()
        if command == "card":
            result = self.card(owner, ident)
            await state.clear()
            return result
        if command == "commit":
            data = await state.get_data()
            pending = data.get("profit_pending")
            if not pending or ident != data.get("profit_nonce"):
                raise Conflict("Подтверждение устарело")
            target, revision = pending["order_id"], pending["revision"]
            if service.account_id != pending["account_id"]:
                raise Conflict("Аккаунт изменился после превью")
            if pending["kind"] == "review":
                service.review(owner, target, revision, pending["fingerprint"])
            elif pending["kind"] == "field":
                service.set_cost(owner, target, revision, pending["field"], pending["value"])
            elif pending["kind"] == "allocation":
                service.allocate(
                    owner,
                    target,
                    revision,
                    pending["transaction_id"],
                    pending["value"],
                    expected_source=pending["source_fingerprint"],
                )
            else:
                raise Conflict("Финансовое действие устарело")
            await state.clear()
            return self.card(owner, target)
        if command in {"sources", "allocs"}:
            page = int(parts[3])
            value = service.view(owner, ident)
            if command == "sources":
                entries, more = service.expense_sources(owner, ident, page)
            else:
                if not 0 <= page <= 1_000_000:
                    raise ValueError("Недопустимая страница")
                allocations = value["allocations"][page * 8 : page * 8 + 9]
                entries, more = [], len(allocations) > 8
                for allocation in allocations[:8]:
                    rows = self.store.rows(
                        "SELECT * FROM account_transactions WHERE account_id=? AND id=?",
                        (service.account_id, allocation["transaction_id"]),
                    )
                    if rows:
                        entries.append({**rows[0], "capacity": allocation["amount"]})
            entries = [
                {**row, "capacity": str(row["capacity"]) if row["capacity"] is not None else None}
                for row in entries
            ]
            nonce = secrets.token_hex(6)
            await state.set_state(None)
            await state.set_data(
                {
                    "profit_selection": {
                        "order_id": ident,
                        "revision": value["basis"]["revision"],
                        "account_id": service.account_id,
                        "entries": entries,
                        "remove": command == "allocs",
                    },
                    "profit_nonce": nonce,
                }
            )
            text = (
                "🧾 <b>"
                + ("Распределения этого заказа" if command == "allocs" else "Источники расходов")
                + f" · {page + 1}</b>"
            )
            rows = [
                [
                    (
                        f"{row['operation']} · {row['capacity'] if row['capacity'] is not None else '?'} ₽ · {row['id'][:8]}",
                        f"pf:pick:{nonce}:{index}",
                    )
                ]
                for index, row in enumerate(entries)
            ]
            navigation = []
            if page:
                navigation.append(("← Назад", f"pf:{command}:{ident}:{page - 1}"))
            if more:
                navigation.append(("Далее →", f"pf:{command}:{ident}:{page + 1}"))
            if navigation:
                rows.append(navigation)
            rows.append([("Учёт заказа", "pf:card:" + ident)])
            if not entries:
                text += "\nНет записей."
            text += "\nДля WITHDRAW доступна только разница списания и явной суммы к получению; это не весь вывод. Сумма источника распределяется между заказами один раз. В списке распределений можно снять прежнее распределение."
            return text, keyboard(rows)
        if command == "pick":
            data = await state.get_data()
            selection = data.get("profit_selection")
            if (
                not selection
                or ident != data.get("profit_nonce")
                or selection["account_id"] != service.account_id
            ):
                raise Conflict("Выбор устарел")
            index = int(parts[3])
            if not 0 <= index < len(selection["entries"]):
                raise Conflict("Источник не найден")
            source = selection["entries"][index]
            pending = {
                "kind": "allocation",
                "order_id": selection["order_id"],
                "revision": selection["revision"],
                "account_id": service.account_id,
                "transaction_id": source["id"],
                "source_fingerprint": source_fingerprint(source),
            }
            if selection["remove"]:
                pending["value"] = "0.00"
                return await self.confirm(
                    state,
                    pending,
                    "Снять только это локальное распределение? Подтверждение полноты расходов будет сброшено.",
                )
            await state.set_data({"profit_pending": pending})
            await state.set_state("profit:amount")
            return (
                "Введите долю этого расхода для заказа в ₽ (0 снимает распределение). Общая сумма распределений проверяется перед сохранением.",
                keyboard([]),
            )
        if command in {"edit", "clear"}:
            field, target = ident, parts[3]
            if field not in {"cost", "other_expenses"}:
                raise ValueError("Неизвестный расход")
            value = service.view(owner, target)
            pending = {
                "kind": "field",
                "order_id": target,
                "revision": value["basis"]["revision"],
                "account_id": service.account_id,
                "field": field,
            }
            if command == "clear":
                pending["value"] = None
                return await self.confirm(
                    state,
                    pending,
                    "Очистить значение? Оно станет неизвестным, а не нулём. Подтверждение полноты расходов будет сброшено.",
                )
            await state.set_data({"profit_pending": pending})
            await state.set_state("profit:amount")
            return (
                "Введите историческую себестоимость всего заказа"
                if field == "cost"
                else "Введите остальные расходы этого заказа без уже распределённых PREMIUM/удержаний"
            ) + " в ₽. Ноль указывается явно. Текущая себестоимость товара не подставляется.", keyboard([])
        if command == "review":
            value = service.view(owner, ident)
            if value["errors"]:
                raise Conflict("Заполните отсутствующие данные и проверьте источники")
            pending = {
                "kind": "review",
                "order_id": ident,
                "revision": value["basis"]["revision"],
                "account_id": service.account_id,
                "fingerprint": value["fingerprint"],
            }
            return await self.confirm(
                state,
                pending,
                self.details(value)
                + f"\n\nРасчёт после указанных расходов: {value['candidate_profit']} ₽.\nПодтверждаю, что историческая себестоимость и все относящиеся к заказу расходы учтены; распределённые суммы не введены повторно в прочие расходы. Это моя проверка, не подтверждение Playerok.",
            )
        raise ValueError("Финансовое действие недоступно")

    async def message(self, message, state):
        data = await state.get_data()
        pending = data.get("profit_pending")
        if await state.get_state() != "profit:amount" or not pending:
            raise Conflict("Используйте кнопку подтверждения или откройте учёт заново")
        self.service().view(message.from_user.id, pending["order_id"])
        text = (message.text or "").strip()
        if len(text) > 32:
            raise ValueError("Слишком длинная денежная сумма")
        pending["value"] = declared_amount(text.replace(",", "."))
        return await self.confirm(
            state,
            pending,
            f"Сохранить {pending['value']} ₽ только в локальном учёте заказа? Полнота расходов проверяется отдельно.",
        )
