"""Controlled live draft test. Never publishes, promotes, pays, or contacts buyers.

The user authorized creation/edit/deletion of isolated test lots. This journal
records identity before any cleanup; the normal runtime stays read-only.
"""

import asyncio
import json
import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw

from app.config import Config
from app.database.store import now
from app.playerok.adapter import APIError, Playerok, SafeAccount
from app.services.products import Products, category_form, validate_field

JOURNAL = Path("data/controlled_live_test.json")


def save(journal):
    JOURNAL.parent.mkdir(exist_ok=True)
    temp = JOURNAL.with_suffix(".tmp")
    temp.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(JOURNAL)


async def mutate(account, operation, method, **kwargs):
    def run():
        with account.permit(operation):
            return getattr(account, method)(**kwargs)

    return await asyncio.to_thread(run)


async def main():
    if JOURNAL.exists():
        previous = json.loads(JOURNAL.read_text(encoding="utf-8"))
        if (
            previous.get("reason")
            == "Playerok rejected technical listing as not compliant with product rules"
        ):
            print("Playerok rejected the technical listing under its product rules; no automatic retry.")
            return
        if previous.get("state") != "CLEANED" and not (
            previous.get("state") == "FAILED" and not previous.get("remote_id")
        ):
            print("Existing unfinished live journal: reconcile it before any new test.")
            return
    config = Config.load()
    account = SafeAccount(config.playerok_header, writes_enabled=True)
    api = Playerok(account)
    journal = {
        "started_at": now(),
        "name": "PCC TEST DO NOT BUY " + uuid.uuid4().hex[:12],
        "state": "PREPARING",
        "paid_operations": 0,
        "published": False,
        "steps": [],
    }
    save(journal)
    ident = None
    try:
        viewer = await api.call("get")
        if not viewer.get("can_publish_items"):
            raise ValueError("SELLING_NOT_AVAILABLE")
        games = await api.call("get_games", name="Steam", count=1)
        game = await api.call("get_game", id=games["games"][0]["id"])
        category_id = next(c["id"] for c in game["categories"] if c.get("slug") == "other")
        category = await api.call("get_game_category", id=category_id)
        obtaining = await api.call("get_game_category_obtaining_types", game_category_id=category_id)
        obtaining_id = obtaining["obtaining_types"][0]["id"] if obtaining["obtaining_types"] else None
        fields = []
        async for page in api.pages(
            "get_game_category_data_fields",
            "data_fields",
            game_category_id=category_id,
            obtaining_type_id=obtaining_id,
        ):
            fields.extend(page)
        form = category_form(category, fields)
        values = {}
        for field in form:
            if field["kind"] == "choice":
                value = str(field["options"][0]["value"])
            elif field["kind"] == "number":
                value = str((field["options"][0].get("value_range_limit") or {}).get("min") or 1)
            elif field["kind"] == "text":
                value = "PCC isolated test data" if field["required"] else ""
            else:
                raise ValueError("UNSUPPORTED_TEST_SCHEMA")
            values[field["key"]] = validate_field(field, value)
        image = Image.new("RGB", (800, 600), "#20152e")
        ImageDraw.Draw(image).text((60, 280), "PCC TEST - NOT FOR SALE", fill="white")
        buffer = BytesIO()
        image.save(buffer, "JPEG")
        journal["state"] = "CREATE_IN_FLIGHT"
        save(journal)
        # No automatic retry on timeout. Draft creation is separate from publication.
        created = await mutate(
            account,
            "createItem",
            "create_item",
            game_category_id=category_id,
            obtaining_type_id=obtaining_id,
            name=journal["name"],
            price=100,
            description="Изолированный технический тест PCC. Не предназначен для покупки. Будет удалён после проверки.",
            options={f["key"]: values[f["key"]] for f in form if not f.get("data_field")},
            data_fields=[
                SimpleNamespace(id=f["key"], value=values[f["key"]]) for f in form if f.get("data_field")
            ],
            attachments=[buffer.getvalue()],
        )
        ident = getattr(created, "id", None)
        if not ident:
            raise APIError("CREATE_ID_MISSING", uncertain=True)
        journal["remote_id"], journal["state"] = ident, "CREATED"
        journal["steps"].append("create_response_received")
        save(journal)
        item = await api.call("get_item", id=ident)
        if (
            (item.get("user") or {}).get("id") != account.id
            or item.get("name") != journal["name"]
            or item.get("status") != "DRAFT"
        ):
            raise ValueError("TEST_OWNERSHIP_OR_STATUS_MISMATCH")
        journal["steps"].append("own_draft_read_verified")
        journal["attachment_count"] = len(item.get("attachments") or [])
        journal["quotes"] = await Products(None, api).priority_preview(ident, 100)
        save(journal)
        await mutate(
            account,
            "updateItem",
            "update_item",
            id=ident,
            description="PCC isolated draft edit test. NOT FOR SALE.",
        )
        item = await api.call("get_item", id=ident)
        journal["edit_verified"] = item.get("description") == "PCC isolated draft edit test. NOT FOR SALE."
        journal["state"] = "TESTED"
        save(journal)
    except (APIError, ValueError, KeyError, TypeError, IndexError, StopIteration) as error:
        journal["error_code"] = getattr(error, "code", type(error).__name__)
        journal["state"] = "UNCERTAIN" if journal["state"] == "CREATE_IN_FLIGHT" else "FAILED"
        save(journal)
    finally:
        if ident:
            try:
                item = await api.call("get_item", id=ident)
                if (item.get("user") or {}).get("id") != account.id or item.get("name") != journal["name"]:
                    raise ValueError("CLEANUP_IDENTITY_MISMATCH")
                journal["state"] = "CLEANUP_IN_FLIGHT"
                save(journal)
                await mutate(account, "removeItem", "remove_item", id=ident)
                # Verify through all current own drafts; don't assume a mutation response is enough.
                remaining = []
                from playerokapi.enums import ItemStatuses

                async for page in api.pages("get_my_items", "items", statuses=[ItemStatuses.DRAFT]):
                    remaining.extend(x["id"] for x in page)
                if ident in remaining:
                    raise ValueError("TEST_DRAFT_STILL_PRESENT")
                journal["state"] = "CLEANED"
                journal["cleanup_verified"] = True
            except (APIError, ValueError) as error:
                journal["state"] = "NEEDS_CLEANUP"
                journal["cleanup_error_code"] = getattr(error, "code", type(error).__name__)
            save(journal)
        account.close()
    print(
        json.dumps(
            {
                k: journal.get(k)
                for k in (
                    "state",
                    "error_code",
                    "attachment_count",
                    "edit_verified",
                    "cleanup_verified",
                    "paid_operations",
                    "published",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
