import threading
from io import BytesIO

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt

_lock = threading.Lock()


def revenue_chart(stats):
    with _lock, plt.style.context("dark_background"):
        figure, axes = plt.subplots(1, 2, figsize=(11, 4), facecolor="#15111f")
        daily = sorted(stats["daily"].items())
        if daily:
            axes[0].bar([d[0][5:] for d in daily], [float(d[1]) for d in daily], color="#ac83f3")
        else:
            axes[0].text(0.5, 0.5, "Нет данных за период", ha="center", transform=axes[0].transAxes)
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
