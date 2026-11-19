"""Native annotation inventory, executed exclusively by the document worker.

The inventory keeps source annotations immutable.  It scans metadata into small
summaries and serves complete text only in byte-bounded detail chunks.
"""

from dataclasses import asdict, replace
import re
from typing import Any

from ..domain.annotation_inventory import (AnnotationCapabilities, AnnotationDetail, AnnotationReply,
                                           AnnotationGeometry, AnnotationSummary)
from ..domain.errors import DomainValidationError
from ..domain.geometry import PdfPoint, PdfQuad, PdfRect
from .external_delete import safe_components

SUMMARY_PAGE_SIZE = 96
SUMMARY_PREVIEW_CHARS = 240
DETAIL_CHUNK_BYTES = 48 * 1024


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


def _points(vertices: Any) -> tuple[PdfPoint, ...]:
    try:
        def coordinate(point: Any, index: int, name: str) -> float:
            value = getattr(point, name, None)
            if value is None:
                value = point[index]
            return float(value)
        return tuple(PdfPoint(coordinate(point, 0, "x"), coordinate(point, 1, "y"))
                     for point in vertices or ())
    except (AttributeError, IndexError, TypeError, ValueError):
        return ()


def _rect(value: Any) -> PdfRect:
    x0, y0, x1, y1 = (float(value.x0), float(value.y0),
                      float(value.x1), float(value.y1))
    return PdfRect(x0, y0, x1, y1)


def _preview(value: str) -> str:
    # This is presentation-only. The detail endpoint preserves every code point.
    return value if len(value) <= SUMMARY_PREVIEW_CHARS else value[:SUMMARY_PREVIEW_CHARS] + "…"


def _chunk(value: str, offset: int) -> tuple[str, int, bool]:
    if type(offset) is not int or offset < 0 or offset > len(value):
        raise ValueError("Invalid detail offset")
    end = offset
    used = 0
    while end < len(value):
        size = len(value[end].encode("utf-8"))
        if used and used + size > DETAIL_CHUNK_BYTES:
            break
        # A malformed-but-valid PDF can contain a character which encodes above
        # the target; returning that one character still makes forward progress.
        used += size
        end += 1
    return value[offset:end], end, end == len(value)


