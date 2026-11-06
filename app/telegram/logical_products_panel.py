"""Owner setup of stable product identities in the existing navigation panel."""

import json
import secrets
from collections import Counter

from app.database.store import Conflict
from app.gifts.core import gift_code_from_description, gift_codes, gift_name
from app.services.automation import message_text
from app.services.inventory import Inventory
from app.services.logical_products import PROBLEMS, LogicalProducts
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

    async def card(self, owner, item_id, state, *, details=False):
        self.owner(owner)
        from app.services.product_readiness import ProductReadiness

        item = await ProductReadiness(self.store, self.api).ui_item(owner, item_id)
        account = self.service.authority(owner)
        if item.get("id") != item_id or (item.get("user") or {}).get("id") != account:
            raise Conflict("Владелец объявления не подтверждён")
        if not item.get("_ui_cached"):
            await self.service.refresh_title(item)
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
        if item.get("_ui_cached"):
            text += "\n🕒 Сохранённые данные: " + local_time(item.get("_ui_observed_at"))
            if item.get("_ui_stale"):
                text += " · обновляются в фоне"
        rows = []
        if product:
            config = json.loads(product["config"])
            platform = config.get("platform_only") is True
            text += "\nПостоянный код: " + (
                escaped(product["code"], 3) if len(product["code"]) == 3 else "требуется перенос"
            )
            text += "\nСпособ: " + ("📦 Выдаёт Playerok" if platform else MODES[product["handler"]])
            text += "\nРазрешение выдачи: " + ("включено" if product["enabled"] else "выключено")
            if platform:
                text += "\nБот не отправляет товар повторно."
            elif product["handler"] == "telegram_gifts":
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
                rows.append([("🔐 Перенести привязку в [XXX]", prefix + "migrate")])
            elif product["state"] == "PENDING":
                operation = self.store.rows(
                    "SELECT status FROM outbox WHERE id=?", (product["operation_id"],)
                )
                outcome = operation[0]["status"] if operation else None
                text += "\nИзменение лота: " + status(outcome)
                if outcome == "UNCERTAIN":
                    rows.append([("🔎 Проверить изменение без повторной отправки", prefix + "reconcile")])
                elif outcome == "PERMANENT_FAILED":
                    text += "\nПривязка не завершена. Обновите настройки и подтвердите новое превью."
            elif product["state"] == "DETACHED":
                text += "\nПривязка отключена. Публичный код не разрешает выдачу."
            else:
                try:
                    if not item.get("_ui_stale"):
                        self.service.verify_listing(product, item)
                except ValueError as exc:
                    text += "\n⚠️ " + escaped(str(exc), 300)
                else:
                    text += (
                        "\nСохранённая привязка товара"
                        if item.get("_ui_stale")
                        else "\nКод товара подтверждён Playerok"
                    )
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
            if (
                (not product["enabled"] or not active_projection)
                and product["state"] == "VERIFIED"
                and product["handler"] != "manual"
                and not platform
            ):
                rows.append([("▶️ Включить выдачу", prefix + "arm")])
            rows += [
                [("Изменить настройки", "platform:item:" + item_id if platform else prefix + "configure")],
                [("Связанные объявления", prefix + "links"), ("История заказов", prefix + "orders:0")],
                [("🔄 Проверить перевыставление", prefix + "check")],
                [("Отвязать товар", prefix + "detach")],
            ]
            if not product["enabled"] and product["state"] == "VERIFIED" and not product["verified_at"]:
                rows.insert(0, [("🔐 Подтвердить изменённый товар", prefix + "rebind")])
            if self.service.title_change_allowed(product, item):
                rows.insert(0, [("🔐 Подтвердить новое название", prefix + "title")])
            if platform:
                rows.append([("Сменить способ выдачи", prefix + "configure")])
            elif product["handler"] != "manual":
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
            text += "\nНастройте выдачу один раз и подтвердите постоянный код [XXX] в названии."
            rows.append([("Настроить выдачу", prefix + "configure")])
        if product and product["handler"] == "unique_stock":
            rows.insert(0, [("📚 Склад", "stock:view:" + item_id)])
        rows.append([("🔄 Изменить способ выдачи", "lp:method:" + item_id)])
        if (
            product
            and product["handler"] in {"unique_stock", "fixed_text"}
            and not json.loads(product["config"]).get("platform_only")
        ):
            rows.append([("Проверить конкретный заказ без отправки", "auto:check:" + item_id)])
        from app.services.product_readiness import ProductReadiness
        from app.telegram.product_presentation import STATES

        readiness_service = ProductReadiness(self.store, self.api)
        readiness_service.remember_item(item)
        readiness = readiness_service.snapshot(item_id, item=item)
        text += "\n" + STATES[readiness["state"]]
        if readiness["problems"]:
            text += "\n" + "\n".join("• " + escaped(p["text"], 180) for p in readiness["problems"][:6])
        rows.append([("Подробности готовности", "rd:check:" + item_id)])
        if not details:
            from app.telegram.product_presentation import brief

            text = "📦 <b>Выдача</b>\n" + escaped(view["title"], 120) + "\n" + brief(readiness)
            if product and product["handler"] == "telegram_gifts":
                text += "\n🎁 " + escaped(gift_summary(config.get("bundle", [])), 220)
                text += f" · {sum(g['stars'] for g in config.get('bundle', []))} ⭐"
            if readiness.get("stock"):
                text += f"\nОстаток: {readiness['stock']['AVAILABLE']} · в резерве: {readiness['stock']['RESERVED']}"
            if product and product["handler"] != "manual":
                text += "\n💬 " + escaped(readiness["message"], 150)
                text += "\n✅ Завершение: " + (
                    readiness.get("completion_note")
                    or ("после доставки, если не нужен отчёт" if readiness["complete"] else "вручную")
                )
            keep = {"stop", "arm", "configure", "editafter", "migrate", "reconcile", "rebind", "title"}
            rows = [
                [
                    (label, route)
                    for label, route in row
                    if route.split(":")[-1] in keep or route.startswith("stock:view:")
                ]
                for row in rows
            ]
            rows = [row for row in rows if row]
            if not product:
                rows = []
            rows.append(
                [("🔄 Изменить способ выдачи" if product else "Настроить выдачу", "lp:method:" + item_id)]
            )
            problem = next((p for p in readiness["problems"] if p["terminal"]), None)
            if problem and problem["action"][1] not in {"lp:item:" + item_id, "iv:delivery:" + item_id}:
                rows.insert(0, [tuple(problem["action"])])
            rows.append([("Подробности и история", "lp:details:" + item_id)])
        return text, keyboard(rows, back=("⬅️ К товару", "item:" + item_id))

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

    async def readiness(self, owner, product, item_id):
        from app.services.product_readiness import ProductReadiness

        result = await ProductReadiness(self.store, self.api).check(owner, item_id, for_arm=True)
        return [p for p in result["problems"] if p["blocking"]]

    async def callback(self, action, owner, state):
        if action.startswith("lp:details:"):
            return await self.card(owner, action.split(":", 2)[2], state, details=True)
        parts = action.split(":")
        if len(parts) == 3 and parts[1] == "title":
            await self.card(owner, parts[2], state)
            view = (await state.get_data())["logical_view"]
            return await self.callback("lp:" + view["nonce"] + ":title", owner, state)
        if len(parts) == 3 and parts[1] in {"method", "messages", "completion", "limits"}:
            await self.card(owner, parts[2], state)
            view = (await state.get_data())["logical_view"]
            product = self.service.product(view["product"], owner) if view.get("product") else None
            prefix = "lp:" + view["nonce"] + ":"
            if parts[1] == "limits":
                if not product or product["handler"] != "telegram_gifts":
                    raise Conflict("Лимиты Stars настраиваются только у подарочного товара")
                if product["enabled"]:
                    return (
                        "Для изменения согласованных лимитов этого товара остановите новые покупки. Уже начатые операции и их резервы сохраняются. Затем откройте лимиты заново.",
                        keyboard(
                            [[("⛔ Остановить выдачу", prefix + "stop")]],
                            back=("⬅️ К товару", "item:" + parts[2]),
                        ),
                    )
                await state.update_data(
                    logical_setup={"handler": product["handler"], "config": json.loads(product["config"])}
                )
                return await self.setup_screen(state)
            if parts[1] == "method":
                rows = [[(label, prefix + "mode:" + handler)] for handler, label in MODES.items()]
                rows.append([("📦 Выдаёт Playerok", "platform:item:" + parts[2])])
                if product and product["enabled"]:
                    rows.insert(0, [("⛔ Остановить текущую выдачу", prefix + "stop")])
                return (
                    "🔄 <b>Способ выдачи</b>\nДля изменения активной выдачи сначала остановите её. Смена на Playerok проверяется отдельно при сохранении.",
                    keyboard(rows, back=("⬅️ К товару", "item:" + parts[2])),
                )
            if product and json.loads(product["config"]).get("platform_only"):
                from app.telegram.platform_panel import PlatformPanel

                return await PlatformPanel(self.store, self.api).callback(
                    "platform:item:" + parts[2], owner, state
                )
            if not product or product["handler"] == "manual":
                return await self.callback(prefix + "configure", owner, state)
            if parts[1] == "messages":
                return await self.callback(prefix + "editafter", owner, state)
            if product["enabled"]:
                config = json.loads(product["config"])
                key = "auto_fulfill" if product["handler"] == "telegram_gifts" else "automatic_completion"
                self.service.update_after(
                    owner, parts[2], product, not config.get(key, False), field="completion"
                )
                return await self.card(owner, parts[2], state)
            await state.update_data(
                logical_setup={"handler": product["handler"], "config": json.loads(product["config"])}
            )
            return await self.setup_screen(state)
        if len(parts) == 3 and parts[0] == "lp" and parts[1] in {"item", "ready"}:
            result = await self.card(owner, parts[2], state)
            view = (await state.get_data())["logical_view"]
            if parts[1] == "ready" and view.get("product"):
                product = self.service.product(view["product"], owner)
                if product["handler"] != "manual":
                    return await self.callback("lp:" + view["nonce"] + ":arm", owner, state)
            return result
        if len(parts) not in {3, 4} or parts[0] != "lp":
            raise Conflict("Кнопка настройки устарела")
        view = await self.view(owner, state, parts[1])
        command, value = parts[2], parts[3] if len(parts) == 4 else None
        prefix = "lp:" + view["nonce"] + ":"
        product = self.service.product(view["product"], owner) if view.get("product") else None
        if command == "title":
            prepared = await self.service.prepare_title_change(owner, view["item"])
            await state.update_data(logical_title=prepared)
            config = json.loads(product["config"])
            gift = (
                "\nОтправляться будет: "
                + escaped(gift_summary(config.get("bundle", [])), 250)
                + f" · {sum(g['stars'] for g in config.get('bundle', []))} ⭐."
                if product["handler"] == "telegram_gifts"
                else ""
            )
            return (
                "🔐 <b>Подтвердить новое название</b>\nБыло: "
                + escaped(prepared["old_title"], 200)
                + "\nСтало: "
                + escaped(prepared["title"], 200)
                + "\nКод, описание, категория и поля получения совпадают с привязкой."
                + gift
                + "\nНастроенный подарок не меняется. Подтверждайте только если он соответствует новому названию."
                + "\nМеняется только подтверждённое название в боте. Разрешения и история сохраняются; новые отправки по-прежнему проходят все проверки.",
                keyboard(
                    [
                        [("✅ Подтвердить новое название", prefix + "title_apply")],
                        [("Остановить и открыть настройки", prefix + "stopconfigure")],
                    ],
                    back=("Отмена", "lp:item:" + view["item"]),
                ),
            )
        if command == "title_apply":
            prepared = (await state.get_data()).get("logical_title")
            if not prepared or prepared.get("item") != view["item"]:
                raise Conflict("Подтверждение названия устарело. Откройте проверку заново")
            await self.service.confirm_title_change(owner, prepared)
            await state.update_data(logical_title=None)
            from app.services.product_readiness import ProductReadiness

            # This is a fresh read-only check, NOT arm(). It also rechecks the
            # full catalogue before clearing an existing relist-scan blocker.
            await ProductReadiness(self.store, self.api).check(owner, view["item"], for_arm=True)
            return await self.card(owner, view["item"], state)
        if command == "stopconfigure":
            if not product:
                raise Conflict("Сначала откройте актуальную настройку товара")
            self.service.stop(owner, product["id"])
            return await self.callback(prefix + "configure", owner, state)
        if command == "rebind":
            if not product or product["enabled"]:
                return await self.callback(prefix + "configure", owner, state)
            from app.services.products import Products

            Products(self.store, self.api).require_critical_edit(view["item"])
            prepared = await self.service.prepare(
                owner,
                view["item"],
                product["handler"],
                json.loads(product["config"]),
                platform_binding=json.loads(product["config"]).get("platform_only") is True,
            )
            view["nonce"] = secrets.token_hex(6)
            await state.update_data(logical_view=view, logical_prepared=prepared)
            return self.preview(prepared, view)
        if command == "migrate":
            prepared = await self.service.prepare_legacy_description(owner, view["item"])
            view["nonce"] = secrets.token_hex(6)
            await state.update_data(logical_view=view, logical_prepared=prepared)
            return self.preview(prepared, view)
        if command == "configure":
            if product and product["enabled"]:
                return (
                    (
                        "Разрешение выдачи включено, но проверка привязки может временно блокировать отправку. "
                        "Для изменения подарка или способа выдачи остановите новые отправки. "
                        "Уже начатые операции и история сохраняются и не повторяются."
                    ),
                    keyboard(
                        [[("⛔ Остановить и открыть настройки", prefix + "stopconfigure")]],
                        back=("К товару", "lp:item:" + view["item"]),
                    ),
                )
            await state.update_data(logical_prepared=None, logical_stock_pending=None)
            rows = [[(label, prefix + "mode:" + handler)] for handler, label in MODES.items()]
            return "⚡ <b>Способ выдачи</b>\nВыберите, что получит покупатель.", keyboard(
                rows, back=("К товару", "lp:item:" + view["item"])
            )
        if command in {"editafter", "editgreeting"}:
            if not product or product["handler"] == "manual":
                raise Conflict("Сначала подтвердите автоматический способ выдачи")
            if command == "editgreeting" and product["handler"] not in {"unique_stock", "fixed_text"}:
                raise Conflict("Приветствие до выдачи относится только к тексту и складу")
            await state.update_data(
                logical_message_edit={
                    key: product[key] for key in ("revision", "control_revision", "config")
                },
                logical_message_field="greeting" if command == "editgreeting" else "after",
            )
            await state.set_state("logical:notice")
            return (
                (
                    "Введите приветствие перед выдачей"
                    if command == "editgreeting"
                    else "Введите сообщение после подтверждённой выдачи"
                )
                + " для будущих покупок. Уже зарегистрированные заказы сохранят свой текст. "
                + (
                    "Чтобы вернуть стандартное уведомление, отправьте один дефис: —"
                    if product["handler"] == "telegram_gifts"
                    else "Чтобы убрать сообщение, отправьте один дефис: —"
                ),
                keyboard(
                    [[("Приветствие перед выдачей", prefix + "editgreeting")]]
                    if command == "editafter" and product["handler"] in {"unique_stock", "fixed_text"}
                    else [],
                    back=("Отмена", "lp:item:" + view["item"]),
                ),
            )
        if command == "mode":
            if product and product["enabled"]:
                return await self.callback(prefix + "configure", owner, state)
            if value not in MODES:
                raise Conflict("Неизвестный способ выдачи")
            config = json.loads(product["config"]) if product and product["handler"] == value else {}
            config.pop("platform_only", None)
            if not product or product["handler"] != value:
                if value == "telegram_gifts":
                    config["auto_fulfill"] = True
                elif value in {"unique_stock", "fixed_text"}:
                    config.update(mark_sent=True, automatic_completion=True)
            if value == "telegram_gifts":
                config.setdefault("after", config.get("options", {}).get("after", ""))
            await state.update_data(logical_setup={"handler": value, "config": config})
            if value == "telegram_gifts":
                await state.set_state("logical:gift")
                await state.update_data(
                    logical_gift_selection=list(gift_codes(config["code"])) if config.get("code") else []
                )
                return await self.callback(prefix + "catalog", owner, state)
            if value == "fixed_text":
                await state.set_state("logical:text")
                return (
                    "✉️ Введите постоянный текст выдачи. Содержимое будет скрыто в обычных карточках.",
                    keyboard([], back=("Выбрать другой способ", prefix + "configure")),
                )
            return await self.setup_screen(state)
        if command == "catalog":
            gifts = self.service.gift_service()
            gifts.settings.require_configured()
            await gifts.refresh()
            return await self.gift_picker(state, prefix)
        if command in {"giftpick", "giftundo", "giftclear", "giftdone"}:
            data = await state.get_data()
            setup = data.get("logical_setup", {})
            if setup.get("handler") != "telegram_gifts":
                raise Conflict("Сначала выберите подарочную выдачу")
            selected = list(data.get("logical_gift_selection", []))
            if command == "giftpick":
                if len(selected) >= self.service.gift_service().settings.max_gifts:
                    raise Conflict("Достигнуто максимальное количество подарков в заказе")
                self.service.gift_service().catalog_bundle(value)
                selected.append(value)
            elif command == "giftundo":
                selected = selected[:-1]
            elif command == "giftclear":
                selected = []
            else:
                if not selected:
                    raise Conflict("Выберите хотя бы один подарок")
                setup["config"]["code"] = ", ".join(selected)
                await state.update_data(logical_setup=setup)
                await state.set_state(None)
                return await self.setup_screen(state)
            await state.update_data(logical_gift_selection=selected)
            return await self.gift_picker(state, prefix)
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
            if setup["handler"] != "telegram_gifts":
                config["automatic_completion"] = config[key]
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
            problems = await self.readiness(owner, product, view["item"])
            if problems:
                return "🔴 <b>Запуск пока невозможен</b>\n" + "\n".join(
                    "• " + escaped(reason["text"], 400) for reason in problems
                ), keyboard(
                    [
                        *[[p["action"]] for p in problems],
                        *(
                            [[("⛔ Остановить выдачу для изменения настроек", prefix + "stop")]]
                            if product["enabled"]
                            else []
                        ),
                        [("Изменить настройки", prefix + "configure")],
                    ],
                    back=("К товару", "item:" + view["item"]),
                )
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

    async def gift_picker(self, state, prefix):
        entries = self.store.rows("SELECT code,stars FROM gift_catalog WHERE available=1 ORDER BY code")
        chosen = (await state.get_data()).get("logical_gift_selection", [])
        prices = {g["code"]: g["stars"] for g in entries}
        text = "🎁 <b>Выберите подарки</b>\nКаждое нажатие добавляет один подарок."
        if chosen:
            text += "\n" + escaped(", ".join(gift_name(code) for code in chosen), 600)
            if any(code not in prices for code in chosen):
                text += "\nВыбранный подарок недоступен. Измените состав."
            else:
                text += f"\nКоличество: {len(chosen)} · {sum(prices[code] for code in chosen)} ⭐ по последней проверке"
        buttons = [
            (f"{gift_name(g['code'])} · {g['stars']} ⭐", prefix + "giftpick:" + g["code"]) for g in entries
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        if chosen:
            rows += [
                [("Убрать последний", prefix + "giftundo"), ("Очистить", prefix + "giftclear")],
                [("✅ Сохранить выбор", prefix + "giftdone")],
            ]
        return text, keyboard(rows, back=("К настройкам", prefix + "setup"))

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
            text += "\nПодарки: " + escaped(
                ", ".join(gift_name(code) for code in gift_codes(config["code"]))
                if config.get("code")
                else "не выбраны",
                300,
            )
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
            automatic = config.get("automatic_completion", False)
            text += "\nЗавершение: " + (
                "автоматически после доставки, если не нужен отчёт"
                if automatic and config.get("mark_sent")
                else "вручную" + ("; напоминание включено" if config.get("mark_sent") else "")
            )
            rows += [
                [("Приветствие", prefix + "input:greeting")],
                [
                    (
                        "Автоматическая отметка выполнения" if automatic else "Напоминать о завершении",
                        prefix + "completion",
                    )
                ],
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
            local_day = config.get("options", {}).get("day")
            text += f"\nЛимиты: {per_order} ⭐ на заказ"
            if daily is not None:
                text += f" · {min(daily, local_day) if local_day is not None else daily} ⭐ в сутки"
            text += "\nВозврат при неверном username: " + (
                "разрешён при глобальном разрешении"
                if config.get("options", {}).get("refund", False)
                else "выключен"
            )
        elif prepared["handler"] == "unique_stock":
            text += f"\nЗапас: {Inventory(self.store, self.api).counts(view['item'])['AVAILABLE']} доступно"
        elif prepared["handler"] == "fixed_text":
            text += f"\nТекст выдачи сохранён: {len(config['text'])} символов"
        name = prepared["name"]
        text += "\n\nНазвание после изменения:\n<blockquote>" + escaped(name, 150) + "</blockquote>"
        text += "\nОписание:\n<blockquote>" + escaped(prepared["cleaned"], 1800) + "</blockquote>"
        text += "\nНе редактируйте объявление одновременно. Изменившиеся после проверки данные остановят запись.\nПодтверждение сохранит код в названии, но не включит выдачу и не разрешит расходы Stars."
        return text, keyboard(
            [[("✅ Подтвердить привязку", "lp:" + view["nonce"] + ":commit")]],
            back=("Исправить настройки", "lp:" + view["nonce"] + ":setup"),
        )

    async def message(self, message, state):
        current = await state.get_state()
        data = await state.get_data()
        if current == "logical:notice":
            view = data.get("logical_view", {})
            await self.view(message.from_user.id, state, view.get("nonce"))
            value = message.text
            if not isinstance(value, str):
                raise Conflict("Отправьте сообщение текстом")
            self.service.update_after(
                message.from_user.id,
                view["item"],
                data.get("logical_message_edit", {}),
                "" if value.strip() in {"-", "—"} else value,
                field=data.get("logical_message_field", "after"),
            )
            await state.set_state(None)
            await state.update_data(logical_message_edit=None)
            text, markup = await self.card(message.from_user.id, view["item"], state)
            return "✅ Сообщение для будущих покупок сохранено.\n\n" + text, markup
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
