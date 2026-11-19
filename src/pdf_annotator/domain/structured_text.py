"""Minimal page text and internal export binding; no paragraph model."""

from dataclasses import asdict, dataclass
import json
import re
from typing import Any

MAX_PAGE_JSON_BYTES = 64 * 1024 * 1024

_LINE_BREAK = r"\r\n|[\r\n\v\f\x85\u2028\u2029]"
_LINE_HYPHEN = re.compile(r"([^\W\d_])-[ \t]*(?:" + _LINE_BREAK + r")[ \t]*(?=([^\W\d_]))")


def normalize_page_text(text: str) -> str:
    """M24 only: join alphabetic line-end hyphens, then flatten line breaks.

    A genuine compound broken after its hyphen is indistinguishable here.
    The right letter is lookahead so successive broken lines can both join.
    """
    text = _LINE_HYPHEN.sub(
        lambda match: match[1] if match[1].isalpha() and match[2].isalpha() else match[0], text)
    text = re.sub(_LINE_BREAK, " ", text)
    return text if text.strip() else ""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def integer(value: Any, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise ValueError("Invalid extraction integer")


@dataclass(frozen=True, slots=True)
class ExtractionIdentity:
    """Transport precondition only; never written in the user's JSON."""
    document_id: str
    document_generation: int
    document_revision: int

    def __post_init__(self) -> None:
        if not isinstance(self.document_id, str) or not 0 < len(self.document_id) <= 128:
            raise ValueError("Invalid extraction document")
        integer(self.document_generation, 1)
        integer(self.document_revision)


@dataclass(frozen=True, slots=True)
class PageText:
    page: int
    text: str

    def __post_init__(self) -> None:
        integer(self.page, 1)
        if not isinstance(self.text, str) or "\n" in self.text or "\r" in self.text:
            raise ValueError("Invalid page text")
        if self.text and not self.text.strip():
            raise ValueError("Whitespace-only page must be empty")

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PageText":
        if not isinstance(payload, dict) or set(payload) != {"page", "text"}:
            raise ValueError("Invalid page text fields")
        return cls(**payload)
