"""Durable identity-only migration through the existing guarded edit Outbox."""

import json
import re
import uuid

from app.database.store import Conflict, encode, now
from app.playerok.adapter import APIError, validate_page
from app.services.logical_products import LogicalProducts, critical, digest
from app.services.product_codes import choose_code, remove_legacy_tail, reserve_code, title_code, titled
from app.services.products import listing_text


def config_hash(product):
    return digest(
        encode(
            {
                key: product[key]
                for key in (
                    "config",
                    "handler",
                    "revision",
                    "control_revision",
                    "enabled",
                    "inherit_enabled",
                    "armed_at",
                )
            }
        )
    )


def definition_hash(product):
    return digest(
        encode(
            {
                key: product[key]
                for key in ("config", "handler", "revision", "stock_item_id", "account_id", "owner_id")
            }
        )
    )


def snapshot(item):
    if not isinstance(item.get("name"), str) or not isinstance(item.get("description"), str):
        raise Conflict("Playerok не вернул полное название и описание")
    return {
        "id": item["id"],
        "name": item["name"],
        "description": item["description"],
        "critical": critical(item),
        "attachments": [a.get("id") for a in item.get("attachments") or []],
        "raw_price": item.get("raw_price"),
        "priority": item.get("priority"),
        "priority_price": item.get("priority_price"),
    }


