"""Atomic gift journal hooks called inside Store's existing finish/recover transaction."""

import json
import uuid

from app.database.store import encode, now

DELAYED_PREFLIGHT_SQL = (
    "SELECT b.id,b.target FROM outbox b WHERE b.status='RETRYABLE_FAILED' "
    "AND b.started_at IS NULL AND b.attempts>=3 AND julianday(b.created_at)<julianday('now','-5 minutes') "
    "AND NOT EXISTS(SELECT 1 FROM outbox_late_results x WHERE x.operation_id=b.id) AND ((b.kind='gift_send' "
    "AND b.error_code NOT IN ('GIFT_TEMPORARY_PAUSE','PAYMENT_LOCKED','RECIPIENT_DATA_PENDING') "
    "AND EXISTS(SELECT 1 FROM gift_jobs g JOIN gift_listings l ON l.item_id=g.item_id "
    "WHERE g.operation_id=b.id AND l.enabled=1 AND g.state IN ('PENDING','CHECKING')) "
    "AND COALESCE((SELECT json_extract(value,'$') FROM settings WHERE key='gifts:stopped'),1)=0 "
    "AND COALESCE((SELECT json_extract(value,'$') FROM settings WHERE key='gifts:paused'),0)=0) OR "
    "(b.kind IN ('delivery','delivery_text','chain_message','fulfill') "
    "AND EXISTS(SELECT 1 FROM orders o WHERE o.id=b.target AND o.status='PAID') "
    "AND EXISTS(SELECT 1 FROM delivery_chains c JOIN delivery_policies p ON p.item_id=c.item_id "
    "WHERE c.order_id=b.target AND c.status='ACTIVE' AND json_extract(p.payload,'$.enabled')=1)))"
)


def track_preflight_wait(db):
    """One diagnostic incident for a prolonged outage, not one alert per paid order."""
    waiting = db.execute(DELAYED_PREFLIGHT_SQL).fetchall()
    row = db.execute("SELECT value FROM settings WHERE key='gifts:preflight_incident'").fetchone()
    try:
        incident = json.loads(row[0]) if row else {}
    except (ValueError, TypeError):
        incident = {}
    if not waiting:
        if row:
            db.execute("UPDATE settings SET value='{}' WHERE key='gifts:preflight_incident'")
        return
    if not isinstance(incident, dict) or not isinstance(incident.get("id"), str):
        incident = {"id": uuid.uuid4().hex, "at": now()}
        db.execute(
            "INSERT INTO settings VALUES('gifts:preflight_incident',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (encode(incident),),
        )
    db.execute(
        "INSERT OR IGNORE INTO inbox(key,kind,payload,received_at,processed_at) VALUES(?,'delivery_delayed',?,?,?)",
        ("gift-wait:" + incident["id"], encode({"incident": incident["id"]}), now(), now()),
    )


