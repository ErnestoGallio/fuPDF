"""Safe deletion of external logical annotation graphs in the document worker."""

from typing import Any, Iterable
from dataclasses import dataclass
import re

from ..domain.errors import DomainValidationError, MutationErrorCode
from .editorial_adapter import _page_transaction

_DELETABLE_SUBTYPES = frozenset(("FreeText", "Ink", "Line", "Highlight", "Text", "Underline",
                                 "Squiggly", "StrikeOut", "Caret"))


def _name(document: Any, xref: int, key: str) -> str | None:
    kind, value = document.xref_get_key(xref, key)
    return value[1:] if kind == "name" and value.startswith("/") else None


def _xref(document: Any, xref: int, key: str) -> int | None:
    kind, value = document.xref_get_key(xref, key)
    if kind != "xref":
        return None
    try:
        return int(value.split()[0])
    except (ValueError, IndexError):
        return None


def _parse_logical_id(logical_id: str) -> tuple[int, int]:
    try:
        prefix, page, xref = logical_id.split(":")
        if prefix != "native":
            raise ValueError
        page_index, root_xref = int(page), int(xref)
        if page_index < 0 or root_xref <= 0:
            raise ValueError
        return page_index, root_xref
    except (AttributeError, ValueError) as exc:
        raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION) from exc


def safe_components(document: Any, logical_id: str) -> tuple[int, tuple[int, ...]]:
    """Return a complete removable graph or reject without mutating it."""
    page_index, root = _parse_logical_id(logical_id)
    from .managed_corrections import collect_managed, has_marker
    if has_marker(document, root):
        group = collect_managed(document, page_index, root)
        return page_index, group.components
    if not 0 <= page_index < document.page_count:
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    page = document.load_page(page_index)
    roots = [xref for xref, _code, _name_value in page.annot_xrefs()]
    if root not in roots or _name(document, root, "Subtype") not in _DELETABLE_SUBTYPES:
        raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
    if _xref(document, root, "IRT") is not None:
        # A reply / group member is never a separately removable logical item.
        raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)

    primary = {root}
    if _name(document, root, "Subtype") == "Caret" and _name(document, root, "IT") == "Replace":
        members = [xref for xref in roots
                   if (_name(document, xref, "Subtype") == "StrikeOut"
                       and _name(document, xref, "IT") == "StrikeOutTextEdit"
                       and _name(document, xref, "RT") == "Group"
                       and _xref(document, xref, "IRT") == root)]
        if len(members) != 1:
            raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
        primary.add(members[0])

    components = set(primary)
    for xref in primary:
        popup = _xref(document, xref, "Popup")
        if popup is not None:
            if _xref(document, popup, "Parent") != xref:
                raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            components.add(popup)
    # Do not orphan third-party replies. The known Replace child is the sole
    # permitted incoming IRT relationship and is already in ``primary``.
    for xref in roots:
        parent = _xref(document, xref, "IRT")
        if parent in primary and xref not in primary:
            raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
    return page_index, tuple(sorted(components))


def delete_external_annotation(document: Any, logical_id: str) -> None:
    page_index, components = safe_components(document, logical_id)
    page = document.load_page(page_index)
    roots = [xref for xref, _code, _name_value in page.annot_xrefs()]
    with _page_transaction(document, page, components):
        remaining = "[" + " ".join(f"{xref} 0 R" for xref in roots if xref not in components) + "]"
        document.xref_set_key(page.xref, "Annots", remaining)
        for xref in components:
            document.update_object(xref, "null")
        document.reload_page(page)


@dataclass(frozen=True, slots=True)
class NativeAnnotationSnapshot:
    """Worker-local native state for just the pages/components a step touches.

    The page object is never copied or restored wholesale: that could rewrite
    unrelated annotations.  Only its `/Annots` key (and an indirect array when
    used) is captured, together with the directly owned annotation graph.
    Appearance streams are referenced, never reconstructed or mutated.
    """

    pages: tuple[tuple[int, int, str, str], ...]
    component_sources: tuple[tuple[int, str], ...]
    component_streams: tuple[tuple[int, bytes], ...]

    @property
    def byte_size(self) -> int:
        return sum(len(kind.encode("utf-8")) + len(value.encode("utf-8")) + 24
                   for _index, _xref, kind, value in self.pages) + sum(
            len(source.encode("utf-8")) + 16 for _xref, source in self.component_sources) + sum(
            len(stream) + 16 for _xref, stream in self.component_streams)


@dataclass(frozen=True, slots=True)
class ExternalDeleteSnapshot:
    logical_id: str
    native: NativeAnnotationSnapshot

    @property
    def byte_size(self) -> int:
        return self.native.byte_size