class CodeMigration:
    def __init__(self, store, api):
        self.store, self.api = store, api
        self.logical = LogicalProducts(store, api)

    async def discover(self, owner):
        """Read all owned listings; attach only exact, unambiguous old identities."""
        account = self.logical.authority(owner)
        cursor, seen, problems = None, [], []
        for _ in range(200):
            try:
                page = await self.api.call("get_my_items", count=24, after_cursor=cursor, statuses=None)
                items, cursor = validate_page(page, "items", seen)
            except APIError as error:
                problems.append({"stage": "CATALOG", "reason": error.code})
                return problems
            for summary in items:
                ident = summary["id"]
                if self.logical.listing(ident):
                    continue
                try:
                    item = await self.api.call("get_item", id=ident)
                except APIError as error:
                    problems.append({"item_id": ident, "reason": error.code})
                    if error.code in {"RATE_LIMITED", "HTTP_429"}:
                        return problems
                    continue
                lines = (item.get("description") or "").splitlines()
                codes = [line.strip() for line in lines if re.fullmatch(r"#[0-9]{7}", line.strip())]
                if not codes:
                    continue
                parents = self.store.rows(
                    "SELECT p.* FROM logical_products p LEFT JOIN logical_code_migrations m ON m.product_id=p.id WHERE p.owner_id=? AND p.account_id=? AND (p.code=? OR m.old_code=?)",
                    (owner, account, codes[-1], codes[-1]),
                )
                try:
                    if (
                        len(codes) != 1
                        or len(parents) != 1
                        or item.get("id") != ident
                        or (item.get("user") or {}).get("id") != account
                    ):
                        raise Conflict("Старый код не имеет однозначной привязки к владельцу")
                    parent = parents[0]
                    baseline = json.loads(parent["baseline"])
                    if critical(item) != {**baseline, "name": critical(item)["name"]}:
                        raise Conflict("Параметры перевыставленного товара отличаются от привязки")
                    hashes = self.store.rows(
                        "SELECT description_sha256 FROM logical_listings WHERE product_id=?", (parent["id"],)
                    )
                    if digest(item["description"]) not in {h["description_sha256"] for h in hashes}:
                        raise Conflict("Описание перевыставленного товара не подтверждено")
                    with self.store.transaction() as db:
                        if config_hash(self.logical.product(parent["id"])) != config_hash(parent):
                            raise Conflict("Настройки изменились во время поиска")
                        db.execute(
                            "INSERT INTO logical_listings(item_id,product_id,config_revision,description_sha256,server_status,title_snapshot) VALUES(?,?,?,?,?,?) ON CONFLICT(item_id) DO NOTHING",
                            (
                                ident,
                                parent["id"],
                                parent["revision"],
                                digest(item["description"]),
                                item.get("status"),
                                item["name"],
                            ),
                        )
                        db.execute(
                            "UPDATE logical_code_migrations SET state='REQUIRED',updated_at=? WHERE product_id=? AND state IN ('VERIFIED','MANUAL_REVIEW_REQUIRED')",
                            (now(), parent["id"]),
                        )
                except ValueError as error:
                    problems.append({"item_id": ident, "reason": str(error)})
            if not cursor:
                return problems
            seen.append(cursor)
        raise Conflict("Каталог слишком велик; поиск не завершён, изменения не начаты")

    async def run(self, owner, *, scan_catalog=False):
        """Owner-authorized migration only; never drain the general Outbox."""
        from app.services.products import Products
        from app.workers.outbox import Executor, OutboxWorker

        self.logical.authority(owner, writes=True)
        self.store.recover()
        problems = await self.discover(owner) if scan_catalog else []

        async def reconcile_unknown():
            for row in self.store.rows(
                "SELECT m.item_id FROM logical_code_migration_items m JOIN logical_products p ON p.id=m.product_id JOIN outbox o ON o.id=m.operation_id WHERE p.owner_id=? AND p.account_id=? AND o.status='UNCERTAIN'",
                (owner, self.api.account.id),
            ):
                try:
                    await self.reconcile(owner, row["item_id"])
                except (ValueError, APIError, TimeoutError) as error:
                    problems.append(
                        {
                            "item_id": row["item_id"],
                            "reason": error.code
                            if isinstance(error, APIError)
                            else str(error)
                            if isinstance(error, ValueError)
                            else type(error).__name__,
                        }
                    )

        await reconcile_unknown()
        plan = await self.prepare(owner)
        for value in plan["products"]:
            if not value["ready"]:
                problems.append({"product_id": value["product_id"], "reason": value["problem"]})
                continue
            try:
                self.queue(owner, {**plan, "products": [value]})
            except (ValueError, APIError) as error:
                problems.append(
                    {
                        "product_id": value["product_id"],
                        "reason": error.code
                        if isinstance(error, APIError)
                        else str(error)
                        if isinstance(error, ValueError)
                        else type(error).__name__,
                    }
                )
        worker = OutboxWorker(self.store, Executor(self.store, self.api, Products(self.store, self.api)))
        # Include durable unstarted operations left by a previous process. Each is
        # attempted at most once in this invocation; unknown results are reads only.
        pending = self.store.rows(
            "SELECT o.id FROM outbox o JOIN logical_code_migration_items m ON m.operation_id=o.id JOIN logical_products p ON p.id=m.product_id WHERE p.owner_id=? AND p.account_id=? AND o.status IN ('PENDING','RETRYABLE_FAILED') ORDER BY o.created_at,o.id",
            (owner, self.api.account.id),
        )
        for operation in pending:
            await worker.once(kinds=("edit",), operation_id=operation["id"])
            await reconcile_unknown()
        rows = self.store.rows(
            "SELECT m.item_id,m.state,m.error_code FROM logical_code_migration_items m JOIN logical_products p ON p.id=m.product_id WHERE p.owner_id=? AND p.account_id=?",
            (owner, self.api.account.id),
        )
        report = {
            "verified": sum(r["state"] == "VERIFIED" for r in rows),
            "items": rows,
            "problems": problems,
            "at": now(),
        }
        self.store.set_setting(f"title_codes:report:{self.api.account.id}:{owner}", report)
        return report

    def idle(self, product_id):
        args = (product_id,)
        if self.store.rows(
            "SELECT 1 FROM promotion_expenses WHERE status='UNCERTAIN' AND item_id IN (SELECT item_id FROM logical_listings WHERE product_id=?) LIMIT 1",
            args,
        ):
            raise Conflict("Расход PREMIUM не подтверждён. Остановка повтора не заменяет сверку платежа")
        if self.store.rows(
            "SELECT 1 FROM outbox o WHERE (o.target IN (SELECT item_id FROM logical_listings WHERE product_id=?) OR o.target IN (SELECT id FROM orders WHERE item_id IN (SELECT item_id FROM logical_listings WHERE product_id=?))) AND o.status IN ('PENDING','PROCESSING','RETRYABLE_FAILED','UNCERTAIN') AND json_extract(o.payload,'$.code_migration') IS NULL LIMIT 1",
            (product_id, product_id),
        ):
            raise Conflict("Есть незавершённые или неизвестные операции. Сначала выполните сверку")
        if self.store.rows(
            "SELECT 1 FROM delivery_chains c JOIN logical_listings l ON l.item_id=c.item_id WHERE l.product_id=? AND c.status IN ('ACTIVE','BLOCKED') LIMIT 1",
            args,
        ):
            raise Conflict("Есть незавершённая цепочка выдачи. Сначала разберите её результат")
        if self.store.rows(
            "SELECT 1 FROM gift_jobs g JOIN logical_listings l ON l.item_id=g.item_id WHERE l.product_id=? AND (g.state IN ('PENDING','CHECKING','SPENDING','UNCERTAIN') OR g.refund_state IN ('QUEUED','SPENDING','UNCERTAIN')) LIMIT 1",
            args,
        ):
            raise Conflict("Подарочная выдача или возврат ещё не разрешены")
        if self.store.rows(
            "SELECT 1 FROM orders o JOIN logical_listings l ON l.item_id=o.item_id WHERE l.product_id=? AND o.status='PAID' AND NOT EXISTS(SELECT 1 FROM gift_jobs g WHERE g.order_id=o.id AND g.state='SUCCEEDED') AND NOT EXISTS(SELECT 1 FROM delivery_chains c WHERE c.order_id=o.id AND c.status IN ('SUCCEEDED','NEEDS_FULFILLMENT')) AND NOT EXISTS(SELECT 1 FROM reservations r JOIN inventory i ON i.id=r.inventory_id WHERE r.order_id=o.id AND i.status='DELIVERED') LIMIT 1",
            args,
        ):
            raise Conflict("Есть оплаченный заказ без подтверждённой выдачи. Сначала разберите заказ")
        if self.store.rows(
            "SELECT 1 FROM reservations r JOIN inventory i ON i.id=r.inventory_id JOIN orders o ON o.id=r.order_id JOIN logical_listings l ON l.item_id=o.item_id WHERE l.product_id=? AND i.status IN ('RESERVED','QUARANTINED') LIMIT 1",
            args,
        ):
            raise Conflict("Есть незавершённый складской резерв")

    async def prepare(self, owner):
        account = self.logical.authority(owner)
        plans = []
        plan_id = uuid.uuid4().hex
        selected = set()
        for product in self.store.rows(
            "SELECT * FROM logical_products WHERE owner_id=? AND account_id=? AND state!='DETACHED' ORDER BY id",
            (owner, account),
        ):
            journal = self.store.rows(
                "SELECT * FROM logical_code_migrations WHERE product_id=?", (product["id"],)
            )
            if len(product["code"]) == 3 and not journal:
                continue
            if journal and journal[0]["state"] == "VERIFIED":
                continue
            old = journal[0]["old_code"] if journal else product["code"]
            with self.store.lock:
                code = product["code"] if len(product["code"]) == 3 else choose_code(self.store.db)
                for _ in range(64):
                    if code not in selected:
                        break
                    code = choose_code(self.store.db)
                else:
                    raise Conflict("Не удалось выбрать различные свободные коды")
                selected.add(code)
            value = {
                "product_id": product["id"],
                "plan_id": plan_id,
                "account": account,
                "owner": owner,
                "old_code": old,
                "code": code,
                "hash": config_hash(product),
                "definition_hash": definition_hash(product),
                "items": [],
                "ready": True,
                "problem": None,
            }
            try:
                if self.store.rows(
                    "SELECT 1 FROM logical_code_migration_items m JOIN outbox o ON o.id=m.operation_id WHERE m.product_id=? AND o.status IN ('PENDING','PROCESSING','UNCERTAIN') LIMIT 1",
                    (product["id"],),
                ):
                    raise Conflict(
                        "Перенос выполняется или его результат неизвестен. Откройте результат и сверку"
                    )
                self.idle(product["id"])
                for link in self.store.rows(
                    "SELECT * FROM logical_listings WHERE product_id=? ORDER BY item_id", (product["id"],)
                ):
                    record = {"id": link["item_id"], "ready": False, "problem": None}
                    value["items"].append(record)
                    if link["source_order_id"]:
                        record["problem"] = "Исторический снимок сделки не изменяется"
                        continue
                    try:
                        item = await self.api.call("get_item", id=link["item_id"])
                    except APIError as error:
                        if error.code != "HTTP_404":
                            raise
                        record["problem"] = (
                            "Объявление недоступно. Перенос при следующем разрешённом размещении"
                        )
                        continue
                    if item.get("id") != link["item_id"] or (item.get("user") or {}).get("id") != account:
                        raise Conflict("ID или владелец объявления не подтверждён")
                    record["name"] = item.get("name")
                    if item.get("is_editable") is not True or item.get("deleted_at"):
                        record["problem"] = (
                            "Playerok не разрешил редактирование. История и настройки сохранены"
                        )
                        continue
                    current = title_code(item.get("name"))
                    if current and current != code:
                        raise Conflict("В названии указан другой постоянный код")
                    before = snapshot(item)
                    baseline = json.loads(product["baseline"])
                    if baseline:
                        expected = {**baseline, "name": before["critical"]["name"]}
                        if before["critical"] != expected:
                            raise Conflict(
                                "Критичные параметры объявления изменились. Требуется отдельная проверка"
                            )
                    if link["verified_at"] and digest(item["description"]) != link["description_sha256"]:
                        raise Conflict("Описание изменилось после подтверждения привязки")
                    description = remove_legacy_tail(item["description"], old)
                    name = titled(item["name"], code)
                    listing_text(name, "name")
                    listing_text(description, "description")
                    record.update(
                        ready=True,
                        before=before,
                        after={
                            **before,
                            "name": name,
                            "description": description,
                            "critical": {**before["critical"], "name": code},
                        },
                    )
                if not any(i["ready"] and i["before"] != i["after"] for i in value["items"]):
                    raise Conflict("Готовых изменений нет. Проверьте ограничения архивных объявлений")
            except (Conflict, ValueError, APIError) as error:
                value["ready"] = False
                value["problem"] = (
                    str(error)
                    if isinstance(error, ValueError) and not isinstance(error, APIError)
                    else "Playerok не подтвердил предварительную проверку"
                )
            plans.append(value)
        return {"owner": owner, "account": account, "products": plans, "at": now()}

    def queue(self, owner, plan):
        self.logical.authority(owner, plan["account"], writes=True)
        if owner != plan["owner"]:
            raise Conflict("Подтверждение принадлежит другому владельцу")
        operations = []
        with self.store.transaction() as db:
            for value in plan["products"]:
                if not value["ready"]:
                    continue
                product = self.logical.product(value["product_id"], owner)
                if config_hash(product) != value["hash"]:
                    raise Conflict("Настройки изменились после превью")
                self.idle(product["id"])
                existing = db.execute(
                    "SELECT * FROM logical_code_migrations WHERE product_id=?", (product["id"],)
                ).fetchone()
                if existing and existing["state"] in ("PENDING", "PROCESSING", "UNCERTAIN"):
                    continue
                if (
                    existing
                    and existing["plan"]
                    and json.loads(existing["plan"]).get("plan_id") == value["plan_id"]
                ):
                    continue
                actual_ids = {
                    row[0]
                    for row in db.execute(
                        "SELECT item_id FROM logical_listings WHERE product_id=?", (product["id"],)
                    )
                }
                planned_ids = [item["id"] for item in value["items"]]
                if set(planned_ids) != actual_ids or len(planned_ids) != len(actual_ids):
                    raise Conflict("Связанные объявления изменились после превью")
                for item in value["items"]:
                    if not item["ready"]:
                        continue
                    before = item["before"]
                    name = titled(before["name"], value["code"])
                    expected = {
                        **before,
                        "name": name,
                        "description": remove_legacy_tail(before["description"], value["old_code"]),
                        "critical": {**before["critical"], "name": value["code"]},
                    }
                    if (
                        item["after"] != expected
                        or before["id"] != item["id"]
                        or before["critical"]["account"] != product["account_id"]
                    ):
                        raise Conflict("Изменение выходит за подтверждённый перенос идентификатора")
                code = reserve_code(db, product["id"], value["code"])
                db.execute(
                    "INSERT INTO logical_code_migrations(product_id,account_id,owner_id,old_code,new_code,state,plan,configuration_hash,was_enabled,revision,control_revision,created_at,updated_at) VALUES(?,?,?,?,?,'PENDING',?,?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET new_code=excluded.new_code,state='PENDING',plan=excluded.plan,configuration_hash=excluded.configuration_hash,updated_at=excluded.updated_at",
                    (
                        product["id"],
                        plan["account"],
                        owner,
                        value["old_code"],
                        code,
                        encode(value),
                        value["hash"],
                        product["enabled"],
                        product["revision"],
                        product["control_revision"],
                        now(),
                        now(),
                    ),
                )
                db.execute(
                    "UPDATE logical_products SET code=?,state='BLOCKED',error_code='CODE_MIGRATING' WHERE id=?",
                    (code, product["id"]),
                )
                for item in value["items"]:
                    if not item["ready"]:
                        continue
                    previous = db.execute(
                        "SELECT state FROM logical_code_migration_items WHERE item_id=?", (item["id"],)
                    ).fetchone()
                    if previous and previous["state"] == "VERIFIED":
                        continue
                    key = "title-code:" + product["id"] + ":" + item["id"] + ":" + value["plan_id"]
                    op = self.store.enqueue(
                        key,
                        "edit",
                        item["id"],
                        {
                            "name": item["after"]["name"],
                            "description": item["after"]["description"],
                            "code_migration": {
                                "product_id": product["id"],
                                "hash": value["hash"],
                                "definition_hash": value["definition_hash"],
                                "code": code,
                                "before": item["before"],
                            },
                        },
                        db,
                        actor=owner,
                    )
                    db.execute(
                        "INSERT INTO logical_code_migration_items(item_id,product_id,state,before_snapshot,after_snapshot,operation_id,updated_at) VALUES(?,?,'PENDING',?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET state='PENDING',before_snapshot=excluded.before_snapshot,after_snapshot=excluded.after_snapshot,operation_id=excluded.operation_id,updated_at=excluded.updated_at",
                        (item["id"], product["id"], encode(item["before"]), encode(item["after"]), op, now()),
                    )
                    operations.append(op)
                self.store.audit(owner, "CODE_MIGRATION", product["id"], "OWNER_CONFIRMED")
        return operations

    def guard(self, action, current=None, *, writes=True):
        value = action["payload"]["code_migration"]
        product = self.logical.product(value["product_id"])
        self.logical.authority(product["owner_id"], product["account_id"], writes=writes)
        rows = self.store.rows(
            "SELECT * FROM logical_code_migration_items WHERE item_id=? AND operation_id=?",
            (action["target"], action["id"]),
        )
        journal = self.store.rows(
            "SELECT state FROM logical_code_migrations WHERE product_id=?", (product["id"],)
        )
        if (
            not rows
            or not journal
            or (writes and journal[0]["state"] not in {"PENDING", "PROCESSING"})
            or product["code"] != value["code"]
            or (
                config_hash(product) != value["hash"]
                if writes
                else definition_hash(product) != value["definition_hash"]
            )
            or product["state"] != "BLOCKED"
        ):
            raise Conflict("План переноса изменился. Требуется новое превью")
        if writes:
            self.idle(product["id"])
        if current is not None and (
            (current.get("user") or {}).get("id") != product["account_id"]
            or current.get("is_editable") is not True
            or snapshot(current) != json.loads(rows[0]["before_snapshot"])
        ):
            raise Conflict("Объявление изменилось или право редактирования отозвано")
        return product, rows[0]

    def confirm(self, action, observed, *, writes=True):
        product, row = self.guard(action, writes=writes)
        expected = json.loads(row["after_snapshot"])
        if (
            (observed.get("user") or {}).get("id") != product["account_id"]
            or observed.get("id") != action["target"]
            or snapshot(observed) != expected
            or title_code(observed["name"]) != product["code"]
        ):
            raise Conflict("Playerok не подтвердил точные поля переноса")
        return {
            "product_id": product["id"],
            "item_id": row["item_id"],
            "hash": action["payload"]["code_migration"]["hash"],
            "snapshot": expected,
            "status": observed.get("status"),
        }

    async def reconcile(self, owner, item_id):
        rows = self.store.rows(
            "SELECT o.* FROM outbox o JOIN logical_code_migration_items m ON m.operation_id=o.id WHERE m.item_id=? AND o.status='UNCERTAIN'",
            (item_id,),
        )
        if len(rows) != 1:
            raise Conflict("Нет единственного неизвестного переноса для сверки")
        action = {**rows[0], "payload": json.loads(rows[0]["payload"])}
        product = self.logical.product(action["payload"]["code_migration"]["product_id"], owner)
        self.logical.authority(owner, product["account_id"])
        receipt = self.confirm(action, await self.api.call("get_item", id=item_id), writes=False)
        with self.store.transaction() as db:
            changed = db.execute(
                "UPDATE outbox SET status='SUCCEEDED',result=?,error_code=NULL,lease_token=NULL,lease_until=NULL WHERE id=? AND status='UNCERTAIN'",
                (encode({"code_migration": receipt}), action["id"]),
            ).rowcount
            if not changed:
                raise Conflict("Результат уже изменился")
            db.execute(
                "INSERT INTO operation_resolutions(operation_id,actor,decision,reason,evidence,previous_error_code,previous_result,resolved_at) VALUES(?,?,'CONFIRM_READ_STATE','Подтверждён новый код товара',?,?,?,?)",
                (action["id"], owner, encode(receipt), action["error_code"], action["result"], now()),
            )
            finish_migration(db, action, "SUCCEEDED", {"code_migration": receipt})
            self.store.audit(owner, "CODE_MIGRATION_RECONCILE", action["id"], "CONFIRMED_READ_ONLY")
        return receipt


