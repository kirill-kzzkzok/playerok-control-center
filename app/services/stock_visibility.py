"""Native stock pause/resume through the existing scheduler and durable Outbox."""

import json
from datetime import UTC, datetime

from app.database.store import Conflict, server_time
from app.playerok.adapter import APIError
from app.services.logical_products import LogicalProducts

KINDS = ("stock_pause", "stock_resume")


def unstarted(db, action):
    return bool(
        db.execute(
            "SELECT 1 FROM outbox o WHERE o.id=? AND o.kind IN ('stock_pause','stock_resume') "
            "AND o.started_at IS NULL AND (o.result IS NULL OR o.result='null') "
            "AND NOT EXISTS(SELECT 1 FROM outbox_late_results r WHERE r.operation_id=o.id)",
            (action["id"],),
        ).fetchone()
    )


def pause_receipt(store, ident):
    rows = store.rows(
        "SELECT * FROM outbox WHERE target=? AND kind='stock_pause' AND status='SUCCEEDED' "
        "AND started_at IS NOT NULL AND json_extract(result,'$.bot_paused')=1 ORDER BY rowid DESC LIMIT 1",
        (ident,),
    )
    return rows[0] if rows else None


def queue_due(store, api):
    if not all(callable(getattr(api.account, name, None)) for name in ("discontinue_item", "republish_item")):
        return
    for row in store.rows(
        "SELECT l.item_id FROM logical_listings l JOIN logical_products p ON p.id=l.product_id "
        "WHERE p.handler='unique_stock' AND p.enabled=1 AND p.state='VERIFIED' "
        "AND l.verified_at IS NOT NULL AND l.source_order_id IS NULL "
        "AND l.server_status IN ('APPROVED','DISCONTINUED')"
    ):
        ident = row["item_id"]
        with store.transaction() as db:
            product = LogicalProducts(store, api).listing(ident)
            if not product or product["config_revision"] != product["revision"]:
                continue
            available = db.execute(
                "SELECT COUNT(*) FROM inventory WHERE item_id=? AND status='AVAILABLE'",
                (product["stock_item_id"],),
            ).fetchone()[0]
            if db.execute(
                "SELECT 1 FROM outbox WHERE target=? AND kind IN ('stock_pause','stock_resume') "
                "AND (status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') "
                "OR error_code='MANUALLY_STOPPED_UNCERTAIN')",
                (ident,),
            ).fetchone():
                continue
            pause = pause_receipt(store, ident)
            resumed = (
                pause
                and db.execute(
                    "SELECT id FROM outbox WHERE kind='stock_resume' AND json_extract(payload,'$.pause_id')=? AND status='SUCCEEDED'",
                    (pause["id"],),
                ).fetchone()
            )
            if available and (not pause or resumed) or not available and pause and not resumed:
                continue
            generation = db.execute(
                "SELECT COALESCE(MAX(id),0) FROM inventory WHERE item_id=?", (product["stock_item_id"],)
            ).fetchone()[0]
            payload = {k: product[k] for k in ("revision", "control_revision", "account_id", "stock_item_id")}
            payload.update(
                product_id=product["id"],
                actor=product["owner_id"],
                pause_id=pause["id"] if available else None,
            )
            store.enqueue(
                f"stock-resume:{pause['id']}:{product['revision']}:{product['control_revision']}"
                if available
                else f"stock-pause:{ident}:{generation}:{product['revision']}:{product['control_revision']}",
                "stock_resume" if available else "stock_pause",
                ident,
                payload,
                db,
                actor=product["owner_id"],
            )


def guard(store, api, action):
    p = action["payload"]
    logical = LogicalProducts(store, api)
    product = logical.listing(action["target"])
    logical.authority(p["actor"], p["account_id"], writes=True)
    if (
        not getattr(api.account, "automation_enabled", False)
        or not product
        or any(product[k] != p[k] for k in ("revision", "control_revision", "account_id", "stock_item_id"))
        or product["id"] != p["product_id"]
        or not product["enabled"]
        or product["handler"] != "unique_stock"
    ):
        raise Conflict("Выдача остановлена или настройки склада изменились")
    policies = store.rows("SELECT payload FROM delivery_policies WHERE item_id=?", (action["target"],))
    policy = json.loads(policies[0]["payload"]) if policies else {}
    if not policy.get("enabled") or not policy.get("auto_enroll") or policy.get("mode") != "stock":
        raise Conflict("Будущие продажи склада не разрешены")
    if store.setting(f"delivery:{action['target']}:enabled", True) is False:
        raise Conflict("Выдача остановлена владельцем")
    try:
        logical.require_delivery(action["target"], "unique_stock")
    except Conflict:
        raise APIError("VISIBILITY_BINDING_PENDING") from None
    available = store.rows(
        "SELECT COUNT(*) n FROM inventory WHERE item_id=? AND status='AVAILABLE'", (product["stock_item_id"],)
    )[0]["n"]
    if bool(available) != (action["kind"] == "stock_resume"):
        raise Conflict("Остаток склада изменился; действие больше не требуется")
    if action["kind"] == "stock_resume":
        receipt = pause_receipt(store, action["target"])
        if not receipt or receipt["id"] != p["pause_id"]:
            raise Conflict("Нет подтверждения штатной остановки этого товара ботом")
        previous = json.loads(receipt["payload"])
        if any(previous[k] != p[k] for k in ("product_id", "stock_item_id", "account_id")):
            raise Conflict("Разрешение изменилось после остановки товара")
    return product


