import threading
from datetime import datetime
from io import BytesIO
from itertools import pairwise

import matplotlib

matplotlib.use("Agg")
from matplotlib import dates as mdates
from matplotlib import pyplot as plt

_lock = threading.Lock()


def smooth_points(x, y, subdivisions=16):
    """Shape-preserving Hermite interpolation; original observations stay exact.

    Harmonic tangents stop at extrema, so nonnegative data never gains invented
    negative values or peaks beyond adjacent observations. This is display only.
    """
    if len(x) != len(y) or subdivisions < 1:
        raise ValueError("Некорректные точки графика")
    if len(x) < 2:
        return list(x), list(y)
    widths = [right - left for left, right in pairwise(x)]
    if any(width <= 0 for width in widths):
        raise ValueError("Время точек должно возрастать")
    secants = [(right - left) / width for left, right, width in zip(y, y[1:], widths)]
    tangents = [secants[0]]
    for index in range(1, len(x) - 1):
        before, after = secants[index - 1], secants[index]
        if before * after <= 0:
            tangents.append(0)
        else:
            w1 = 2 * widths[index] + widths[index - 1]
            w2 = widths[index] + 2 * widths[index - 1]
            tangents.append((w1 + w2) / (w1 / before + w2 / after))
    tangents.append(secants[-1])
    points_x, points_y = [x[0]], [y[0]]
    for index, width in enumerate(widths):
        for step in range(1, subdivisions + 1):
            t = step / subdivisions
            value = (
                (2 * t**3 - 3 * t**2 + 1) * y[index]
                + (t**3 - 2 * t**2 + t) * width * tangents[index]
                + (-2 * t**3 + 3 * t**2) * y[index + 1]
                + (t**3 - t**2) * width * tangents[index + 1]
            )
            points_x.append(x[index] + t * width)
            points_y.append(value)
    return points_x, points_y


def temporal_curve(axis, values, color="#ac83f3"):
    rows = sorted(values.items())
    if not rows:
        axis.text(0.5, 0.5, "Нет подтверждённых данных", ha="center", transform=axis.transAxes)
        axis.set_xticks([])
        axis.set_yticks([])
        return
    times = [datetime.fromisoformat(key) for key, _ in rows]
    x = [float(mdates.date2num(value)) for value in times]
    y = [float(value) for _, value in rows]
    hourly = len(rows[0][0]) > 10
    max_gap = 1.5 / 24 if hourly else 1.5
    starts = [0] + [index for index in range(1, len(x)) if x[index] - x[index - 1] > max_gap] + [len(x)]
    for start, end in pairwise(starts):
        curve_x, curve_y = smooth_points(x[start:end], y[start:end])
        axis.plot(curve_x, curve_y, color=color, linewidth=2.4)
        axis.fill_between(curve_x, curve_y, 0, color=color, alpha=0.16)
    axis.scatter(x, y, color=color, s=16, zorder=3)
    if len(x) == 1:
        axis.set_xlim(x[0] - max_gap / 2, x[0] + max_gap / 2)
        axis.set_xticks(x)
    else:
        axis.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=6))
    axis.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M" if hourly else "%d.%m.%Y"))
    plt.setp(axis.get_xticklabels(), rotation=35, ha="right", fontsize=8)
    axis.set_ylim(bottom=0)
    axis.grid(axis="y", alpha=0.12)
    axis.text(
        0,
        -0.36,
        "Точки — факты; линия — сглаживание.\nПропуски данных — разрывы.",
        transform=axis.transAxes,
        fontsize=8,
        color="#bcb0d4",
    )


def revenue_chart(stats, view="overview"):
    if view != "overview":
        return analytics_chart(stats, view)
    with _lock, plt.style.context("dark_background"):
        figure, axes = plt.subplots(1, 2, figsize=(11, 4.8), facecolor="#15111f")
        temporal_curve(axes[0], stats["daily"])
        axes[0].set_title("Выручка по дням · Москва")
        axes[0].set_ylabel("₽ · до комиссий")
        categories = sorted(stats["categories"].items(), key=lambda row: row[1], reverse=True)[:5]
        if categories:
            axes[1].barh([c[0][:20] for c in categories], [float(c[1]) for c in categories], color="#7e63b8")
        else:
            axes[1].text(0.5, 0.5, "Нет продаж", ha="center", transform=axes[1].transAxes)
        axes[1].set_title("Категории")
        figure.tight_layout()
        stream = BytesIO()
        figure.savefig(stream, format="png", dpi=130)
        plt.close(figure)
        return stream.getvalue()


def analytics_chart(stats, view):
    if view not in {"time", "average", "promotion", "comparison"}:
        raise ValueError("Неизвестный график")
    with _lock, plt.style.context("dark_background"):
        figure, axes = plt.subplots(1, 2, figsize=(12, 5.2), facecolor="#15111f")

        def series(axis, values, title, ylabel):
            temporal_curve(axis, values)
            axis.set_title(title)
            axis.set_ylabel(ylabel)

        if view == "time":
            hourly = stats["days"] in {1, 2}
            series(
                axes[0],
                stats["hourly_orders"] if hourly else stats["daily_orders"],
                "Заказы по часу" if hourly else "Заказы по дню",
                "Количество",
            )
            series(
                axes[1],
                stats["hourly"] if hourly else stats["daily"],
                "Выручка · до комиссий",
                "₽ · известные суммы",
            )
        elif view == "average":
            series(axes[0], stats["daily_average"], "Средний чек оплаченных заказов", "₽ · известные суммы")
            series(axes[1], stats["daily_paid"], "Оплаченные заказы", "Количество")
        elif view == "promotion":
            series(
                axes[0],
                stats["promotion_daily"],
                "Подтверждённые расходы продвижения",
                "₽ · ledger SUCCEEDED",
            )
            axes[1].axis("off")
            axes[1].text(
                0.05,
                0.6,
                f"Продвижение: {stats['promotion_expenses']} ₽\nПрочие расходы: {stats['expenses']} ₽\n\nИсточники показаны отдельно.\nНе суммируются без проверки пересечений.\nОценки dry-run не включены.",
                transform=axes[1].transAxes,
                fontsize=12,
                va="center",
            )
        else:
            if stats["days"] == 0:
                for axis in axes:
                    axis.text(
                        0.5,
                        0.5,
                        "Для всего времени нет\nпредыдущего равного периода",
                        ha="center",
                        transform=axis.transAxes,
                    )
            else:
                axes[0].bar(
                    ["Предыдущий", "Текущий"],
                    [float(stats["previous_revenue"]), float(stats["revenue"])],
                    color=["#65578c", "#ac83f3"],
                )
                axes[0].set_title("Известная выручка · до комиссий")
                axes[0].set_ylabel("₽")
                axes[1].bar(
                    ["Предыдущий", "Текущий"],
                    [stats["previous_paid"], stats["paid"]],
                    color=["#65578c", "#ac83f3"],
                )
                axes[1].set_title("Оплаченные заказы")
        figure.suptitle("PLAYEROK CONTROL CENTER · локальное покрытие · Москва", fontsize=10, color="#bcb0d4")
        figure.tight_layout()
        stream = BytesIO()
        figure.savefig(stream, format="png", dpi=130)
        plt.close(figure)
        return stream.getvalue()
