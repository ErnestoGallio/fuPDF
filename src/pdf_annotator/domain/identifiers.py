"""Stable application identities, distinct from PDF implementation locators."""

from dataclasses import dataclass
from uuid import UUID, uuid4


def _uuid(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {name}")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"Invalid {name}") from exc
    if parsed.version != 4:
        raise ValueError(f"Invalid {name}")
    return parsed.hex


@dataclass(frozen=True, slots=True)
class AnnotationId:
    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _uuid(self.value, "annotation identity"))


def new_annotation_id() -> AnnotationId:
    return AnnotationId(uuid4().hex)


def new_command_id() -> str:
    return uuid4().hex


def validate_command_id(value: str) -> str:
    return _uuid(value, "command identity")


def validate_target_id(value: str) -> str:
    return _uuid(value, "target identity")


@dataclass(frozen=True, slots=True)
class PdfBackingReference:
    pdf_nm: str | None = None
    xref: int | None = None
    associated_xrefs: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.pdf_nm is not None and (not isinstance(self.pdf_nm, str)
                                        or not 0 < len(self.pdf_nm) <= 256):
            raise ValueError("Invalid PDF annotation name")
        if self.xref is not None and (type(self.xref) is not int or self.xref <= 0):
            raise ValueError("Invalid PDF xref")
        if (not isinstance(self.associated_xrefs, tuple)
                or any(type(value) is not int or value <= 0 for value in self.associated_xrefs)
                or len(set(self.associated_xrefs)) != len(self.associated_xrefs)):
            raise ValueError("Invalid associated PDF xrefs")

