from concurrent.futures import ThreadPoolExecutor

import pytest

from app.database.store import Conflict, Store


def test_concurrent_changes_to_same_target_are_serialized_across_connections(tmp_path):
    path = tmp_path / "locks.sqlite"
    Store(path).close()

    def enqueue(index):
        store = Store(path)
        try:
            return store.enqueue(f"edit:{index}", "edit", "item", {"price": 100 + index})
        except Conflict:
            return None
        finally:
            store.close()

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(enqueue, range(8)))
    assert sum(result is not None for result in results) == 1


def test_uncertain_operation_blocks_new_change_until_reconciled(tmp_path):
    store = Store(tmp_path / "locks.sqlite")
    try:
        ident = store.enqueue("edit:one", "edit", "item", {"price": 100})
        action = store.claim()
        store.finish(ident, action["lease_token"], "UNCERTAIN", error="NETWORK_ERROR")
        assert store.enqueue("edit:one", "edit", "item", {"price": 100}) == ident
        with pytest.raises(Conflict):
            store.enqueue("edit:two", "delete", "item", {})
        assert store.enqueue("edit:other", "edit", "another-item", {"price": 300}) != ident
    finally:
        store.close()
