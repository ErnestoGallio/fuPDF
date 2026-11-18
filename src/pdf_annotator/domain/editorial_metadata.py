"""Plain annotation authorship and optional editorial motivation."""

from dataclasses import dataclass

from .targets import plain_text


@dataclass(frozen=True, slots=True)
class EditorialMetadata:
    reason: str | None = None
    author: str | None = None

    def __post_init__(self) -> None:
        if self.reason is not None:
            plain_text(self.reason)
            self.reason.encode("utf-8", errors="strict")
        if self.author is not None:
            if not isinstance(self.author, str) or not self.author or len(self.author) > 256 or "\x00" in self.author:
                raise ValueError("Invalid editorial author")
            self.author.encode("utf-8", errors="strict")


def validate_metadata(value: EditorialMetadata | None, *, editorial: bool = True) -> None:
    if value is not None and not isinstance(value, EditorialMetadata):
        raise ValueError("Invalid editorial metadata")
    if not editorial and value is not None and value.reason is not None:
        raise ValueError("Motivations are limited to editorial corrections")
