"""Baseline behavior from the pinned upstream; no network or credentials."""

import ast
from pathlib import Path


def test_create_and_publish_are_separate():
    tree = ast.parse(Path("playerokapi/account.py").read_text(encoding="utf-8"))
    account = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Account")
    methods = {n.name: n for n in account.body if isinstance(n, ast.FunctionDef)}
    create = ast.get_source_segment(
        Path("playerokapi/account.py").read_text(encoding="utf-8"), methods["create_item"]
    )
    assert '"createItem"' in create
    assert "publish_item(" not in create
    assert {
        "create_item",
        "publish_item",
        "get_item_priority_statuses",
        "get_game_category_data_fields",
    } <= methods.keys()


def test_seller_sent_is_not_buyer_confirmation():
    from playerokapi.enums import ItemDealStatuses

    assert ItemDealStatuses.SENT != ItemDealStatuses.CONFIRMED


def test_update_attachment_map_regression():
    text = Path("playerokapi/account.py").read_text(encoding="utf-8")
    update = text.split("    def update_item(", 1)[1].split("    def remove_item(", 1)[0]
    assert 'f"variables.addedAttachments.{i-1}"' in update
