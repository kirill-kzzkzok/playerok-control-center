import uuid
from dataclasses import asdict, is_dataclass
from decimal import Decimal

from app.database.store import encode, now


def serializable(value):
    if is_dataclass(value):
        return serializable(asdict(value))
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: serializable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(child) for child in value]
    return value


def save_quote(store, owner, target, inputs, result, *, context=None):
    # A quote is not a financial operation. Never copy a whole product payload:
    # it may contain buyer credentials, chat messages or inventory.
    allowed = {
        key: inputs.get(key)
        for key in (
            "price",
            "withdrawal_amount",
            "fee_multiplier",
            "fee_checked_at",
            "fee_source",
            "fee_basis",
            "cost",
            "withdrawal",
            "placement",
            "placement_quote",
        )
        if key in inputs
    }
    payload = {
        "kind": "ESTIMATE_ONLY",
        "formula_version": "playerok-web-2026-10-29",
        "inputs": serializable(allowed),
        "result": serializable(result),
        "context": serializable(context),
    }
    ident = uuid.uuid4().hex
    store.execute(
        "INSERT INTO financial_quotes VALUES(?,?,?,?,?)", (ident, owner, target, now(), encode(payload))
    )
    return ident


def item_preferences(store, owner, ident):
    return [
        store.setting(f"finance:{owner}:{key}:{ident}") for key in ("cost", "placement", "placement_per_sale")
    ] + [store.setting(f"finance:{owner}:withdrawal")]


async def prepare_item_quote(store, api, products, owner, ident):
    from app.playerok.adapter import APIError
    from app.services.finance import money, selected_fee_multiplier
    from app.services.product_readiness import ProductReadiness
    from app.services.products import priority_price_basis
    from app.services.withdrawals import draft_calculation, withdrawal_snapshot

    account = api.account.id
    if not account or not store.rows("SELECT 1 FROM administrators WHERE user_id=?", (owner,)):
        raise ValueError("Принадлежность расчёта не подтверждена")
    readiness = ProductReadiness(store, api)
    version = readiness.item_version(ident)
    preferences = item_preferences(store, owner, ident)
    item = await api.call("get_item", id=ident)
    if (
        item.get("id") != ident
        or (item.get("user") or {}).get("id") != account
        or api.account.id != account
        or store.setting("playerok:account_id") not in (None, account)
    ):
        raise ValueError("Владение товаром не подтверждено")
    payload = {
        "price": item["price"],
        "fee_multiplier": item.get("fee_multiplier"),
        "fee_checked_at": now(),
        "cost": store.setting(f"finance:{owner}:cost:{ident}"),
    }
    obtaining = item.get("obtaining_type") or {}
    payload["fee_multiplier"] = selected_fee_multiplier(
        item.get("fee_multiplier"), obtaining.get("fee_multiplier")
    )
    payload["fee_basis"] = {
        "item_id": ident,
        "obtaining_type_id": obtaining.get("id"),
        "category_id": (item.get("category") or {}).get("id"),
    }
    if (
        item.get("fee_multiplier") is None
        and obtaining.get("fee_multiplier") is None
        and payload["fee_basis"]["category_id"]
    ):
        try:
            schema = await products.schema_details(
                payload["fee_basis"]["category_id"], obtaining.get("id"), auto_agreements=False
            )
            payload["fee_multiplier"] = schema["fee_multiplier"]
            payload["fee_checked_at"] = schema["checked_at"]
        except APIError:
            payload["fee_multiplier"] = None
    chosen = store.setting(f"finance:{owner}:withdrawal")
    if chosen:
        payload["withdrawal"] = await withdrawal_snapshot(api, chosen["provider"]["id"])
    placement, quotes = None, []
    if item.get("raw_price") is not None:
        try:
            quotes = await products.priority_preview(ident, priority_price_basis(item))
        except APIError:
            quotes = []
    if item.get("status") == "DRAFT" and quotes:
        preferred = store.setting(f"finance:{owner}:placement:{ident}")
        chosen = (
            next((q for q in quotes if q.get("id") == preferred), None)
            if preferred
            else next(
                (
                    q
                    for q in quotes
                    if q.get("type") == "DEFAULT" and q.get("price") is not None and money(q["price"]) == 0
                ),
                None,
            )
        )
        if chosen and chosen.get("price") is not None:
            placement = money(chosen["price"])
            payload["placement_quote"] = {
                "id": chosen["id"],
                "type": chosen.get("type"),
                "price": str(placement),
                "base_price": priority_price_basis(item),
                "checked_at": now(),
                "source": "Playerok itemPriorityStatuses",
            }
    allocation = store.setting(f"finance:{owner}:placement_per_sale:{ident}")
    result = draft_calculation(payload, placement=money(allocation) if allocation is not None else None)
    if api.account.id != account or store.setting("playerok:account_id") not in (None, account):
        raise ValueError("Аккаунт Playerok изменился")
    display_item = {"id": ident, "name": item.get("name"), "status": item.get("status")}
    display_quotes = [{"type": q.get("type"), "price": q.get("price")} for q in quotes]
    save_quote(
        store,
        owner,
        "item:" + ident,
        {**payload, "placement": allocation},
        result,
        context={
            "account": account,
            "item": display_item,
            "quotes": display_quotes,
            "version": version,
            "preferences": preferences,
            "new_placement": str(placement) if placement is not None else None,
        },
    )
    return display_item, payload, result, quotes, allocation, placement


async def refresh_due_quote(store, api):
    import asyncio
    import json
    import time

    from app.playerok.adapter import APIError
    from app.services.products import Products

    account = api.account.id
    if not account or account != store.setting("playerok:account_id"):
        return
    rows = store.rows(
        "SELECT key,value FROM settings WHERE key GLOB 'ui:finance_refresh:*' "
        "AND json_extract(value,'$.account')=? AND json_extract(value,'$.due')<=? "
        "ORDER BY json_extract(value,'$.due'),key LIMIT 1",
        (account, time.time()),
    )
    if not rows:
        return
    row = rows[0]
    request = json.loads(row["value"])
    try:
        async with asyncio.timeout(8):
            await prepare_item_quote(store, api, Products(store, api), request["owner"], request["item"])
    except (APIError, TimeoutError, ValueError, TypeError, KeyError) as error:
        failures = min(request.get("failures", 0) + 1, 5)
        retry_after = getattr(error, "retry_after", None)
        delay = max(30 * 2**failures, int(retry_after) if str(retry_after).isdigit() else 0)
        store.set_setting(row["key"], {**request, "failures": failures, "due": time.time() + delay})
    else:
        store.execute("DELETE FROM settings WHERE key=? AND value=?", (row["key"], row["value"]))
