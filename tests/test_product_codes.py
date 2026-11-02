"""Identity primitives against a temporary SQLite; no external integrations."""

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.database.store import Conflict, Store
from app.services.product_codes import choose_code, remove_legacy_tail, reserve_code, title_code, titled


@pytest.mark.parametrize(
    "name,code",
    [("Product [AB6]", "AB6"), ("Product [ab6]", "AB6"), ("❤️ Зомби [MY2]", "MY2"), ("Plain name", None)],
)
def test_strict_suffix(name, code):
    assert title_code(name) == code


@pytest.mark.parametrize(
    "name",
    [
        "[AB6] Product",
        "Product [AB6] [LF9]",
        "Product [AB!]",
        "Product [ABCD]",
        "Product [AB6",
        "Product AB6]",
    ],
)
def test_ambiguous_or_malformed_suffix_is_rejected(name):
    with pytest.raises(ValueError):
        title_code(name)


def test_title_preserves_unicode_suffix_and_utf16_budget():
    assert titled("❤️ Название", "ab6") == "❤️ Название [AB6]"
    assert titled("❤️ Название [ab6]", "AB6") == "❤️ Название [AB6]"
    with pytest.raises(Conflict):
        titled("Product [LF9]", "AB6")
    with pytest.raises(ValueError):
        titled("🎁" * 34, "AB6")
    assert titled("x" * 66, "AB6") == "x" * 66 + " [AB6]"


def test_legacy_removal_is_exact_and_keeps_user_hashtags_and_format():
    source = "Description #steam\n\n#2163148\n"
    assert remove_legacy_tail(source, "#2163148") == "Description #steam\n\n"
    assert remove_legacy_tail("Text #1234567 remains\n#steam", "#2163148") == "Text #1234567 remains\n#steam"
    with pytest.raises(Conflict):
        remove_legacy_tail("#2163148\nText", "#2163148")


def test_code_is_stable_normalized_reserved_forever_and_unique(tmp_path):
    store = Store(tmp_path / "codes.sqlite")
    with store.transaction() as db:
        assert reserve_code(db, "product-1", "ab6") == "AB6"
        assert reserve_code(db, "product-1") == "AB6"
        with pytest.raises(Conflict):
            reserve_code(db, "product-2", "AB6")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM logical_code_reservations WHERE code='AB6'")
    with store.transaction() as db:
        code = reserve_code(db, "product-2")
        assert re.fullmatch("[A-Z0-9]{3}", code)
        assert code != "AB6"
    store.close()


def test_collision_retries_are_bounded(tmp_path, monkeypatch):
    store = Store(tmp_path / "collision.sqlite")
    with store.transaction() as db:
        reserve_code(db, "one", "AAA")
        values = iter("AAA" + "BBB")
        monkeypatch.setattr("app.services.product_codes.secrets.choice", lambda alphabet: next(values))
        assert reserve_code(db, "two") == "BBB"
        monkeypatch.setattr("app.services.product_codes.secrets.choice", lambda alphabet: "A")
        with pytest.raises(Conflict):
            choose_code(db)
    store.close()


def test_two_connections_cannot_reserve_same_code(tmp_path):
    path = tmp_path / "concurrent.sqlite"
    Store(path).close()

    def reserve(product):
        store = Store(path)
        try:
            with store.transaction() as db:
                return reserve_code(db, product, "AB6")
        except Conflict:
            return "CONFLICT"
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["one", "two"]))
    assert sorted(results) == ["AB6", "CONFLICT"]


def test_database_rejects_non_normalized_direct_code_insert(tmp_path):
    store = Store(tmp_path / "normal.sqlite")
    with store.transaction() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO logical_code_reservations VALUES('qZ9','raw','now')")
    store.close()
