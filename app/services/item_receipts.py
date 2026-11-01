from decimal import Decimal, InvalidOperation


def edit_receipt_matches(target, changes, result):
    """Positive evidence for requested fields, never just a matching item ID."""
    if not isinstance(result, dict) or result.get("id") != target:
        return False
    for field in ("name", "description"):
        if field in changes and result.get(field) != changes[field]:
            return False
    if "price" in changes:
        try:
            observed = Decimal(str(result.get("price")))
            if not observed.is_finite() or observed != Decimal(str(changes["price"])):
                return False
        except (ValueError, InvalidOperation):
            return False
    if "add_attachments" in changes:
        images = result.get("attachments")
        if not isinstance(images, list) or len(images) != len(changes["add_attachments"]):
            return False
        ids = []
        for image in images:
            if not isinstance(image, dict) or not isinstance(image.get("id"), str) or not image["id"]:
                return False
            ids.append(image["id"])
        if len(set(ids)) != len(ids) or set(ids).intersection(changes.get("remove_attachments", [])):
            return False
    return True


def field_receipt_matches(target, observed, attributes, fields, comment):
    if not isinstance(observed, dict) or observed.get("id") != target:
        return False
    actual = observed.get("attributes")
    if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in attributes.items()):
        return False
    if comment is not None and observed.get("comment") != comment:
        return False
    values = observed.get("data_fields")
    if not isinstance(values, list):
        return False
    indexed = {value.get("id"): value.get("value") for value in values if isinstance(value, dict)}
    return all(indexed.get(field.id) == field.value for field in fields)