def snapshot_native_annotation(document: Any, page_indices: Iterable[int],
                               component_xrefs: Iterable[int], *,
                               include_normal_appearance: bool = False) -> NativeAnnotationSnapshot:
    pages: list[tuple[int, int, str, str]] = []
    objects: set[int] = {xref for xref in component_xrefs if type(xref) is int and xref > 0}
    for page_index in sorted(set(page_indices)):
        page = document.load_page(page_index)
        kind, value = document.xref_get_key(page.xref, "Annots")
        pages.append((page_index, page.xref, kind, value))
        if kind == "xref":
            try:
                objects.add(int(value.split()[0]))
            except (IndexError, ValueError):
                raise DomainValidationError(MutationErrorCode.INTERNAL_PDF_MUTATION) from None
    if include_normal_appearance:
        # ``Annot.update()`` may reuse and overwrite the existing normal-AP
        # Form. The root dictionary alone is therefore not an exact external
        # Callout history snapshot.
        for root in tuple(objects):
            kind, appearance = document.xref_get_key(root, "AP")
            if kind == "dict":
                match = re.search(r"/N\s+(\d+)\s+0\s+R", appearance)
                if match is not None:
                    objects.add(int(match.group(1)))
    sources = tuple((xref, document.xref_object(xref)) for xref in sorted(objects))
    streams = tuple((xref, document.xref_stream(xref)) for xref, _source in sources
                    if document.xref_is_stream(xref))
    return NativeAnnotationSnapshot(tuple(pages), sources, streams)


def restore_native_annotation(document: Any, target: NativeAnnotationSnapshot,
                              opposite: NativeAnnotationSnapshot) -> None:
    """Atomically restore only the exact native state of one history step."""
    target_objects = dict(target.component_sources)
    opposite_objects = dict(opposite.component_sources)
    target_streams = dict(target.component_streams)
    opposite_streams = dict(opposite.component_streams)

    def apply(snapshot: NativeAnnotationSnapshot, remove: dict[int, str]) -> None:
        sources = dict(snapshot.component_sources)
        for xref in remove.keys() - sources.keys():
            document.update_object(xref, "null")
        for xref, source in sources.items():
            document.update_object(xref, source)
        for xref in remove.keys() - sources.keys():
            if xref in target_streams or xref in opposite_streams:
                document.update_object(xref, "null")
        for xref, stream in snapshot.component_streams:
            # ``update_stream`` defaults to compression. That changes an
            # originally unfiltered Form into ``/FlateDecode`` and rewrites
            # its ``/Length`` even when the decoded AP bytes are unchanged.
            # Restore the snapshot dictionary first, then use its original
            # compression policy while restoring the decoded stream. PyMuPDF
            # owns any compressed byte representation, but an unfiltered AP
            # remains unfiltered and the saved Form stays structurally valid.
            source = sources[xref]
            document.update_stream(xref, stream,
                                   compress=1 if "/Filter" in source else 0)
        for _index, page_xref, _kind, value in snapshot.pages:
            document.xref_set_key(page_xref, "Annots", value)
        for page_index, _page_xref, _kind, _value in snapshot.pages:
            # A snapshot restore has no retained Page instance. On 1.28.2 a
            # freshly loaded page can already be the current instance, for
            # which reload_page asserts instead of refreshing. A fresh load
            # is sufficient: all subsequent users obtain their own Page.
            page = document.load_page(page_index)
            try:
                document.reload_page(page)
            except AssertionError:
                document.load_page(page_index)

    try:
        apply(target, opposite_objects)
    except Exception:
        try:
            apply(opposite, target_objects)
        except Exception as rollback_error:
            document.close()
            raise RuntimeError("Native history restore rollback failed; document closed") from rollback_error
        raise


def snapshot_external_annotation(document: Any, logical_id: str) -> ExternalDeleteSnapshot:
    page_index, components = safe_components(document, logical_id)
    return ExternalDeleteSnapshot(logical_id,
                                  snapshot_native_annotation(document, (page_index,), components))


def restore_external_annotation(document: Any, snapshot: ExternalDeleteSnapshot) -> None:
    """Restore a deleted graph atomically before moving the history cursor."""
    # Capture the current deleted state as the rollback target.  This remains
    # scoped to the same annotation page and never rewrites an unrelated page.
    pages = tuple(index for index, _xref, _kind, _value in snapshot.native.pages)
    current = snapshot_native_annotation(document, pages, ())
    restore_native_annotation(document, snapshot.native, current)
