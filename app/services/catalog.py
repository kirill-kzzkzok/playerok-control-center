from app.services.products import ValidationError

ORDER_FILTERS = {
    "ALL": ("Все", "1=1", ()),
    "PAID": ("Оплаченные", "status='PAID'", ()),
    "PENDING": ("Ожидающие", "status='PENDING'", ()),
    "SENT": ("Выданные", "status='SENT'", ()),
    "CONFIRMED": ("Завершённые", "status IN ('CONFIRMED','CONFIRMED_AUTOMATICALLY')", ()),
    "ROLLED_BACK": ("Возвраты", "status='ROLLED_BACK'", ()),
    "FAILED": ("Не состоялись", "status='FAILED'", ()),
    "PROBLEM": (
        "Проблемные",
        "(json_extract(payload,'$.has_problem')=1 OR EXISTS (SELECT 1 FROM order_annotations a WHERE a.order_id=orders.id AND a.problem=1))",
        (),
    ),
}

DRAFT_FILTERS = {
    "ALL": ("Все", "1=1"),
    "DRAFT": ("Локальные черновики", "state='DRAFT'"),
    "QUEUED": ("В очереди", "state='QUEUED'"),
    "REMOTE_DRAFT": ("Созданные на сервере", "state='REMOTE_DRAFT'"),
    "UPDATED": ("Изменённые", "state='UPDATED'"),
    "CANCELLED": ("Отменённые", "state='CANCELLED'"),
    "OTHER": ("Другие состояния", "state NOT IN ('DRAFT','QUEUED','REMOTE_DRAFT','UPDATED','CANCELLED')"),
}


def draft_page(store, owner, page=0, filter_name="ALL", search="", page_size=10):
    if (
        filter_name not in DRAFT_FILTERS
        or type(page) is not int
        or not 0 <= page <= 1_000_000
        or type(page_size) is not int
        or not 1 <= page_size <= 30
        or not isinstance(search, str)
        or len(search) > 200
    ):
        raise ValidationError("Некорректный поиск, фильтр или страница черновиков")
    where = "owner_id=? AND " + DRAFT_FILTERS[filter_name][1]
    args = (owner,)
    if search:
        value = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # Search public name/ID only, never seller data, description or inventory secrets.
        where += " AND (id LIKE ? ESCAPE '\\' OR json_extract(payload,'$.name') LIKE ? ESCAPE '\\')"
        args += ("%" + value + "%",) * 2
    total = store.rows("SELECT count(*) AS n FROM drafts WHERE " + where, args)[0]["n"]
    page = min(page, max(0, (total - 1) // page_size))
    rows = store.rows(
        "SELECT id,state,created_at,json_extract(payload,'$.name') AS name FROM drafts WHERE "
        + where
        + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
        (*args, page_size, page * page_size),
    )
    return {
        "rows": rows,
        "total": total,
        "page": page,
        "pages": max(1, (total + page_size - 1) // page_size),
        "has_previous": page > 0,
        "has_next": (page + 1) * page_size < total,
    }


def order_page(store, page=0, filter_name="ALL", search="", page_size=12):
    if filter_name not in ORDER_FILTERS or not 0 <= page <= 1_000_000 or not 1 <= page_size <= 30:
        raise ValidationError("Некорректный фильтр или страница")
    _, where, args = ORDER_FILTERS[filter_name]
    if search:
        # Wildcard characters are escaped; user input never becomes SQL.
        value = search[:200].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND (id LIKE ? ESCAPE '\\' OR json_extract(payload,'$.item.name') LIKE ? ESCAPE '\\' OR json_extract(payload,'$.user.username') LIKE ? ESCAPE '\\')"
        args += ("%" + value + "%",) * 3
    total = store.rows("SELECT count(*) AS n FROM orders WHERE " + where, args)[0]["n"]
    rows = store.rows(
        "SELECT id,status,amount,json_extract(payload,'$.item.name') AS name,"
        "server_time(json_extract(payload,'$.created_at')) AS server_created_at FROM orders WHERE "
        + where
        + " ORDER BY server_created_at DESC,created_at DESC,id DESC LIMIT ? OFFSET ?",
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
