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