class NativeAnnotationInventory:
    """Per-document cache of lightweight logical native annotations."""

    def __init__(self, document: Any) -> None:
        self._document = document
        self._summaries: tuple[AnnotationSummary, ...] | None = None
        self._details: dict[str, AnnotationDetail] = {}

    def summaries(self, cursor: int) -> dict[str, object]:
        if self._summaries is None:
            self._scan()
        assert self._summaries is not None
        if type(cursor) is not int or cursor < 0 or cursor > len(self._summaries):
            raise ValueError("Invalid inventory cursor")
        entries = self._summaries[cursor:cursor + SUMMARY_PAGE_SIZE]
        next_cursor = cursor + len(entries)
        return {"summaries": [summary_to_payload(entry) for entry in entries],
                "next_cursor": next_cursor, "complete": next_cursor == len(self._summaries)}

    def detail(self, logical_id: str, offset: int) -> dict[str, object]:
        # GUI summaries can outlive a worker inventory invalidated by a mutation.
        if self._summaries is None:
            self._scan()
        detail = self._details.get(logical_id)
        if detail is None:
            raise ValueError("Unknown annotation")
        contents, next_offset, complete = _chunk(detail.contents, offset)
        edit_payload = {}
        if complete:
            from .external_profiles import inspect_profile
            from .external_delete import _parse_logical_id
            from ..domain.serialization import rich_text_to_payload, style_to_payload
            page_index, root = _parse_logical_id(logical_id)
            page = self._document.load_page(page_index)
            profile = inspect_profile(self._document, page, page.load_annot(root))
            if profile.capabilities().editable_text and profile.rich_text is not None:
                edit_payload = {"edit_rich_text": rich_text_to_payload(profile.rich_text),
                                "edit_style": style_to_payload(profile.style)}
        return {"logical_id": logical_id, "contents": contents, "next_offset": next_offset,
                # RC is retained worker-side for future rich inspection, but
                # never piggybacks as an unbounded control-frame field.
                "complete": complete, "rich_contents": None,
                "subject": _preview(detail.subject or "") if offset == 0 else None,
                **edit_payload}

    def summary_for_logical_id(self, logical_id: str) -> AnnotationSummary:
        """Read one known-safe logical graph without rebuilding the inventory."""
        page_index, components = safe_components(self._document, logical_id)
        root = int(logical_id.rsplit(":", 1)[1])
        page = self._document.load_page(page_index)
        annot = page.load_annot(root)
        if annot is None:
            raise ValueError("Unreadable annotation")
        subtype = _name(self._document, root, "Subtype") or "Unknown"
        members = [root]
        if subtype == "Caret" and _name(self._document, root, "IT") == "Replace":
            members.extend(xref for xref in components
                           if xref != root and _xref(self._document, xref, "IRT") == root
                           and _name(self._document, xref, "RT") == "Group")
        return self._record(page_index, page, annot, subtype, members)

    def group_summaries(self, logical_id: str) -> tuple[AnnotationSummary, ...]:
        """Read only a verified group's rows, retaining native identities."""
        from .managed_corrections import collect_managed
        from .external_delete import _parse_logical_id
        page_index, root = _parse_logical_id(logical_id)
        group = collect_managed(self._document, page_index, root)
        records = [self.summary_for_logical_id(logical_id)]
        for xref in group.replies:
            record = self.summary_for_edit(f"native:{page_index}:{xref}")
            records.append(replace(record, reply=AnnotationReply(
                root, f"{root} 0 R", self._document.xref_get_key(xref, "RT")[1]
                if self._document.xref_get_key(xref, "RT")[0] != "null" else None,
                logical_id, "linked")))
        # Manifest creation order is stable for the app-generated group.
        records[0] = replace(records[0], reply_ids=tuple(record.logical_id for record in records[1:]))
        return tuple(records)

    def prepare_delta(self, delta) -> tuple["NativeAnnotationInventory", bool]:
        """Stage cache adoption so a failed finalization leaves the old cache intact."""
        candidate = NativeAnnotationInventory(self._document)
        candidate._summaries = self._summaries
        candidate._details = self._details.copy()
        return candidate, candidate.apply_delta(delta)

    def summary_for_edit(self, logical_id: str) -> AnnotationSummary:
        from .external_delete import _parse_logical_id
        page_index, root = _parse_logical_id(logical_id)
        page = self._document.load_page(page_index)
        annot = page.load_annot(root)
        if annot is None:
            raise ValueError("Unreadable annotation")
        return self._record(page_index, page, annot,
                            _name(self._document, root, "Subtype") or "Unknown", [root])

    def apply_delta(self, delta) -> bool:
        """Patch an already populated cache after one external edit/history step."""
        if self._summaries is None:
            return False
        removed = set(delta.inventory_removed_ids)
        if any((item.reply is not None and item.reply.parent_logical_id in removed
                and item.logical_id not in removed)
               or (item.logical_id in removed and set(item.reply_ids) - removed) for item in self._summaries):
            # Only removing related objects can alter topology in the current
            # command domain. Move/resize/text edits never change IRT or RT.
            self._summaries = None
            self._details.clear()
            return True
        records = {item.logical_id: item for item in self._summaries}
        for logical_id in delta.inventory_removed_ids:
            records.pop(logical_id, None)
            self._details.pop(logical_id, None)
        for summary in delta.inventory_added + delta.inventory_updated:
            # Re-read only the changed annotation's complete detail; summaries
            # intentionally carry bounded previews, not unbounded comment text.
            self.summary_for_edit(summary.logical_id)
            previous = records.get(summary.logical_id)
            if previous is not None:
                summary = replace(summary, reply=summary.reply or previous.reply,
                                  reply_ids=summary.reply_ids or previous.reply_ids)
            records[summary.logical_id] = summary
        self._summaries = tuple(sorted(records.values(), key=lambda item:
            (item.page_index, item.geometry.bbox.y0, item.geometry.bbox.x0, item.logical_id)))
        return False

    def _scan(self) -> None:
        # ``Annot`` objects are page-bound and become orphaned when a later
        # page is loaded. Keep only stable xrefs during the document-wide pass;
        # reload each annotation on its owning page immediately before reading
        # its API properties.
        physical: dict[int, int] = {}
        for page_index in range(self._document.page_count):
            page = self._document.load_page(page_index)
            for xref, _type_code, _name_value in page.annot_xrefs():
                physical[xref] = page_index

        self._details.clear()
        grouped_children: dict[int, list[int]] = {}
        for xref in physical:
            parent = _xref(self._document, xref, "IRT")
            if (parent in physical and physical[parent] == physical[xref]
                    and _name(self._document, xref, "Subtype") == "StrikeOut"
                    and _name(self._document, parent, "Subtype") == "Caret"
                    and _xref(self._document, parent, "IRT") is None
                    and _name(self._document, xref, "IT") == "StrikeOutTextEdit"
                    and _name(self._document, xref, "RT") == "Group"
                    and _name(self._document, parent, "IT") == "Replace"):
                grouped_children.setdefault(parent, []).append(xref)

        records: list[AnnotationSummary] = []
        group_roots = {child: root for root, children in grouped_children.items() for child in children}
        for xref, page_index in physical.items():
            subtype = _name(self._document, xref, "Subtype") or "Unknown"
            if subtype == "Popup" or xref in group_roots:
                continue
            page = self._document.load_page(page_index)
            annot = page.load_annot(xref)
            if annot is None:
                # A malformed /Annots reference still remains a valid PDF
                # complexity concern; omit only this unreadable physical item,
                # never invalidate the complete inventory.
                continue
            members = [xref, *grouped_children.get(xref, ())]
            records.append(self._record(page_index, page, annot, subtype, members))
        from .annotation_replies import associate_replies
        records = associate_replies(self._document, records, physical, group_roots)
        records.sort(key=lambda item: (item.page_index, item.geometry.bbox.y0,
                                       item.geometry.bbox.x0, item.logical_id))
        self._summaries = tuple(records)

    def _record(self, page_index: int, page: Any, annot: Any, subtype: str,
                members: list[int]) -> AnnotationSummary:
        xref = annot.xref
        info = annot.info or {}
        contents = str(info.get("content") or "")
        rich = self._pdf_string(xref, "RC")
        intent = _name(self._document, xref, "IT")
        kind = {
            "Ink": "ink", "Line": "line", "Highlight": "highlight", "FreeText": "text_box",
            "StrikeOut": "delete_text", "Caret": "insert_text", "Text": "sticky_note",
            "Underline": "underline", "Squiggly": "squiggly",
        }.get(subtype, subtype.lower() or "unknown")
        # MuPDF may emit an unused /CL array on an ordinary FreeText.  Intent
        # is the unambiguous native callout discriminator used by Acrobat and
        # by the app's own M9 writer.
        if subtype == "FreeText" and intent == "FreeTextCallout":
            kind = "callout"
        if intent == "Replace" and len(members) > 1:
            kind = "replace_text"
        elif subtype == "StrikeOut" and intent != "StrikeOutTextEdit":
            # Acrobat's observed Delete Text primitive is an otherwise plain
            # StrikeOut with no payload. A payload / non-editorial intent stays
            # explicitly generic, because it cannot be proven lossless.
            kind = "delete_text" if not contents and intent is None else "strikeout"

        raw_vertices = getattr(annot, "vertices", ())
        vertices = _points(raw_vertices)
        quads: tuple[PdfQuad, ...] = ()
        paths: tuple[tuple[PdfPoint, ...], ...] = ()
        if subtype in ("Highlight", "Underline", "Squiggly", "StrikeOut") and len(vertices) >= 4:
            quads = tuple(PdfQuad(*vertices[index:index + 4])
                          for index in range(0, len(vertices) - 3, 4))
        elif subtype == "Ink":
            # Ink.vertices is a list of strokes, while markup.vertices is a
            # flat point sequence.
            try:
                paths = tuple(path for path in (_points(stroke) for stroke in raw_vertices or ()) if path)
            except TypeError:
                paths = ()
        if kind == "replace_text" and len(members) > 1:
            # The logical root is the Caret; the actual replaced range belongs
            # to its grouped StrikeOut component.
            member = page.load_annot(members[1])
            member_vertices = _points(getattr(member, "vertices", ())) if member else ()
            if len(member_vertices) >= 4:
                quads = tuple(PdfQuad(*member_vertices[index:index + 4])
                              for index in range(0, len(member_vertices) - 3, 4))
        callout = vertices if kind == "callout" and vertices else self._callout_points(xref)
        if subtype == "Line":
            # A Line is not a FreeText callout, even with an opaque /CL key.
            callout = ()
        rect = _rect(annot.rect)
        caret = None
        if kind in ("insert_text", "replace_text"):
            center = (rect.x0 + rect.x1) / 2
            caret = (PdfPoint(center, rect.y0), PdfPoint(center, rect.y1))
        if quads:
            points = tuple(point for quad in quads for point in quad.points)
            rect = PdfRect(min(rect.x0, *(point.x for point in points)),
                           min(rect.y0, *(point.y for point in points)),
                           max(rect.x1, *(point.x for point in points)),
                           max(rect.y1, *(point.y for point in points)))
        text_rect = self._freetext_text_rect(annot, rect) if subtype == "FreeText" else None
        geometry = AnnotationGeometry(rect, quads, paths, callout, caret, text_rect,
                                      vertices if subtype == "Line" and len(vertices) == 2 else ())
        colors = getattr(annot, "colors", {}) or {}
        raw_opacity = float(getattr(annot, "opacity", -1.0))
        # PyMuPDF intentionally reports -1 when /CA is absent, while PDF's
        # visual default is fully opaque. Treating it as 0 made normal external
        # FreeText and Ink invisible until the selection outline was painted.
        opacity = 1.0 if raw_opacity < 0.0 else min(1.0, raw_opacity)
        native_stroke = list(colors.get("stroke") or ())
        native_fill = list(colors.get("fill") or ())
        da_color = self._da_color(xref)
        if subtype == "FreeText":
            # Acrobat Callout files commonly use /C for the fill and /DA's
            # colour for the border / leader, while PyMuPDF exposes /C as a
            # generic "stroke". Preserve both interpretations for fallback.
            native_fill = native_fill or native_stroke
            native_stroke = da_color or native_stroke
        style = {"stroke": native_stroke,
                 "fill": native_fill, "text": da_color or [],
                 "opacity": opacity,
                 "border": dict(getattr(annot, "border", {}) or {}), "intent": intent}
        if subtype == "Line":
            style["line_ends"] = list(annot.line_ends or ())
        logical_id = f"native:{page_index}:{xref}"
        components = tuple(sorted(set(members + self._popups(members))))
        detail = AnnotationDetail(logical_id, contents, rich, str(info.get("subject") or "") or None)
        self._details[logical_id] = detail
        from .managed_corrections import collect_managed, has_marker
        marked = has_marker(self._document, xref)
        managed = None
        try:
            _safe_page, safe = safe_components(self._document, logical_id)
            if marked:
                managed = collect_managed(self._document, page_index, xref)
                components = managed.components
            deletable = _safe_page == page_index and set(safe) == set(components)
        except DomainValidationError:
            deletable = False
        if marked:
            style["external_profile"] = "fuPDF-correction-v1" if managed is not None else "unverified-fuPDF-correction"
            capabilities = AnnotationCapabilities(deletable=deletable)
        else:
            from .external_profiles import inspect_profile
            # Graph validation may have visited other pages. Obtain a fresh
            # page-bound annotation rather than retaining an orphaned wrapper.
            page = self._document.load_page(page_index)
            annot = page.load_annot(xref)
            profile = inspect_profile(self._document, page, annot)
            style["external_profile"] = profile.name
            style["external_edit_reason"] = profile.reason
            capabilities = profile.capabilities(deletable=deletable)
        from dataclasses import replace
        capabilities = replace(capabilities, logical_group=managed is not None or len(members) > 1)
        return AnnotationSummary(
            logical_id, page_index, kind, subtype, geometry, _preview(contents),
            str(info.get("title") or "") or None, str(info.get("modDate") or "") or None,
            str(info.get("creationDate") or "") or None, style,
            capabilities, components,
            self._has_key(xref, "AP"), "external", str(info.get("id") or "") or None)

    def _popups(self, xrefs: list[int]) -> list[int]:
        return [popup for xref in xrefs if (popup := _xref(self._document, xref, "Popup")) is not None]

    def _callout_points(self, xref: int) -> tuple[PdfPoint, ...]:
        kind, value = self._document.xref_get_key(xref, "CL")
        if kind != "array":
            return ()
        try:
            numbers = [float(part) for part in value.strip("[]").split()]
            return tuple(PdfPoint(numbers[index], numbers[index + 1])
                         for index in range(0, len(numbers) - 1, 2))
        except ValueError:
            return ()

    @staticmethod
    def _freetext_text_rect(annot: Any, rect: PdfRect) -> PdfRect:
        """Map PDF /RD (left, top, right, bottom) into canonical coordinates."""
        try:
            left, top, neg_right, neg_bottom = (float(value) for value in annot.rect_delta)
            result = PdfRect(rect.x0 + left, rect.y0 - neg_bottom,
                             rect.x1 + neg_right, rect.y1 - top)
            return result
        except (TypeError, ValueError):
            return rect

    def _has_key(self, xref: int, key: str) -> bool:
        return self._document.xref_get_key(xref, key)[0] != "null"

    def _pdf_string(self, xref: int, key: str) -> str | None:
        kind, value = self._document.xref_get_key(xref, key)
        # ``info`` is the authoritative decoded Contents. RC is retained as a
        # diagnostic/read-only rich payload without trying to interpret markup.
        return value if kind == "string" else None

    def _da_color(self, xref: int) -> list[float] | None:
        value = self._pdf_string(xref, "DA")
        if not value:
            return None
        match = re.search(r"(?<!\S)([+-]?(?:\d+\.?\d*|\.\d+))\s+"
                          r"([+-]?(?:\d+\.?\d*|\.\d+))\s+"
                          r"([+-]?(?:\d+\.?\d*|\.\d+))\s+rg\b", value)
        if match is None:
            return None
        try:
            color = [float(component) for component in match.groups()]
            return color if all(0.0 <= component <= 1.0 for component in color) else None
        except ValueError:
            return None


