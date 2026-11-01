"""Owner setup of stable product identities in the existing navigation panel."""

import json
import secrets
from collections import Counter

from app.database.store import Conflict
from app.gifts.core import gift_code_from_description, gift_name
from app.services.automation import message_text
from app.services.inventory import Inventory
from app.services.logical_products import PROBLEMS, LogicalProducts, marked_description
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard
from app.telegram.presentation import local_time, order_label, status
from app.workers.logical_products import LogicalProductWorker

MODES = {
    "telegram_gifts": "Telegram-подарки",
    "unique_stock": "Уникальные данные со склада",
    "fixed_text": "Постоянный текст",
    "manual": "Ручная выдача",
}


def gift_summary(bundle):
    counts = Counter(g["code"] for g in bundle)
    return ", ".join(f"{gift_name(code)} ×{count}" for code, count in counts.items())


class LogicalProductsPanel:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.service = LogicalProducts(store, api)

    def owner(self, owner):
        if not self.store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
            raise Conflict("Доступ закрыт")

    async def card(self, owner, item_id, state):
        self.owner(owner)
        item = await self.api.call("get_item", id=item_id)
        account = self.service.authority(owner)
        if item.get("id") != item_id or (item.get("user") or {}).get("id") != account:
            raise Conflict("Владелец объявления не подтверждён")
        product = self.service.listing(item_id)
        if product and product["owner_id"] != owner:
            raise Conflict("Настройки принадлежат другому владельцу")
        view = {
            "nonce": secrets.token_hex(6),
            "owner": owner,
            "item": item_id,
            "title": item.get("name") or "Товар",
            "product": product["id"] if product else None,
            "revision": product["revision"] if product else 0,
        }
        await state.set_state(None)
        await state.set_data({"logical_view": view})
        prefix = "lp:" + view["nonce"] + ":"
        text = "⚡ <b>Автовыдача товара</b>\n" + escaped(view["title"], 120)
        rows = []
        if product:
            config = json.loads(product["config"])
            text += "\nПостоянный код: " + escaped(product["code"], 20)
            text += "\nСпособ: " + MODES[product["handler"]]
            text += "\nРазрешение выдачи: " + ("включено" if product["enabled"] else "выключено")
            if product["handler"] == "telegram_gifts":
                bundle = config.get("bundle", [])
                text += "\n" + escaped(gift_summary(bundle), 250)
                text += f"\nСогласовано: {sum(g['stars'] for g in bundle)} ⭐"
            elif product["handler"] == "unique_stock":
                text += (
                    f"\nДоступно на складе: {Inventory(self.store, self.api).counts(item_id)['AVAILABLE']}"
                )
            elif product["handler"] == "fixed_text":
                text += f"\nТекст выдачи сохранён: {len(config.get('text', ''))} символов; содержимое скрыто"
            if product["state"] == "LEGACY":
                text += "\nНужен перенос старой привязки: код ещё не добавлен в Playerok. Действующая история сохранена."
            elif product["state"] == "PENDING":
                operation = self.store.rows(
                    "SELECT status FROM outbox WHERE id=?", (product["operation_id"],)
                )
                outcome = operation[0]["status"] if operation else None
                text += "\nОписание: " + status(outcome)
                if outcome == "UNCERTAIN":
                    rows.append([("🔎 Сверить описание без повторной записи", prefix + "reconcile")])
                elif outcome == "PERMANENT_FAILED":
                    text += "\nПривязка не завершена. Обновите настройки и подтвердите новое превью."
            elif product["state"] == "DETACHED":
                text += "\nПривязка отключена. Публичный код не разрешает выдачу."
            else:
                try:
                    self.service.verify_listing(product, item)
                except ValueError:
                    text += "\n⚠️ Параметры объявления изменились. Повторно проверьте привязку."
                else:
                    text += "\nОписание подтверждено Playerok"
                text += "\nНаследование перевыставлений: " + (
                    "разрешено" if product["inherit_enabled"] else "выключено"
                )
            completed = self.store.setting("logical:scan:" + account + ":completed", {})
            text += "\nСверка каталога: " + (
                local_time(completed.get("at")) if completed else "ещё не завершена"
            )
            if product["error_code"]:
                text += "\n⚠️ " + PROBLEMS.get(product["error_code"], "Сверка объявления требует проверки.")
            active_projection = self.store.rows(
                "SELECT 1 FROM gift_listings WHERE item_id=? AND enabled=1 UNION ALL "
                "SELECT 1 FROM delivery_policies WHERE item_id=? AND json_extract(payload,'$.enabled')=1 LIMIT 1",
                (item_id, item_id),
            )
            if product["enabled"] or active_projection:
                rows.append([("⛔ Остановить выдачу товара", prefix + "stop")])
            elif product["state"] == "VERIFIED" and product["handler"] != "manual":
                rows.append([("▶️ Включить выдачу", prefix + "arm")])
            rows += [
                [("Изменить настройки", prefix + "configure")],
                [("Связанные объявления", prefix + "links"), ("История заказов", prefix + "orders:0")],
                [("🔄 Проверить перевыставление", prefix + "check")],
                [("Отвязать товар", prefix + "detach")],
            ]
            if product["handler"] != "manual":
                rows.insert(1, [("Сообщение после выдачи", prefix + "editafter")])
            delivered = self.store.rows(
                "SELECT o.id,o.created_at FROM logical_order_scopes s JOIN orders o ON o.id=s.order_id "
                "WHERE s.product_id=? AND (EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id AND g.state='SUCCEEDED' AND g.receipt IS NOT NULL) "
                "OR EXISTS(SELECT 1 FROM delivery_chain_steps d JOIN outbox b ON b.id=d.operation_id WHERE d.order_id=o.id AND d.kind IN ('delivery','delivery_text') AND b.status='SUCCEEDED')) "
                "ORDER BY o.created_at DESC,o.id DESC LIMIT 1",
                (product["id"],),
            )
            if delivered:
                text += "\nПоследний заказ с доставкой: " + local_time(delivered[0]["created_at"])
                rows.insert(0, [("Последняя подтверждённая выдача", "order:" + delivered[0]["id"])])
        else:
            text += "\nНастройте выдачу один раз и подтвердите постоянный код в описании."
            rows.append([("Настроить выдачу", prefix + "configure")])
        if product and product["handler"] == "unique_stock":
            rows.insert(0, [("📚 Склад", "stock:view:" + item_id)])
        rows.append([("🔄 Обновить", "lp:item:" + item_id)])
        return text, keyboard(rows, back=("К настройке товара", "iv:delivery:" + item_id))

    async def view(self, owner, state, nonce):
        self.owner(owner)
        view = (await state.get_data()).get("logical_view", {})
        if not view or view.get("nonce") != nonce or view.get("owner") != owner:
            raise Conflict("Экран устарел. Откройте товар и настройку выдачи заново")
        if view.get("product"):
            product = self.service.product(view["product"], owner)
            if product["revision"] != view["revision"]:
                raise Conflict("Настройки товара изменились. Обновите карточку")
        return view

    async def callback(self, action, owner, state):
        parts = action.split(":")
        if len(parts) == 3 and parts[:2] == ["lp", "item"]:
            return await self.card(owner, parts[2], state)
        if len(parts) not in {3, 4} or parts[0] != "lp":
            raise Conflict("Кнопка настройки устарела")
        view = await self.view(owner, state, parts[1])
        command, value = parts[2], parts[3] if len(parts) == 4 else None
        prefix = "lp:" + view["nonce"] + ":"
        product = self.service.product(view["product"], owner) if view.get("product") else None
        if command == "configure":
            if product and product["enabled"]:
                raise Conflict("Сначала остановите выдачу товара. Уже начатые операции не будут повторяться")
            await state.update_data(logical_prepared=None, logical_stock_pending=None)
            rows = [[(label, prefix + "mode:" + handler)] for handler, label in MODES.items()]
            return "⚡ <b>Способ выдачи</b>\nВыберите, что получит покупатель.", keyboard(
                rows, back=("К товару", "lp:item:" + view["item"])
            )
        if command == "editafter":
            if not product or product["enabled"] or product["handler"] == "manual":
                raise Conflict("Сначала остановите выдачу товара и выберите автоматический способ")
            config = json.loads(product["config"])
            if product["handler"] == "telegram_gifts":
                config.setdefault("after", config.get("options", {}).get("after", ""))
            await state.update_data(logical_setup={"handler": product["handler"], "config": config})
            await state.set_state("logical:after")
            return (
                "Введите сообщение после подтверждённой выдачи. Чтобы убрать его, отправьте один дефис: —",
                keyboard([], back=("Отмена", "lp:item:" + view["item"])),
            )
        if command == "mode":
            if value not in MODES:
                raise Conflict("Неизвестный способ выдачи")
            config = json.loads(product["config"]) if product and product["handler"] == value else {}
            if value == "telegram_gifts":
                config.setdefault("after", config.get("options", {}).get("after", ""))
            await state.update_data(logical_setup={"handler": value, "config": config})
            if value in {"telegram_gifts", "fixed_text"}:
                await state.set_state("logical:gift" if value == "telegram_gifts" else "logical:text")
                text = (
                    "🎁 Введите пакет, например <code>gift: heart, heart</code>. Порядок и повторы сохраняются."
                    if value == "telegram_gifts"
                    else "✉️ Введите постоянный текст выдачи. Содержимое будет скрыто в обычных карточках."
                )
                rows = (
                    [[("Посмотреть актуальный каталог", prefix + "catalog")]]
                    if value == "telegram_gifts"
                    else []
                )
                return text, keyboard(rows, back=("Выбрать другой способ", prefix + "configure"))
            return await self.setup_screen(state)
        if command == "catalog":
            gifts = self.service.gift_service()
            gifts.settings.require_configured()
            await gifts.refresh()
            catalog = self.store.rows("SELECT code,stars,available FROM gift_catalog ORDER BY code")
            lines = ["🎁 <b>Актуальный каталог</b>"]
            lines += [
                escaped(gift_name(row["code"]), 80)
                + (f" · {row['stars']} ⭐" if row["available"] else " · недоступен")
                for row in catalog
            ]
            lines.append("Введите нужные коды одним сообщением: gift: heart, heart")
            return "\n".join(lines), keyboard([], back=("К настройкам", prefix + "setup"))
        if command == "setup":
            await state.update_data(logical_prepared=None, logical_stock_pending=None)
            await state.set_state(None)
            return await self.setup_screen(state)
        if command == "input":
            if value not in {"after", "greeting", "order", "day"}:
                raise Conflict("Поле настройки недоступно")
            setup = (await state.get_data()).get("logical_setup", {})
            if setup.get("handler") == "telegram_gifts" and value == "greeting":
                raise Conflict("Для подарков здесь настраивается только сообщение после доставки")
            if value in {"order", "day"}:
                if setup.get("handler") != "telegram_gifts":
                    raise Conflict("Лимит Stars относится только к подаркам")
                await state.set_state("logical:" + value)
                label = "один заказ" if value == "order" else "сутки"
                return (
                    "Введите целый лимит Stars на "
                    + label
                    + ". Он не может превысить общие разрешённые лимиты магазина.",
                    keyboard([], back=("Отмена", prefix + "setup")),
                )
            await state.set_state("logical:" + value)
            text = (
                "Введите сообщение после подтверждённой выдачи."
                if value == "after"
                else "Введите приветствие перед выдачей."
            )
            return text + "\nЧтобы убрать сообщение, отправьте один дефис: —", keyboard(
                [], back=("Отмена", prefix + "setup")
            )
        if command == "stockinput":
            setup = (await state.get_data()).get("logical_setup", {})
            if setup.get("handler") != "unique_stock":
                raise Conflict("Выберите складскую выдачу")
            await state.set_state("logical:stock")
            return (
                "📚 Отправьте уникальные данные по одной единице на строку.\nСначала покажем только количество; содержимое не выводится.",
                keyboard([], back=("Отмена", prefix + "setup")),
            )
        if command == "stock_commit":
            pending = (await state.get_data()).get("logical_stock_pending")
            if not pending or pending["nonce"] != view["nonce"] or pending["item"] != view["item"]:
                raise Conflict("Подтверждение запаса устарело. Подготовьте загрузку заново")
            await Inventory(self.store, self.api).import_values(
                owner, view["item"], pending["text"], pending["preview"], pending["status"]
            )
            view["nonce"] = secrets.token_hex(6)
            await state.update_data(logical_view=view, logical_stock_pending=None)
            await state.set_state(None)
            return await self.setup_screen(state)
        if command == "completion":
            setup = (await state.get_data()).get("logical_setup")
            if not setup:
                raise Conflict("Настройка устарела")
            config = setup["config"]
            key = "auto_fulfill" if setup["handler"] == "telegram_gifts" else "mark_sent"
            config[key] = not config.get(key, False)
            await state.update_data(logical_setup=setup)
            return await self.setup_screen(state)
        if command == "refund":
            setup = (await state.get_data()).get("logical_setup")
            if not setup or setup.get("handler") != "telegram_gifts":
                raise Conflict("Возврат при неверном username относится только к подаркам")
            options = setup["config"].setdefault("options", {})
            options["refund"] = not options.get("refund", False)
            await state.update_data(logical_setup=setup)
            return await self.setup_screen(state)
        if command == "preview":
            setup = (await state.get_data()).get("logical_setup")
            if not setup:
                raise Conflict("Настройки не заполнены")
            prepared = await self.service.prepare(owner, view["item"], setup["handler"], setup["config"])
            view["nonce"] = secrets.token_hex(6)
            await state.update_data(logical_view=view, logical_prepared=prepared)
            await state.set_state(None)
            return self.preview(prepared, view)
        if command == "commit":
            prepared = (await state.get_data()).get("logical_prepared")
            if not prepared or prepared["item_id"] != view["item"]:
                raise Conflict("Превью устарело. Проверьте настройки заново")
            self.service.commit(owner, prepared)
            await state.set_state(None)
            await state.set_data({})
            return (
                "⏳ Изменение описания поставлено в очередь. Выдача пока не включена.\nУспех будет показан после подтверждения Playerok.",
                keyboard(
                    [[("🔄 Проверить результат", "lp:item:" + prepared["item_id"])]],
                    back=("К товару", "item:" + prepared["item_id"]),
                ),
            )
        if not product:
            raise Conflict("Сначала подтвердите привязку товара")
        if command == "reconcile":
            await self.service.reconcile_binding(owner, product["id"])
            return await self.card(owner, view["item"], state)
        if command == "arm":
            view["nonce"] = secrets.token_hex(6)
            view["confirm"] = "enable"
            await state.update_data(logical_view=view)
            prefix = "lp:" + view["nonce"] + ":"
            return (
                "▶️ <b>Включить выдачу будущих заказов?</b>\nПроверим объявление и настройки. Старые заказы не запускаются.\nНаследование разрешает применять эти настройки к проверенным перевыставлениям с тем же кодом; конфликтующие копии блокируются.",
                keyboard(
                    [
                        [("Включить только это объявление", prefix + "enable:this")],
                        [("Включить и наследовать перевыставления", prefix + "enable:inherit")],
                    ],
                    back=("Отмена", "lp:item:" + view["item"]),
                ),
            )
        if command == "enable":
            if value not in {"this", "inherit"} or view.get("confirm") != "enable":
                raise Conflict("Подтверждение включения устарело")
            await self.service.arm(
                owner, product["id"], view["revision"], inherit=value == "inherit", item_id=view["item"]
            )
            return await self.card(owner, view["item"], state)
        if command == "stop":
            self.service.stop(owner, product["id"])
            await state.clear()
            return (
                "⛔ Новые выдачи товара остановлены. Результаты уже отправленных запросов сохраняются; неизвестные операции не повторяются.",
                keyboard([[("К настройке товара", "lp:item:" + view["item"])]]),
            )
        if command == "detach":
            view["nonce"] = secrets.token_hex(6)
            view["confirm"] = "detach"
            await state.update_data(logical_view=view)
            prefix = "lp:" + view["nonce"] + ":"
            return (
                "Отвязать товар?\nВыдача будет остановлена для всех его объявлений. История, запас и квитанции сохранятся. Код останется в описании, но не разрешит новую выдачу.",
                keyboard(
                    [[("Подтвердить отвязку", prefix + "detach_confirm")]],
                    back=("Отмена", "lp:item:" + view["item"]),
                ),
            )
        if command == "detach_confirm":
            if view.get("confirm") != "detach":
                raise Conflict("Сначала откройте и подтвердите отвязку товара")
            self.service.detach(owner, product["id"], view["revision"])
            await state.clear()
            return "Привязка отключена. История сохранена; объявление Playerok не удалялось.", keyboard(
                [[("К товару", "item:" + view["item"])]]
            )
        if command == "links":
            page = int(value or "0")
            if not 0 <= page <= 100000:
                raise Conflict("Некорректная страница")
            links = self.store.rows(
                "SELECT item_id,server_status,verified_at FROM logical_listings WHERE product_id=? ORDER BY verified_at DESC,item_id DESC LIMIT 11 OFFSET ?",
                (product["id"], page * 10),
            )
            lines = ["🔗 <b>Связанные объявления</b>", escaped(product["code"], 20)]
            lines += [
                escaped(row["item_id"], 80)
                + " · "
                + status(row["server_status"], "item")
                + (" · проверено" if row["verified_at"] else " · не проверено")
                for row in links[:10]
            ]
            rows = [[("Открыть объявление", "item:" + row["item_id"])] for row in links[:10]]
            paging = []
            if page:
                paging.append(("⬅️", prefix + "links:" + str(page - 1)))
            if len(links) > 10:
                paging.append(("➡️", prefix + "links:" + str(page + 1)))
            if paging:
                rows.append(paging)
            return "\n".join(lines), keyboard(
                rows,
                back=("К товару", "lp:item:" + view["item"]),
            )
        if command == "orders":
            page = int(value or "0")
            if not 0 <= page <= 100000:
                raise Conflict("Некорректная страница")
            orders = self.store.rows(
                "SELECT o.* FROM orders o JOIN logical_order_scopes s ON s.order_id=o.id WHERE s.product_id=? ORDER BY o.created_at DESC,o.id DESC LIMIT 11 OFFSET ?",
                (product["id"], page * 10),
            )
            rows = [[(order_label(self.store, row["id"]), "order:" + row["id"])] for row in orders[:10]]
            paging = []
            if page:
                paging.append(("⬅️", prefix + "orders:" + str(page - 1)))
            if len(orders) > 10:
                paging.append(("➡️", prefix + "orders:" + str(page + 1)))
            if paging:
                rows.append(paging)
            return "🛒 <b>История этого товара</b>\n" + (
                "Выберите заказ." if orders else "Подтверждённых привязанных заказов пока нет."
            ), keyboard(rows, back=("К товару", "lp:item:" + view["item"]))
        if command == "check":
            account = self.service.authority(owner)
            key = "logical:scan:" + account
            before = self.store.setting(key + ":completed", {})
            await LogicalProductWorker(self.store, self.api).once()
            after = self.store.setting(key + ":completed", {})
            if after and before != after:
                note = "Сверка каталога завершена."
            elif self.store.setting(key, {}):
                note = "Каталог ещё загружается. Наследование непроверенных объявлений не разрешено."
            else:
                note = "Полная сверка не запускалась: для неё нужно включённое наследование. Текущее объявление прочитано заново."
            text, markup = await self.card(owner, view["item"], state)
            return note + "\n\n" + text, markup
        raise Conflict("Кнопка настройки устарела. Откройте карточку товара")

    async def setup_screen(self, state):
        data = await state.get_data()
        view, setup = data.get("logical_view"), data.get("logical_setup")
        if not view or not setup:
            raise Conflict("Настройки устарели. Откройте товар заново")
        handler, config = setup["handler"], setup["config"]
        prefix = "lp:" + view["nonce"] + ":"
        text = "⚙️ <b>Настройки выдачи</b>\nСпособ: " + MODES[handler]
        rows = []
        if handler == "telegram_gifts":
            text += "\nПакет: " + escaped(config.get("code") or "не задан", 180)
            text += "\nЗавершение: " + (
                "автоматически, если допускает Playerok" if config.get("auto_fulfill") else "вручную"
            )
            rows += [
                [("Изменить пакет", prefix + "mode:telegram_gifts")],
                [("Способ завершения", prefix + "completion")],
                [("Лимит на заказ", prefix + "input:order"), ("Лимит в сутки", prefix + "input:day")],
                [("Возврат при неверном username", prefix + "refund")],
            ]
            options = config.get("options", {})
            text += "\nВозврат при неверном username: " + (
                "разрешён" if options.get("refund", False) else "выключен"
            )
            text += "\nДля возврата также нужно глобальное разрешение; после начала оплаты подарка возврат всего заказа запрещён."
            text += (
                "\nЛимиты товара: "
                + (str(options["order"]) + " ⭐ на заказ" if options.get("order") else "общий лимит заказа")
                + " · "
                + (str(options["day"]) + " ⭐ в сутки" if options.get("day") else "общий суточный лимит")
            )
        elif handler == "fixed_text":
            text += f"\nТекст: {len(config.get('text', ''))} символов; содержимое скрыто"
            rows.append([("Изменить текст", prefix + "mode:fixed_text")])
        elif handler == "unique_stock":
            text += (
                f"\nДоступно на складе: {Inventory(self.store, self.api).counts(view['item'])['AVAILABLE']}"
            )
            rows.append([("📚 Загрузить запас", prefix + "stockinput")])
        if handler in {"fixed_text", "unique_stock"}:
            text += "\nЗавершение: вручную" + ("; напоминание включено" if config.get("mark_sent") else "")
            rows += [
                [("Приветствие", prefix + "input:greeting")],
                [("Напоминать о завершении", prefix + "completion")],
            ]
        if handler != "manual":
            text += "\nСообщение после выдачи: " + (
                "сохранено"
                if config.get("after")
                else "стандартное"
                if handler == "telegram_gifts"
                else "нет"
            )
            rows.append([("Сообщение после выдачи", prefix + "input:after")])
        rows.append([("🔎 Проверить и посмотреть превью", prefix + "preview")])
        return text, keyboard(rows, back=("Другой способ выдачи", prefix + "configure"))

    def preview(self, prepared, view):
        config = prepared["config"]
        text = "🔎 <b>Подтверждение привязки</b>\nКод: " + escaped(prepared["code"], 20)
        text += "\nСпособ: " + MODES[prepared["handler"]]
        if prepared["handler"] == "telegram_gifts":
            text += "\n" + escaped(gift_summary(config["bundle"]), 240)
            text += f"\nПроверенная стоимость: {sum(g['stars'] for g in config['bundle'])} ⭐"
            per_order, daily = self.service.gift_service().limits(view["item"])
            per_order = config.get("options", {}).get("order", per_order)
            daily = config.get("options", {}).get("day", daily)
            text += f"\nЛимиты: {per_order} ⭐ на заказ · {daily} ⭐ в сутки"
            text += "\nВозврат при неверном username: " + (
                "разрешён при глобальном разрешении"
                if config.get("options", {}).get("refund", False)
                else "выключен"
            )
        elif prepared["handler"] == "unique_stock":
            text += f"\nЗапас: {Inventory(self.store, self.api).counts(view['item'])['AVAILABLE']} доступно"
        elif prepared["handler"] == "fixed_text":
            text += f"\nТекст выдачи сохранён: {len(config['text'])} символов"
        description = marked_description(prepared["cleaned"], prepared["code"])
        text += "\n\nОписание после изменения:\n<blockquote>" + escaped(description, 2050) + "</blockquote>"
        text += "\nНе редактируйте объявление одновременно. Изменившиеся после проверки данные остановят запись.\nПодтверждение изменит описание, но не включит выдачу и не разрешит расходы Stars."
        return text, keyboard(
            [[("✅ Подтвердить изменение описания", "lp:" + view["nonce"] + ":commit")]],
            back=("Исправить настройки", "lp:" + view["nonce"] + ":setup"),
        )

    async def message(self, message, state):
        current = await state.get_state()
        data = await state.get_data()
        view, setup = data.get("logical_view"), data.get("logical_setup")
        if (
            not view
            or not setup
            or current
            not in {
                "logical:gift",
                "logical:text",
                "logical:after",
                "logical:greeting",
                "logical:order",
                "logical:day",
                "logical:stock",
            }
        ):
            raise Conflict("Ввод устарел. Откройте настройку выдачи заново")
        await self.view(message.from_user.id, state, view["nonce"])
        value = message.text
        if current == "logical:stock":
            inventory = Inventory(self.store, self.api)
            item = await inventory.item(message.from_user.id, view["item"])
            preview = inventory.preview(view["item"], value)
            view["nonce"] = secrets.token_hex(6)
            await state.update_data(
                logical_view=view,
                logical_stock_pending={
                    "nonce": view["nonce"],
                    "item": view["item"],
                    "text": value,
                    "preview": preview,
                    "status": item["status"],
                },
            )
            await state.set_state("logical:stockconfirm")
            return (
                f"📚 Сохранить запас?\nНовых единиц: {preview['new']}\nПустых строк: {preview['empty']} · повторов в сообщении: {preview['duplicates']}\nУже известны складу: {preview['known']}\nСодержимое скрыто.",
                keyboard(
                    [[("✅ Сохранить запас", "lp:" + view["nonce"] + ":stock_commit")]],
                    back=("Отмена", "lp:" + view["nonce"] + ":setup"),
                ),
            )
        if current in {"logical:order", "logical:day"}:
            if (
                not isinstance(value, str)
                or not value.strip().isdigit()
                or not 1 <= int(value.strip()) <= 1_000_000
            ):
                raise Conflict("Введите положительный целый лимит Stars")
            setup["config"].setdefault("options", {})[current.split(":")[1]] = int(value.strip())
        elif current == "logical:gift":
            if not isinstance(value, str):
                raise Conflict("Отправьте коды подарков текстом")
            setup["config"]["code"] = gift_code_from_description(
                value if value.strip().lower().startswith("gift:") else "gift: " + value,
                max_count=self.service.gift_service().settings.max_gifts,
            )
        elif current == "logical:text":
            setup["config"]["text"] = message_text(value, required=True)
        else:
            setup["config"][current.split(":")[1]] = "" if value in {"-", "—"} else message_text(value)
        await state.update_data(logical_setup=setup)
        await state.set_state(None)
        return await self.setup_screen(state)