async def execute(executor, action):
    store, api, ident = executor.store, executor.api, action["target"]
    product = guard(store, api, action)
    item = await executor.owned_item(ident)
    logical = LogicalProducts(store, api)
    await logical.refresh_title(item)
    product = guard(store, api, action)
    try:
        logical.verify_listing(product, item)
    except Conflict:
        raise APIError("VISIBILITY_BINDING_PENDING") from None
    state = await api.call("get_item_visibility", id=ident)
    if (
        state.get("id") != ident
        or (state.get("user") or {}).get("id") != product["account_id"]
        or state.get("status") != item.get("status")
    ):
        raise APIError("VISIBILITY_DETAILS_INCOMPLETE")
    resume = action["kind"] == "stock_resume"
    if not resume and item.get("status") != "APPROVED":
        return {"remote_id": ident, "bot_paused": False}
    if resume and item.get("status") == "APPROVED" and state.get("pause_available") is True:
        fresh = await executor.owned_item(ident)
        if fresh != item:
            raise APIError("VISIBILITY_PREPARE_CHANGED")
        guard(store, api, action)
        return {"remote_id": ident, "bot_paused": False, "status": "APPROVED", "already_active": True}
    if (
        state.get("republish_available" if resume else "pause_available") is not True
        or resume
        and state.get("may_be_published") is not False
    ):
        raise APIError("VISIBILITY_NOT_AVAILABLE")
    if resume:
        receipt = json.loads(pause_receipt(store, ident)["result"])
        if (
            item.get("status") != "DISCONTINUED"
            or item.get("priority") != receipt.get("priority")
            or not server_time(item.get("updated_at"))
            or server_time(item.get("updated_at")) != server_time(receipt.get("updated_at"))
            or state.get("status_expiration_date") != receipt.get("status_expiration_date")
        ):
            raise Conflict("Параметры снятого товара изменились; платная публикация не выполняется")
        try:
            expiry = datetime.fromisoformat(state["status_expiration_date"])
        except (KeyError, TypeError, ValueError):
            raise Conflict("Срок размещения не подтверждён") from None
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise Conflict("Срок размещения истёк; автоматическая платная публикация запрещена")
    again = await executor.owned_item(ident)
    if any(
        again.get(k) != item.get(k)
        for k in (
            "id",
            "name",
            "description",
            "price",
            "priority",
            "status",
            "deleted_at",
            "attributes",
            "data_fields",
            "obtaining_type",
            "category",
            "user",
        )
    ):
        raise APIError("VISIBILITY_PREPARE_CHANGED")
    guard(store, api, action)
    await executor.mutate(
        action,
        "republishItem" if resume else "discontinueItem",
        "republish_item" if resume else "discontinue_item",
        id=ident,
    )
    from app.services.product_readiness import ProductReadiness

    readiness = ProductReadiness(store, api)
    read_version = readiness.item_version(ident)
    try:
        fresh = await executor.owned_item(ident)
        state_after = await api.call("get_item_visibility", id=ident)
        LogicalProducts(store, api).verify_listing(product, fresh)
        if (
            fresh.get("status") != ("APPROVED" if resume else "DISCONTINUED")
            or state_after.get("id") != ident
            or (state_after.get("user") or {}).get("id") != product["account_id"]
            or state_after.get("status") != fresh.get("status")
            or any(fresh.get(k) != item.get(k) for k in ("id", "name", "description", "price", "priority"))
            or state_after.get("status_expiration_date") != state.get("status_expiration_date")
        ):
            raise ValueError
    except (APIError, ValueError, Conflict):
        raise APIError("VISIBILITY_RESULT_UNKNOWN", uncertain=True) from None
    readiness.remember_item(fresh, version=read_version)
    return {
        "remote_id": ident,
        "bot_paused": not resume,
        "status": fresh["status"],
        "price": fresh.get("price"),
        "priority": fresh.get("priority"),
        "updated_at": fresh.get("updated_at"),
        "status_expiration_date": state_after.get("status_expiration_date"),
    }