def _point_payload(point: PdfPoint) -> list[float]:
    return [point.x, point.y]


def summary_to_payload(summary: AnnotationSummary) -> dict[str, object]:
    geometry = summary.geometry
    return {"logical_id": summary.logical_id, "page_index": summary.page_index,
            "kind": summary.kind, "subtype": summary.subtype,
            "geometry": {"bbox": [geometry.bbox.x0, geometry.bbox.y0,
                                     geometry.bbox.x1, geometry.bbox.y1],
                         "quads": [[_point_payload(point) for point in
                                    (quad.upper_left, quad.upper_right,
                                     quad.lower_left, quad.lower_right)] for quad in geometry.quads],
                         "paths": [[_point_payload(point) for point in path] for path in geometry.paths],
                         "callout_line": [_point_payload(point) for point in geometry.callout_line],
                         "caret": ([_point_payload(point) for point in geometry.caret]
                                   if geometry.caret is not None else None),
                         "text_rect": ([geometry.text_rect.x0, geometry.text_rect.y0,
                                        geometry.text_rect.x1, geometry.text_rect.y1]
                                       if geometry.text_rect is not None else None),
                         "line": [_point_payload(point) for point in geometry.line]},
            "preview": summary.preview, "author": summary.author, "modified": summary.modified,
            "created": summary.created, "style": summary.style,
            "capabilities": asdict(summary.capabilities),
            "component_xrefs": list(summary.component_xrefs),
            "appearance_available": summary.appearance_available,
            "provenance": summary.provenance, "native_name": summary.native_name,
            "reply": asdict(summary.reply) if summary.reply is not None else None,
            "reply_ids": list(summary.reply_ids)}


