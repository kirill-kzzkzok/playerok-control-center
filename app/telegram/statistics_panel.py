import asyncio
import secrets

from aiogram.types import BufferedInputFile

from app.rendering.charts import revenue_chart
from app.services.finance import checked_time
from app.services.order_finance import profit_statistics, settlement_statistics
from app.services.statistics import confirm_offer_group, grouped_statistics, offer_identities
from app.services.transactions import transaction_statistics
from app.telegram.formatting import escaped
from app.telegram.keyboards import keyboard

VIEWS = {
    "overview": "Выручка/категории",
    "time": "Продажи во времени",
    "average": "Средний чек",
    "promotion": "Расходы",
    "comparison": "Сравнение",
}


class StatisticsPanel:
    def __init__(self, store):
        self.store = store

    def markup(self, days, *, details=False, expanded=False):
        rows = [
            [
                ("24 ч", "stats:1"),
                ("48 ч", "stats:2"),
                ("7 дн", "stats:7"),
                ("30 дн", "stats:30"),
                ("Всё", "stats:0"),
            ]
        ]
        if not details and not expanded:
            rows += [
                [("📈 Графики", f"sr:{days}:charts"), ("🏆 Лучшие товары", f"sr:{days}:top")],
                [("💰 Финансы", f"sr:{days}:finance"), ("📋 Подробный отчёт", f"sd:{days}:details")],
            ]
            return keyboard(rows, back=("⬅️ Ещё", "more"))
        # Historical deep links remain valid; detailed reports return to this summary.
        rows += [
            [("Графики", f"sr:{days}:charts"), ("Лучшие товары", f"sr:{days}:top")],
            [("Финансы", f"sr:{days}:finance"), ("Как рассчитывается", f"sd:{days}:details")],
        ]
        return keyboard(rows, back=("⬅️ К статистике", f"stats:{days}"))

    async def handle(self, action, message, *, owner=None, state=None):
        parts = action.split(":")
        days = int(parts[1])
        if parts[0] == "sg":
            account = self.store.setting("playerok:account_id")
            if not account:
                raise ValueError("Сначала подтвердите подключённый аккаунт в профиле")
            if parts[2] == "sources":
                grouped = grouped_statistics(self.store, owner, account, days)
                members = grouped["offer_sources"].get("group:" + parts[3])
                if not members:
                    raise ValueError("Объединение изменилось. Откройте рейтинг заново")
                page = int(parts[4]) if len(parts) == 5 else 0
                if page < 0 or page * 20 >= len(members):
                    raise ValueError("Страница исходных объявлений устарела")
                names = offer_identities(self.store, account)
                text = "🔗 <b>Исходные объявления группы</b>\n" + "\n".join(
                    escaped(names[ident]["name"], 80) + " · <code>" + escaped(ident, 100) + "</code>"
                    for ident in members[page * 20 : page * 20 + 20]
                )
                rows = []
                if page:
                    rows.append([("⬅️ Объявления", f"sg:{days}:sources:{parts[3]}:{page - 1}")])
                if (page + 1) * 20 < len(members):
                    rows.append([("Объявления ➡️", f"sg:{days}:sources:{parts[3]}:{page + 1}")])
                return await message.answer(
                    text, reply_markup=keyboard(rows, back=("К рейтингу", f"sr:{days}:top"))
                )
            if parts[2] == "commit":
                pending = (await state.get_data()).get("offer_group", {})
                if pending.get("nonce") != parts[3] or pending.get("account") != account:
                    raise ValueError("Подтверждение объединения устарело")
                confirm_offer_group(
                    self.store, owner, account, pending["members"], expected=pending["expected"]
                )
                await state.clear()
                return await message.answer(
                    "✅ Предложения объединены только для аналитики. История заказов сохранена.",
                    reply_markup=keyboard([[("Рейтинг товаров", f"sr:{days}:top")]]),
                )
            if parts[2] == "reset":
                # Local projection only, never changes an order or money ledger.
                self.store.set_setting(f"analytics:{owner}:{account}:offer_groups", [])
                return await message.answer(
                    "Объединения аналитики отменены. Исходные объявления показаны отдельно.",
                    reply_markup=keyboard([[("Рейтинг товаров", f"sr:{days}:top")]]),
                )
            values = offer_identities(self.store, account)
            ids = sorted(values)
            page = int(parts[3]) if len(parts) == 4 else 0
            if page < 0 or page * 20 > len(ids):
                raise ValueError("Страница предложений устарела")
            await state.set_state("statsgroup:choose")
            await state.set_data({"group_candidates": ids, "group_account": account, "group_days": days})
            lines = [
                "🔗 <b>Объединить предложения</b>",
                "Выберите номера объявлений одного продукта через запятую. Совпадение названий само по себе ничего не объединяет.",
            ]
            lines += [
                f"{i}. {escaped(values[ident]['name'], 70)} · {escaped(values[ident]['category'], 40)}"
                for i, ident in enumerate(ids[page * 20 : page * 20 + 20], page * 20 + 1)
            ]
            rows = [[("Отменить объединения аналитики", f"sg:{days}:reset")]]
            if page:
                rows.append([("⬅️ Предложения", f"sg:{days}:list:{page - 1}")])
            if (page + 1) * 20 < len(ids):
                rows.append([("Предложения ➡️", f"sg:{days}:list:{page + 1}")])
            return await message.answer(
                "\n".join(lines) if ids else "Нет предложений с подтверждёнными владельцем и категорией.",
                reply_markup=keyboard(
                    rows,
                    back=("К рейтингу", f"sr:{days}:top"),
                ),
            )
        if parts[0] == "sr":
            group = parts[2] if len(parts) == 3 else "reports"
            groups = {
                "charts": ("📈 Графики", [[(label, f"st:{days}:{view}")] for view, label in VIEWS.items()]),
                "top": (
                    "🏆 Лучшие товары",
                    [
                        [("По заказам", f"sd:{days}:bestsellers"), ("По выручке", f"sd:{days}:items")],
                        [("Объединить предложения", f"sg:{days}:list")],
                        [
                            ("Категории · заказы", f"sd:{days}:category_counts"),
                            ("Категории · выручка", f"sd:{days}:categories"),
                        ],
                    ],
                ),
                "finance": (
                    "💰 Финансы",
                    [
                        [("Поступления и расходы", f"sd:{days}:transactions")],
                        [("Себестоимость и прибыль", f"sd:{days}:profit")],
                        [("Комиссии продаж", f"sd:{days}:settlements")],
                    ],
                ),
                "reports": (
                    "📊 Графики и отчёты",
                    [
                        [("Графики", f"sr:{days}:charts"), ("Лучшие товары", f"sr:{days}:top")],
                        [("Финансы", f"sr:{days}:finance"), ("Как рассчитывается", f"sd:{days}:details")],
                    ],
                ),
            }
            if group not in groups:
                raise ValueError("Раздел статистики недоступен. Откройте статистику заново")
            title, rows = groups[group]
            return await message.answer(
                "<b>" + title + "</b>", reply_markup=keyboard(rows, back=("⬅️ К статистике", f"stats:{days}"))
            )
        stats = await asyncio.to_thread(
            grouped_statistics, self.store, owner, self.store.setting("playerok:account_id"), days
        )
        view = parts[2] if len(parts) == 3 else "overview"
        if parts[0] == "sd":
            if view == "settlements":
                account = self.store.setting("playerok:account_id") or self.store.setting(
                    "transactions:account_id"
                )
                report = await asyncio.to_thread(settlement_statistics, self.store, owner, account, days)
                text = (
                    "🧾 <b>Сверенные комиссии продаж</b>"
                    + f"\nПериод: {'всё время' if days == 0 else str(days * 24) + ' ч'} · по дате завершения зачисления"
                    + f"\nОднозначно сверено заказов: {report['orders']}"
                    + f"\nЦена этих заказов: {report['gross']} ₽\nЗачислено по ним: {report['credit']} ₽\nСверенная комиссия продажи: {report['fees']} ₽"
                    + f"\nНе сверено за период: {report['unreconciled']}"
                    + f"\nБез однозначной даты/связи во всём кэше: {report['unattributed']} · без подтверждённого владения: {report['foreign']}"
                    + "\n\nТолько CONFIRMED заказы и SELL IN RUB с точным ID-сопоставлением и равенством цена − transaction.fee = зачисление. Несовпадения исключаются; поле fee других операций не считается комиссией продажи. Это часть локальной истории, не гарантированно вся история магазина. Комиссия уже учтена в зачислении и повторно не вычитается из прибыли."
                )
            elif view == "profit":
                account = self.store.setting("playerok:account_id") or self.store.setting(
                    "transactions:account_id"
                )
                report = await asyncio.to_thread(profit_statistics, self.store, owner, account, days)
                text = (
                    "🧾 <b>Прибыль по учёту владельца</b>"
                    + f"\nПериод: {'всё время' if days == 0 else str(days * 24) + ' ч'} · по завершению SELL-зачисления"
                    + f"\nЗаказов с проверенными расходами: {report['reviewed_orders']}"
                    + f"\nИтог только по ним: {report['profit']} ₽"
                    + f"\nЗаказов за период с неполным/непроверенным учётом: {report['incomplete_orders']}"
                    + f"\nПодтверждённых заказов без однозначной даты зачисления во всём кэше: {report['unattributed_orders']}"
                    + f"\nНе подтверждено владение сохранённой продажей: {report['foreign_orders']}"
                    + "\n\nОткройте заказ → «Себестоимость и прибыль». Текущая себестоимость товара не подставляется задним числом. Себестоимость и прочие расходы указывает владелец; распределённые PREMIUM/удержания проверяются по источникам. Итог не включает заказы с неизвестными данными и не означает прибыль всего магазина. Изменение исходных сумм сбрасывает результат до повторной проверки."
                )
            elif view == "transactions":
                ledger = await asyncio.to_thread(transaction_statistics, self.store, days)
                text = (
                    "💰 <b>Подтверждённые транзакции</b>\n"
                    + (
                        "Есть локальные данные; полнота не гарантирована."
                        if ledger["has_data"]
                        else "История транзакций ещё не загружена."
                    )
                    + f"\nПериод: {'всё время' if days == 0 else str(days * 24) + ' ч'} · по дате завершения"
                    + f"\nЗаписей в кэше: {ledger['cached']} · подтверждённых за период: {ledger['confirmed']}"
                    + f"\nЗачислено на баланс (все операции): {ledger['credits']} ₽"
                    + f"\nИз них за продажи: {ledger['sales_credit']} ₽ · пополнения: {ledger['deposits']} ₽"
                    + f"\nСписано с баланса (все операции): {ledger['debits']} ₽"
                    + f"\nИз них выплаты: {ledger['withdraw_debit']} ₽ · PREMIUM: {ledger['premium_debit']} ₽"
                    + f"\nДругие приоритеты: {ledger['other_priority_debit']} ₽"
                    + f"\nК получению по явному полю API выплат: {ledger['withdraw_net_reported']} ₽"
                    + f"\nРазница списания и явной суммы к получению: {ledger['withdraw_retained_known']} ₽ · сверено выплат: {ledger['withdraw_known_count']}"
                    + f"\nВыплат без подтверждённой суммы к получению: {ledger['withdraw_net_unknown']}"
                    + f"\n\nПропущено сумм: {ledger['unknown_amounts']} · других валют: {ledger['foreign_currency']}"
                    + f"\nНеизвестных направлений: {ledger['unknown_directions']} · операций вне отдельных категорий: {ledger['unknown_operations']}"
                    + f"\nПодтверждённых записей без даты завершения во всём кэше: {ledger['unknown_dates']}"
                    + "\nПокрытие завершений: "
                    + checked_time(ledger["coverage_start"])
                    + " — "
                    + checked_time(ledger["coverage_end"])
                    + "\nПоследний полный обход: "
                    + checked_time(ledger["last_sync"])
                    + "\n\nТолько текущий CONFIRMED и известные суммы RUB. Заказы и транзакции используют разные даты. Пополнения не являются доходом от продаж. Выплата списания не доказывает доставку денег в банк. Поле fee не суммируется: единицы неоднозначны. Другие приоритеты не объявляются поднятиями. Эти суммы не складываются с другими журналами расходов без сверки пересечений. Прибыль требует исторической себестоимости."
                )
            elif view == "details":
                text = (
                    "📊 <b>Состав статистики</b>\nПериод: "
                    + (checked_time(stats["period_start"]) if days else "всё время")
                    + " — "
                    + checked_time(stats["period_end"])
                    + f"\nЗаказов: {stats['orders']} · оплаченных: {stats['paid']}\nПодтверждено: {stats['confirmed']} · возвращено: {stats['refunded']}"
                    + f"\nИзвестная выручка: {stats['revenue']} ₽\nПодтверждённые продажи до комиссий: {stats['confirmed_revenue']} ₽\nВозвраты по текущему статусу: {stats['refunds']} ₽"
                    + f"\nСредний чек: {stats['average'] if stats['average'] is not None else 'неизвестен'}"
                    + f"\nПредыдущий период: {stats['previous_revenue']} ₽ · оплаченных {stats['previous_paid']}"
                    + f"\nИзменение выручки: {str(stats['growth_percent']) + '%' if stats['growth_percent'] is not None else 'не рассчитывается'}"
                    + f"\nПрочие подтверждённые расходы: {stats['expenses']} ₽\nПродвижение по ledger: {stats['promotion_expenses']} ₽"
                    + "\nЭти источники расходов не складываются без сверки пересечений. Разделение PREMIUM/поднятий по имеющимся записям не доказано."
                    + f"\n\nНеизвестных сумм: {stats['unknown_amounts']} (оплаченных: {stats['unknown_paid_amounts']})\nЗаписей без серверной даты: {stats['unknown_dates']}\nНеполных/нерублёвых расходов: {stats['unknown_expenses']}"
                    + "\nПокрытие заказов: "
                    + checked_time(stats["coverage_start"])
                    + " — "
                    + checked_time(stats["coverage_end"])
                    + "\nПоследний обход API: "
                    + checked_time(stats["last_sync"])
                    + "\n\nЗаказы сгруппированы по дате создания, статусы текущие. Суммы возвратов не означают дату движения денег. Локальное покрытие не доказывает полноту API-истории. Комиссии, поступления и прибыль требуют сверки транзакций; себестоимость в карточке не становится историческим расходом автоматически."
                )
            elif view in {"bestsellers", "category_counts"}:
                by_item = view == "bestsellers"
                counts = stats["item_orders" if by_item else "category_orders"]
                revenues = stats["items" if by_item else "categories"]
                unknown = stats["item_unknown_paid" if by_item else "category_unknown_paid"]
                top = sorted(counts, key=lambda key: (-counts[key], key))[:15]
                text = (
                    "📊 <b>Топ-15 · "
                    + ("товары" if by_item else "категории")
                    + " по оплаченным заказам</b>"
                    + f"\nПериод: {'всё время' if days == 0 else str(days * 24) + ' ч'}"
                )
                for index, key in enumerate(top, 1):
                    label = stats["item_names"].get(key, key) if by_item else key
                    text += f"\n\n{index}. {escaped(label, 80)}"
                    known = counts[key] - unknown.get(key, 0)
                    amount = f"{revenues.get(key, 0)} ₽" if known else "нет известных сумм"
                    text += f"\nЗаказов: {counts[key]} · {amount}"
                    if unknown.get(key, 0):
                        text += f" · без суммы: {unknown[key]}"
                if not top:
                    text += "\nНет оплаченных заказов с серверной датой за период."
                text += (
                    "\n\nКоличество заказов, не единиц товара. По серверной дате создания, статусы текущие; "
                    "возвраты исключены. Суммы до комиссий; неизвестные не заменяются нулём. "
                    "Локальная история может быть неполной."
                )
                if not by_item:
                    text += " Категории сгруппированы по сохранённому названию."
            elif view in {"items", "categories"}:
                values = stats[view]
                top = sorted(values.items(), key=lambda row: (-row[1], row[0]))[:15]
                text = (
                    "📊 <b>"
                    + ("Лучшие товары" if view == "items" else "Категории")
                    + " · известная выручка</b>\n"
                    + "\n".join(
                        f"{index}. {escaped(stats['item_names'].get(key, key) if view == 'items' else key, 120)} · {amount} ₽"
                        for index, (key, amount) in enumerate(top, 1)
                    )
                )
                if not top:
                    text += "Нет данных."
                text += "\nДо комиссий, по текущим статусам локальных заказов."
            else:
                raise ValueError("Раздел статистики недоступен")
            markup = self.markup(days, details=True)
            if view in {"items", "bestsellers"}:
                visible = {key for key, _amount in top} if view == "items" else set(top)
                for key, members in stats.get("offer_sources", {}).items():
                    if key in visible:
                        markup.inline_keyboard.insert(
                            0,
                            keyboard(
                                [
                                    [
                                        (
                                            "Состав: " + stats["item_names"].get(key, "Товар")[:35],
                                            f"sg:{days}:sources:{key.split(':')[1]}",
                                        )
                                    ]
                                ],
                                home=False,
                            ).inline_keyboard[0],
                        )
            return await message.answer(text, reply_markup=markup)
        if parts[0] == "stats":
            text = (
                f"📊 <b>Статистика · {'всё время' if days == 0 else str(days) + ' дн.'}</b>"
                + f"\n🛒 Оплаченных заказов: {stats['paid']}"
                + f"\n💰 Выручка до комиссий: {stats['revenue']} ₽"
                + f"\n📈 Средний чек: {str(stats['average']) + ' ₽' if stats['average'] is not None else 'нет данных'}"
                + "\nПрибыль: пока не рассчитана → Финансы"
                + "\nПо сохранённым данным Playerok; полнота магазина не подтверждена."
                + (
                    f"\nНеизвестных сумм: {stats['unknown_paid_amounts']}"
                    if stats["unknown_paid_amounts"]
                    else ""
                )
            )
            return await message.answer(text, reply_markup=self.markup(days))
        if view not in VIEWS:
            raise ValueError("График недоступен")
        image = await asyncio.to_thread(revenue_chart, stats, view)
        caption = (
            f"📊 <b>Статистика · {'всё время' if days == 0 else str(days * 24) + ' ч'}</b>\nЗаказов: {stats['orders']} · оплаченных: {stats['paid']}"
            + f"\nИзвестная выручка до комиссий: {stats['revenue']} ₽\nВозвраты: {stats['refunds']} ₽\nСредний чек: {stats['average'] if stats['average'] is not None else 'неизвестен'}"
            + f"\nНеизвестных оплаченных сумм: {stats['unknown_paid_amounts']}"
            + "\nТекущие статусы заказов по дате создания. История может быть неполной. Поступления и прибыль не подтверждены."
        )
        return await message.answer_photo(
            BufferedInputFile(image, filename="statistics.png"),
            caption=caption,
            reply_markup=self.markup(days),
        )

    async def message(self, message, state):
        data = await state.get_data()
        ids = data.get("group_candidates", [])
        try:
            indices = [int(value.strip()) - 1 for value in (message.text or "").split(",")]
        except ValueError:
            raise ValueError("Введите номера через запятую, например: 1, 2, 3") from None
        if len(set(indices)) < 2 or any(i < 0 or i >= len(ids) for i in indices):
            raise ValueError("Выберите не менее двух разных номеров из списка")
        account = data["group_account"]
        values = offer_identities(self.store, account)
        members = sorted({ids[i] for i in indices})
        if (
            any(ident not in values for ident in members)
            or len({str(values[ident]["identity"]) for ident in members}) != 1
        ):
            raise ValueError("У предложений должны совпадать подтверждённые владелец, название и категория")
        nonce = secrets.token_hex(8)
        await state.set_state(None)
        await state.update_data(
            offer_group={
                "nonce": nonce,
                "account": account,
                "members": members,
                "expected": {ident: values[ident] for ident in members},
            }
        )
        return (
            f"Объединить {len(members)} объявлений «{escaped(values[members[0]]['name'], 100)}» в один продукт рейтинга?\nПодтвердите, что это один продукт. Заказы и их суммы не изменятся.",
            keyboard(
                [[("✅ Это один продукт", f"sg:{data['group_days']}:commit:{nonce}")]],
                back=("Отмена", f"sr:{data['group_days']}:top"),
            ),
        )
