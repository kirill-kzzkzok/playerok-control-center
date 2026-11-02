"""Export source-level Telegram button inventory; no configuration or API access."""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HANDLERS = {}
for prefixes, handler in [
    ("gft", "GiftsPanel.callback"),
    ("lp", "LogicalProductsPanel.callback"),
    ("fin finance providers", "FinancePanel.callback"),
    ("sg stats st sd sr", "StatisticsPanel.handle"),
    ("payp", "PaidPromotionsPanel.callback"),
    ("promo pp pi promotions", "PromotionsPanel.callback"),
    ("auto ap automation", "AutomationPanel.callback"),
    ("w create cancel_wizard", "Wizard / router"),
    ("dl drafts", "DraftsPanel.callback"),
    ("draft launch ln", "LaunchPanel / Wizard / router"),
    ("il icp listnext itemfilter itemfilters items chats", "ListingsPanel / ItemCategoryPicker / router"),
    ("iv item itemmedia ipe", "ItemPanel / router"),
    ("chat chatpage chatmore chatmedia", "ChatPanel / router"),
    ("stock", "StockPanel.callback"),
    ("ff", "FulfillmentPanel.callback"),
    ("order orders orderfilters orderpage od delivered", "OrdersPanel / router"),
    ("pf", "OrderFinancePanel.callback"),
    ("aq", "AutomationQueue.callback"),
    ("ar", "AutoRepliesPanel.callback"),
    ("quick quickreplies replypreview", "RepliesPanel / router"),
    ("agv", "AgreementsPanel.callback"),
    ("ops", "OperationsPanel.callback"),
    ("result", "OperationResults / router"),
    ("ic", "InterfaceCleanupPanel.callback"),
    ("sp settings", "SettingsPanel.callback"),
    ("profile", "ProfilePanel.handle"),
    ("home more tools attention", "HomePanel / router"),
    ("nav", "Navigation.enter/close"),
    (
        "backup diagnostics sync compose readchat reply editfields quote discount editprice editdesc editname editphoto publish delete commit",
        "router callback / guarded confirmation",
    ),
]:
    HANDLERS.update(dict.fromkeys(prefixes.split(), handler))


def inventory():
    rows = []
    for path in sorted((ROOT / "app/telegram").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Tuple) or len(node.elts) != 2:
                continue
            if isinstance(parents.get(node), (ast.For, ast.comprehension)):
                continue
            label, callback = node.elts
            if not isinstance(label, (ast.Constant, ast.JoinedStr)) or not isinstance(
                callback, (ast.Constant, ast.JoinedStr)
            ):
                continue
            route = callback.value if isinstance(callback, ast.Constant) else ast.unparse(callback)
            if not isinstance(route, str) or not re.match(r"^(?:f?['\"])?[a-z][a-z0-9_]*(?::|$)", route):
                continue
            title = label.value if isinstance(label, ast.Constant) else ast.unparse(label)
            if isinstance(title, str) and title.endswith("_id"):
                continue  # Wizard field→step data, not keyboard declarations.
            functions = [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.lineno <= node.lineno <= n.end_lineno
            ]
            function = min(functions, key=lambda n: n.end_lineno - n.lineno).name if functions else "module"
            rows.append((path.name, function, node.lineno, str(title), route))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not ast.unparse(node.func).endswith("InlineKeyboardButton"):
                continue
            arguments = {keyword.arg: keyword.value for keyword in node.keywords}
            destination = arguments.get("callback_data", arguments.get("url"))
            if destination is None or "text" not in arguments:
                continue
            functions = [
                n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.lineno <= node.lineno <= n.end_lineno
            ]
            function = min(functions, key=lambda n: n.end_lineno - n.lineno).name if functions else "module"
            route = ast.unparse(destination)
            if "url" in arguments:
                route = "url:" + route
            elif isinstance(destination, ast.BinOp) and isinstance(destination.left, ast.Constant):
                route = str(destination.left.value) + "{dynamic}"
            elif isinstance(destination, ast.Constant):
                route = str(destination.value)
            rows.append((path.name, function, node.lineno, ast.unparse(arguments["text"]), route))
    return rows


def export():
    rows = inventory()
    text = [
        "# Карта кнопок Telegram",
        "",
        "Статический реестр production-рендереров. Динамические шаблоны приведены как выражения Python; это инвентаризация, а не утверждение о live-проверке каждой ветки. Контекстные возвраты исполняет Navigation, явные переходы сохраняют указанный callback. Денежные callbacks по-прежнему проверяют FSM nonce, CAS, разрешения и Outbox.",
        "",
        "| Экран / исходник | Кнопка | Callback | Обработчик | Действие | Следующий экран | Возврат |",
        "|---|---|---|---|---|---|---|",
    ]
    for file, function, line, title, route in rows:
        clean = lambda value: value.replace("|", "&#124;").replace("\n", " ")
        prefix = re.match(r"[a-z][a-z0-9_]*", route.removeprefix("f'").removeprefix('f"'))
        handler = HANDLERS.get(
            prefix.group() if prefix else "",
            "Динамическая фабрика keyboard: callback передаёт вызывающий renderer",
        )
        if route.startswith("url:"):
            handler = "Telegram-клиент: внешняя ссылка, без серверной мутации бота"
        effect = "Шаг существующего сценария; проверки — в указанном обработчике"
        destination = "Экран указанного callback; ошибка/ожидание при неподтверждённом результате"
        if route.startswith("nav:back"):
            effect, destination = "Восстановить безопасный UI-контекст", "Фактический родитель из истории"
        elif route.startswith("nav:close"):
            effect, destination = "Закрыть только панель", "Панель закрыта; бизнес-состояния неизменны"
        elif route.startswith(("commit:", "payp:commit:", "payp:confirmarm:", "gft:commit:")):
            effect, destination = (
                "Проверить действующее подтверждение и разрешения",
                "Очередь/результат/безопасный отказ; постановка не означает успех",
            )
        elif route.startswith("fin:select:"):
            effect, destination = "Сохранить общий способ получения денег", "Метод сохранён без запроса суммы"
        elif route.startswith("url:"):
            effect, destination = "Открыть ссылку", "Внешний сайт; состояние бота не меняется"
        text.append(
            f"| {file}:{function}:{line} | {clean(title)} | `{clean(route)}` | {handler} | {effect} | {destination} | Фактический read-only родитель / сценарный FSM / явный переход |"
        )
    (ROOT / "UX_BUTTON_MAP.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    print(f"Exported {len(rows)} button declarations; dynamic factories require journey tests.")


if __name__ == "__main__":
    export()
