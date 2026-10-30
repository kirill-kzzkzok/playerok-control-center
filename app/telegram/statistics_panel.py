import asyncio

from aiogram.types import BufferedInputFile

from app.rendering.charts import revenue_chart
from app.services.finance import checked_time
from app.services.statistics import statistics
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

    def markup(self, days, *, details=False):
        rows = [
            [
                ("24 ч", "stats:1"),
                ("48 ч", "stats:2"),
                ("7 дн", "stats:7"),
                ("30 дн", "stats:30"),
                ("Всё", "stats:0"),
            ]
        ]
        rows.extend([[(label, f"st:{days}:{view}")] for view, label in VIEWS.items()])
        if not details:
            rows.append(
                [("Подробности и покрытие", f"sd:{days}:details"), ("Лучшие товары", f"sd:{days}:items")]
            )
        rows.append([("Категории", f"sd:{days}:categories")])
        rows.append([("Поступления и расходы по транзакциям", f"sd:{days}:transactions")])
        return keyboard(rows)

    async def handle(self, action, message):
        parts = action.split(":")
        days = int(parts[1])
        stats = await asyncio.to_thread(statistics, self.store, days)
        view = parts[2] if len(parts) == 3 else "overview"
        if parts[0] == "sd":
            if view == "transactions":
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
            return await message.answer(text, reply_markup=self.markup(days, details=True))
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