def finish_migration(db, action, status, result):
    value = json.loads(action["payload"]) if isinstance(action["payload"], str) else action["payload"]
    if not value.get("code_migration"):
        return
    product_id = value["code_migration"]["product_id"]
    state = (
        "VERIFIED"
        if status == "SUCCEEDED" and result and result.get("code_migration")
        else "UNCERTAIN"
        if status == "UNCERTAIN"
        else "BLOCKED"
    )
    db.execute(
        "UPDATE logical_code_migration_items SET state=?,error_code=?,verified_at=?,updated_at=? WHERE operation_id=?",
        (
            state,
            None if state == "VERIFIED" else state,
            now() if state == "VERIFIED" else None,
            now(),
            action["id"],
        ),
    )
    if state == "VERIFIED":
        proof = result["code_migration"]
        observed = proof["snapshot"]
        db.execute(
            "UPDATE logical_listings SET description_sha256=?,verified_at=?,server_status=? WHERE item_id=? AND product_id=?",
            (digest(observed["description"]), now(), proof["status"], proof["item_id"], product_id),
        )
        db.execute(
            "UPDATE logical_listings SET title_snapshot=? WHERE item_id=? AND product_id=?",
            (observed["name"], proof["item_id"], product_id),
        )
        db.execute(
            "UPDATE gift_listings SET description_sha256=? WHERE item_id=?",
            (digest(observed["description"]), proof["item_id"]),
        )
    pending = db.execute(
        "SELECT 1 FROM logical_code_migration_items WHERE product_id=? AND state!='VERIFIED' LIMIT 1",
        (product_id,),
    ).fetchone()
    if not pending and state == "VERIFIED":
        db.execute(
            "UPDATE logical_products SET baseline=?,state='VERIFIED',error_code=NULL,updated_at=? WHERE id=?",
            (encode(result["code_migration"]["snapshot"]["critical"]), now(), product_id),
        )
        journal = db.execute(
            "SELECT plan FROM logical_code_migrations WHERE product_id=?", (product_id,)
        ).fetchone()
        partial = any(not item["ready"] for item in json.loads(journal[0])["items"])
        db.execute(
            "UPDATE logical_code_migrations SET state=?,confirmed_at=?,updated_at=? WHERE product_id=?",
            ("MANUAL_REVIEW_REQUIRED" if partial else "VERIFIED", now(), now(), product_id),
        )
    elif state != "VERIFIED":
        db.execute(
            "UPDATE logical_code_migrations SET state=?,updated_at=? WHERE product_id=?",
            (state, now(), product_id),
        )
    elif not db.execute(
        "SELECT 1 FROM logical_code_migration_items WHERE product_id=? AND state IN ('UNCERTAIN','BLOCKED') LIMIT 1",
        (product_id,),
    ).fetchone():
        db.execute(
            "UPDATE logical_code_migrations SET state='PENDING',updated_at=? WHERE product_id=?",
            (now(), product_id),
        )