def summary_from_payload(payload: dict[str, object]) -> AnnotationSummary:
    geometry_data = payload["geometry"]
    if not isinstance(geometry_data, dict):
        raise ValueError("Invalid inventory geometry")
    bbox = geometry_data["bbox"]
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("Invalid inventory bounds")
    def point(value: object) -> PdfPoint:
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("Invalid inventory point")
        return PdfPoint(float(value[0]), float(value[1]))
    quads = tuple(PdfQuad(*(point(value) for value in quad)) for quad in geometry_data.get("quads", []))
    paths = tuple(tuple(point(value) for value in path) for path in geometry_data.get("paths", []))
    caret_data = geometry_data.get("caret")
    caret = (tuple(point(value) for value in caret_data) if isinstance(caret_data, list) else None)
    if caret is not None and len(caret) != 2:
        raise ValueError("Invalid inventory caret")
    text_rect_data = geometry_data.get("text_rect")
    text_rect = (PdfRect(*(float(value) for value in text_rect_data))
                 if isinstance(text_rect_data, list) and len(text_rect_data) == 4 else None)
    geometry = AnnotationGeometry(PdfRect(*(float(value) for value in bbox)), quads, paths,
                                  tuple(point(value) for value in geometry_data.get("callout_line", [])),
                                  caret, text_rect,
                                  tuple(point(value) for value in geometry_data.get("line", [])))
    capabilities = payload["capabilities"]
    if not isinstance(capabilities, dict):
        raise ValueError("Invalid inventory capabilities")
    return AnnotationSummary(str(payload["logical_id"]), int(payload["page_index"]), str(payload["kind"]),
                             str(payload["subtype"]), geometry, str(payload["preview"]),
                             payload.get("author") if isinstance(payload.get("author"), str) else None,
                             payload.get("modified") if isinstance(payload.get("modified"), str) else None,
                             payload.get("created") if isinstance(payload.get("created"), str) else None,
                             dict(payload.get("style") or {}), AnnotationCapabilities(**capabilities),
                             tuple(int(value) for value in payload.get("component_xrefs", [])),
                             bool(payload.get("appearance_available")), str(payload.get("provenance", "external")),
                             payload.get("native_name"),
                             AnnotationReply(**payload["reply"]) if payload.get("reply") is not None else None,
                             tuple(payload.get("reply_ids", ())))
