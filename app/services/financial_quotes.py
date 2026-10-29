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


def save_quote(store, owner, target, inputs, result):
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
    }
    ident = uuid.uuid4().hex
    store.execute(
        "INSERT INTO financial_quotes VALUES(?,?,?,?,?)", (ident, owner, target, now(), encode(payload))
    )
    return ident
