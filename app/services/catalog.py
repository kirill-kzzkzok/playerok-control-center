from app.services.products import ValidationError

ORDER_FILTERS = {
    "ALL": ("Все", "1=1", ()),
    "PAID": ("Оплаченные", "status='PAID'", ()),
    "PENDING": ("Ожидающие", "status='PENDING'", ()),
    "SENT": ("Выданные", "status='SENT'", ()),
    "CONFIRMED": ("Завершённые", "status IN ('CONFIRMED','CONFIRMED_AUTOMATICALLY')", ()),
    "ROLLED_BACK": ("Возвраты", "status='ROLLED_BACK'", ()),
    "PROBLEM": ("Проблемные", "json_extract(payload,'$.has_problem')=1", ()),
}


def order_page(store, page=0, filter_name="ALL", search="", page_size=12):
    if filter_name not in ORDER_FILTERS or not 0 <= page <= 1_000_000 or not 1 <= page_size <= 30:
        raise ValidationError("Некорректный фильтр или страница")
    _, where, args = ORDER_FILTERS[filter_name]
    if search:
        # Wildcard characters are escaped; user input never becomes SQL.
        value = search[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND (id LIKE ? ESCAPE '\\' OR json_extract(payload,'$.item.name') LIKE ? ESCAPE '\\')"
        args += ("%" + value + "%", "%" + value + "%")
    total = store.rows("SELECT count(*) AS n FROM orders WHERE " + where, args)[0]["n"]
    rows = store.rows(
        "SELECT id,status,amount FROM orders WHERE "
        + where
        + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
        (*args, page_size, page * page_size),
    )
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "has_previous": page > 0,
        "has_next": (page + 1) * page_size < total,
        "filter": filter_name,
    }