def unstarted_preflight(db, action, *, blocked=False):
    """Positive journal proof, never absence of a response after payment."""
    job = db.execute(
        "SELECT j.*,s.position FROM outbox o JOIN gift_jobs j ON j.operation_id=o.id "
        "JOIN gift_steps s ON s.operation_id=o.id WHERE o.id=? "
        "AND o.started_at IS NULL AND (o.result IS NULL OR o.result='null') "
        "AND ((?=0 AND j.state IN ('PENDING','CHECKING') AND s.state IN ('PENDING','CHECKING')) "
        "OR (?=1 AND j.state='BLOCKED' AND s.state='BLOCKED')) "
        "AND j.refund_state='NONE' AND j.receipt IS NULL "
        "AND s.spent_at IS NULL AND s.form_id IS NULL AND s.receipt IS NULL "
        "AND s.message_id IS NULL AND s.ack_message_id IS NULL "
        "AND NOT EXISTS(SELECT 1 FROM outbox_late_results r WHERE r.operation_id=o.id)",
        (action["id"], int(blocked), int(blocked)),
    ).fetchone()
    if not job:
        return False
    steps = db.execute(
        "SELECT s.*,o.status AS operation_status,o.started_at AS operation_started,o.result AS operation_result,"
        "EXISTS(SELECT 1 FROM outbox_late_results r WHERE r.operation_id=s.operation_id) AS late_result "
        "FROM gift_steps s LEFT JOIN outbox o ON o.id=s.operation_id WHERE s.order_id=? ORDER BY s.position",
        (job["order_id"],),
    ).fetchall()
    if [s["position"] for s in steps] != list(range(len(steps))):
        return False
    previous = steps[: job["position"]]
    remaining = steps[job["position"] :]
    if not remaining or any(
        s["spent_at"]
        or s["form_id"]
        or s["receipt"]
        or s["message_id"]
        or s["ack_message_id"]
        or s["state"] in {"SPENDING", "UNCERTAIN", "SUCCEEDED"}
        or s["late_result"]
        for s in remaining
    ):
        return False
    if any(s["operation_id"] for s in remaining[1:]):
        return False
    known_paid = set()
    receipt_ids = set()
    for step in previous:
        if (
            step["state"] != "SUCCEEDED"
            or not step["spent_at"]
            or not step["form_id"]
            or not step["message_id"]
            or step["operation_status"] != "SUCCEEDED"
            or not step["operation_started"]
            or step["late_result"]
            or not job["recipient_id"]
            or not job["payer_id"]
            or step["recipient_id"] != job["recipient_id"]
            or step["payer_id"] != job["payer_id"]
            or step["receipt_mode"] == "MESSAGE_ID"
            and step["ack_message_id"] != step["message_id"]
        ):
            return False
        try:
            receipt = json.loads(step["receipt"])
            result = json.loads(step["operation_result"])
            if (
                not isinstance(receipt, dict)
                or not isinstance(result, dict)
                or result.get("gift_receipt") != receipt
                or receipt.get("message_id") != step["message_id"]
                or receipt.get("recipient_id") != step["recipient_id"]
                or str(receipt.get("gift_id")) != str(step["gift_id"])
                or receipt.get("reference") != step["reference"]
            ):
                return False
        except (ValueError, TypeError):
            return False
        if step["message_id"] in receipt_ids:
            return False
        receipt_ids.add(step["message_id"])
        known_paid.add(step["operation_id"])
    history = db.execute(
        "SELECT * FROM outbox WHERE kind='gift_send' AND target=?", (job["order_id"],)
    ).fetchall()
    for operation in history:
        if operation["id"] not in known_paid and (
            operation["started_at"]
            or operation["result"] not in {None, "null"}
            or db.execute(
                "SELECT 1 FROM outbox_late_results WHERE operation_id=?", (operation["id"],)
            ).fetchone()
        ):
            return False
    if not previous:
        return not any(job[k] for k in ("spent_at", "form_id", "message_id", "reserved_stars"))
    return bool(
        job["spent_at"]
        and job["form_id"] == previous[-1]["form_id"]
        and job["message_id"] == previous[-1]["message_id"]
        and job["stars"] == sum(s["stars"] for s in previous)
        and job["reserved_stars"] == sum(s["stars"] for s in remaining)
    )


