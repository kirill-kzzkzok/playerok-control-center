"""Atomic gift journal hooks called inside Store's existing finish/recover transaction."""

from app.database.store import encode, now


def finish(db, action, status, result, error):
    job = db.execute("SELECT * FROM gift_jobs WHERE order_id=?", (action["target"],)).fetchone()
    if not job:
        return
    if action["kind"] == "gift_send":
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
