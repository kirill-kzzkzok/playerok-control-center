"""Scope synchronization failures without hiding degraded chat diagnostics."""


def orders_sync_failed(store):
    health = store.setting("sync:health", {})
    if health.get("status") != "ERROR":
        return False
    errors = health.get("errors")
    # Unknown global failures stay conservative. Only explicitly identified
    # chat streams can be excluded from order-delivery admission.
    return not (
        isinstance(errors, dict)
        and errors
        and all(key == "chats" or key.startswith("messages:") for key in errors)
    )


def orders_sync_ready(store):
    return bool(
        store.setting("orders:last_complete_sync")
        and store.setting("sync:health", {}).get("status") in {"OK", "BACKFILL", "ERROR"}
        and not orders_sync_failed(store)
    )


def chat_sync_ready(store, chat_id):
    """A completed own-chat baseline can outlive unrelated history errors."""
    health = store.setting("sync:health", {})
    if health.get("status") == "OK":
        return True  # Backward-compatible complete baseline.
    if not orders_sync_ready(store):
        return False
    if "messages:" + chat_id in (health.get("errors") or {}):
        return False
    rows = store.rows("SELECT initial_complete,error_code FROM sync_chats WHERE id=?", (chat_id,))
    return bool(rows and rows[0]["initial_complete"] and not rows[0]["error_code"])