def finish(db, action, status, result, error, store=None):
    job = db.execute("SELECT * FROM gift_jobs WHERE order_id=?", (action["target"],)).fetchone()
    if not job:
        return
    if action["kind"] == "gift_send":
        step = db.execute("SELECT * FROM gift_steps WHERE operation_id=?", (action["id"],)).fetchone()
        if step:
            if status == "RETRYABLE_FAILED" and unstarted_preflight(db, action):
                db.execute(
                    "UPDATE gift_steps SET state='PENDING',error_code=? WHERE operation_id=?",
                    (error, action["id"]),
                )
                db.execute(
                    "UPDATE gift_jobs SET state='PENDING',error_code=?,updated_at=? WHERE order_id=?",
                    (error, now(), job["order_id"]),
                )
                return
            if status == "SUCCEEDED" and step["state"] == "SPENDING":
                receipt = (result or {}).get("gift_receipt")
                if not receipt or not receipt.get("message_id"):
                    raise ValueError("Gift delivery receipt required")
                db.execute(
                    "UPDATE gift_steps SET state='SUCCEEDED',receipt=?,message_id=? WHERE operation_id=?",
                    (encode(receipt), receipt["message_id"], action["id"]),
                )
                db.execute(
                    "UPDATE gift_jobs SET message_id=?,stars=(SELECT SUM(stars) FROM gift_steps WHERE order_id=? AND spent_at IS NOT NULL),updated_at=? WHERE order_id=?",
                    (receipt["message_id"], job["order_id"], now(), job["order_id"]),
                )
                from app.gifts.service import GiftService

                GiftService(store, None, None).continue_bundle(job["order_id"], db)
            else:
                state = "UNCERTAIN" if step["state"] == "SPENDING" else "BLOCKED"
                db.execute(
                    "UPDATE gift_steps SET state=?,error_code=? WHERE operation_id=?",
                    (state, error or "CHECK_INTERRUPTED", action["id"]),
                )
                if job["state"] != "INVALID_RECIPIENT":
                    db.execute(
                        "UPDATE gift_jobs SET state=?,error_code=?,updated_at=? WHERE order_id=?",
                        (state, error or "CHECK_INTERRUPTED", now(), job["order_id"]),
                    )
            return
        if status == "SUCCEEDED" and job["state"] == "SPENDING":
            receipt = (result or {}).get("gift_receipt")
            if not receipt or not receipt.get("message_id"):
                raise ValueError("Gift delivery receipt required")
            db.execute(
                "UPDATE gift_jobs SET state='SUCCEEDED',receipt=?,message_id=?,updated_at=? WHERE order_id=?",
                (encode(receipt), receipt["message_id"], now(), job["order_id"]),
            )
        elif job["state"] == "SPENDING":
            db.execute(
                "UPDATE gift_jobs SET state='UNCERTAIN',error_code=?,updated_at=? WHERE order_id=?",
                (error or "PAYMENT_UNKNOWN", now(), job["order_id"]),
            )
        elif job["state"] in {"PENDING", "CHECKING"}:
            db.execute(
                "UPDATE gift_jobs SET state='BLOCKED',error_code=?,updated_at=? WHERE order_id=?",
                (error or "CHECK_INTERRUPTED", now(), job["order_id"]),
            )
    if action["kind"] == "gift_refund":
        state = (
            "CONFIRMED"
            if status == "SUCCEEDED"
            else "UNCERTAIN"
            if status == "UNCERTAIN" or job["refund_state"] == "SPENDING"
            else "FAILED"
        )
        db.execute(
            "UPDATE gift_jobs SET refund_state=?,updated_at=? WHERE order_id=?",
            (state, now(), job["order_id"]),
        )


def recover(db, action):
    if action["kind"] == "gift_send":
        db.execute(
            "UPDATE gift_steps SET state=CASE WHEN state='SPENDING' THEN 'UNCERTAIN' WHEN state IN ('PENDING','CHECKING') THEN 'BLOCKED' ELSE state END,error_code='PROCESS_INTERRUPTED' WHERE operation_id=?",
            (action["id"],),
        )
        db.execute(
            "UPDATE gift_jobs SET state=CASE WHEN state='SPENDING' THEN 'UNCERTAIN' "
            "WHEN state IN ('PENDING','CHECKING') THEN 'BLOCKED' ELSE state END,"
            "error_code='PROCESS_INTERRUPTED',updated_at=? WHERE order_id=?",
            (now(), action["target"]),
        )
    elif action["kind"] == "gift_refund":
        db.execute(
            "UPDATE gift_jobs SET refund_state='UNCERTAIN',updated_at=? WHERE order_id=?",
            (now(), action["target"]),
        )
