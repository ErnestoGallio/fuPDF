"""Strict Structured Outputs contract and local validation for M20.1."""

import json
from typing import Any


OPERATIONS = ("replace", "delete", "insert", "comment")
CATEGORIES = (
    "orthography", "grammar", "syntax", "terminology", "technical", "clarity",
    "verification_needed",
)
ANCHOR_SIDES = ("before", "after")
TEXT_FIELDS = ("original_text", "replacement_text", "context_before", "context_after")
CORRECTION_FIELDS = {
    "page_index", "operation", *TEXT_FIELDS, "reason", "category", "insert_anchor",
}


class InvalidCorrections(ValueError):
    pass


def object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def correction_schema(first: int, last: int) -> dict[str, Any]:
    anchor = object_schema({
        "text": {"type": "string"},
        "side": {"type": "string", "enum": list(ANCHOR_SIDES)},
    })
    item = object_schema({
        "page_index": {"type": "integer", "minimum": first, "maximum": last},
        "operation": {"type": "string", "enum": list(OPERATIONS)},
        **{field: {"type": ["string", "null"]} for field in TEXT_FIELDS},
        "reason": {"type": "string"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "insert_anchor": {"anyOf": [anchor, {"type": "null"}]},
    })
    return object_schema({"corrections": {"type": "array", "items": item}})


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise InvalidCorrections("duplicate_keys")
    return result


def valid_text(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def validate_item(item: Any, first: int, last: int) -> None:
    if not isinstance(item, dict) or item.keys() != CORRECTION_FIELDS:
        raise InvalidCorrections("invalid_correction_fields")
    if type(item["page_index"]) is not int or not first <= item["page_index"] <= last:
        raise InvalidCorrections("invalid_page_index")
    operation = item["operation"]
    if operation not in OPERATIONS or item["category"] not in CATEGORIES:
        raise InvalidCorrections("invalid_operation_or_category")
    if not valid_text(item["reason"]) or not item["reason"].strip():
        raise InvalidCorrections("invalid_reason")
    if any(item[field] is not None and not valid_text(item[field]) for field in TEXT_FIELDS):
        raise InvalidCorrections("invalid_text_field")

    has_original = item["original_text"] is not None
    has_replacement = item["replacement_text"] is not None
    anchor = item["insert_anchor"]
    if operation == "insert":
        if (not isinstance(anchor, dict) or anchor.keys() != {"text", "side"}
                or anchor["side"] not in ANCHOR_SIDES or not valid_text(anchor["text"])
                or not anchor["text"].strip()):
            raise InvalidCorrections("invalid_insert_anchor")
    elif anchor is not None:
        raise InvalidCorrections("unexpected_insert_anchor")

    if (has_replacement != (operation in ("replace", "insert"))
            or (operation in ("replace", "delete") and not has_original)
            or (operation == "insert" and has_original)):
        raise InvalidCorrections("invalid_operation_payload")
    if item["category"] == "verification_needed" and operation != "comment":
        raise InvalidCorrections("verification_requires_comment")


def parse_corrections(text: str, first: int, last: int) -> dict[str, Any]:
    try:
        result = json.loads(text, object_pairs_hook=unique_keys)
    except (ValueError, RecursionError):
        raise InvalidCorrections("invalid_corrections_json") from None
    if (not isinstance(result, dict) or result.keys() != {"corrections"}
            or not isinstance(result["corrections"], list)):
        raise InvalidCorrections("invalid_corrections_schema")
    for item in result["corrections"]:
        validate_item(item, first, last)
    return result
