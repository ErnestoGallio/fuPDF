"""In-place external edits; PDF dictionary writes stay behind A-06.

Imported appearances remain untouched for conservative operations. The first
text-layout operation creates the app's native two-role appearance; history
retains the unmodified imported root for exact Undo.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any
from xml.etree import ElementTree as ET

from ..domain.errors import DomainValidationError, MutationErrorCode
from ..domain.external_edits import EditExternalAnnotation, ExternalEditKind, CAPABILITY_FOR_EDIT
from ..domain.callout_geometry import callout_line, move_callout_box
from ..domain.geometry import PdfPoint, PdfRect
from .external_profiles import inspect_profile, native_border_style, numbers, value
from .freetext_adapter import _rich_markup, _default_style, _pdf_font, _pdf_rgb
from .canonical_text_ap import patch_native_freetext_appearance
from .external_delete import restore_native_annotation, snapshot_native_annotation
from .save_validation import _parse


# Exact root-dictionary mutation surface; everything else is preserved verbatim.
MUTATION_KEYS = {
    "translation": frozenset(("Rect", "M")),
    "ink": frozenset(("Rect", "InkList", "M")),
    "line": frozenset(("Rect", "L", "M")),
    "box": frozenset(("Rect", "RD", "AP", "M")),
    "callout": frozenset(("Rect", "RD", "CL", "AP", "M")),
    "text": frozenset(("Contents", "RC", "AP", "M")),
    "style": frozenset(("C", "DA", "DS", "BS", "CA", "AP", "M")),
}


def array(values) -> str:
    return "[" + " ".join(f"{v:.8f}" for v in values) + "]"


_NUMBER = rb"[+-]?(?:\d+\.?\d*|\.\d+)"
_FONT_TOKEN = re.compile(rb"/([A-Za-z0-9_.+-]+)\s+" + _NUMBER + rb"\s+Tf|<([0-9A-Fa-f\s]+)>")
_NORMAL_AP = re.compile(r"/N\s+(\d+)\s+0\s+R")
_FONT_RESOURCE = re.compile(r"/([A-Za-z0-9_.+-]+)\s+(\d+)\s+0\s+R")
_XHTML = "http://www.w3.org/1999/xhtml"
_XFA = "http://www.xfa.org/schema/xfa-data/1.0/"


def _normal_appearance_xref(document: Any, root: int) -> int:
    """Return the one normal appearance form that can be safely copied.

    Imported FreeText AP forms own their font resources.  Never modify that
    form in place: an external producer may share it with another annotation.
    """
    kind, appearance = document.xref_get_key(root, "AP")
    if kind != "dict":
        raise ValueError("Missing normal appearance")
    match = _NORMAL_AP.search(appearance)
    if match is None:
        raise ValueError("Unsupported normal appearance")
    return int(match.group(1))


def _copy_normal_appearance(document: Any, root: int) -> int:
    source = _normal_appearance_xref(document, root)
    replacement = document.get_new_xref()
    document.update_object(replacement, document.xref_object(source))
    document.update_stream(replacement, document.xref_stream(source))
    document.xref_set_key(root, "AP", f"<</N {replacement} 0 R>>")
    return replacement


def _unicode_map(document: Any, font_xref: int) -> dict[str, str]:
    """Read the font's actual ToUnicode CMap; no font-name guessing.

    The inverse is only used for glyphs already declared by the imported font.
    An absent mapping deliberately refuses AP text surgery instead of silently
    writing a Helvetica encoding under an imported resource name.
    """
    object_text = document.xref_object(font_xref)
    match = re.search(r"/ToUnicode\s+(\d+)\s+0\s+R", object_text)
    if match is None:
        return {}
    try:
        cmap = document.xref_stream(int(match.group(1))).decode("ascii")
    except (UnicodeDecodeError, RuntimeError, ValueError):
        return {}

    result: dict[str, str] = {}

    def decode(value: str) -> str | None:
        try:
            return bytes.fromhex(value).decode("utf-16-be")
        except (UnicodeDecodeError, ValueError):
            return None

    for block in re.findall(r"\d+\s+beginbfchar\s*(.*?)\s*endbfchar", cmap, re.S):
        for source, target in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            character = decode(target)
            if character is not None:
                result[source.upper()] = character
    for block in re.findall(r"\d+\s+beginbfrange\s*(.*?)\s*endbfrange", cmap, re.S):
        for start, end, target in re.findall(
                r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            first, last = int(start, 16), int(end, 16)
            character = decode(target)
            if character is None or len(character) != 1:
                continue
            for offset, code in enumerate(range(first, last + 1)):
                result[f"{code:0{len(start)}X}"] = chr(ord(character) + offset)
    return result


def _decode_codes(value: str, cmap: dict[str, str]) -> str | None:
    lengths = sorted({len(code) for code in cmap}, reverse=True)
    decoded: list[str] = []
    position = 0
    while position < len(value):
        code = next((value[position:position + size].upper() for size in lengths
                     if value[position:position + size].upper() in cmap), None)
        if code is None:
            return None
        decoded.append(cmap[code])
        position += len(code)
    return "".join(decoded)


def _encode_text(text: str, cmap: dict[str, str]) -> str | None:
    reverse: dict[str, str] = {}
    for code, character in cmap.items():
        # Use the first declared code deterministically.  It is the imported
        # font's own CMap, not a guessed PostScript-name mapping.
        reverse.setdefault(character, code)
    try:
        return "".join(reverse[character] for character in text)
    except KeyError:
        return None


@dataclass(frozen=True)
class _AppearanceTextFragment:
    start: int
    end: int
    text: str
    font: str
    cmap: dict[str, str]


def _appearance_fragments(document: Any, appearance_xref: int) -> tuple[bytes, list[_AppearanceTextFragment]]:
    stream = document.xref_stream(appearance_xref)
    form = document.xref_object(appearance_xref)
    font_xrefs = {name: int(xref) for name, xref in _FONT_RESOURCE.findall(form)}
    fragments: list[_AppearanceTextFragment] = []
    for text_block in re.finditer(rb"\bBT\b(.*?)\bET\b", stream, re.S):
        active_font: str | None = None
        for token in _FONT_TOKEN.finditer(text_block.group(1)):
            font, encoded = token.groups()
            if font is not None:
                active_font = font.decode("ascii")
                continue
            if encoded is None or active_font is None or active_font not in font_xrefs:
                continue
            value = re.sub(rb"\s+", b"", encoded).decode("ascii")
            cmap = _unicode_map(document, font_xrefs[active_font])
            decoded = _decode_codes(value, cmap)
            if decoded is None:
                continue
            start = text_block.start(1) + token.start(2)
            fragments.append(_AppearanceTextFragment(start, text_block.start(1) + token.end(2),
                                                      decoded, active_font, cmap))
    return stream, fragments


def _rewrite_rc_text(document: Any, root: int, old_text: str, new_text: str) -> str | None:
    """Replace text nodes while retaining the imported XHTML/CSS font runs."""
    kind, markup = document.xref_get_key(root, "RC")
    if kind != "string":
        return None
    try:
        body = ET.fromstring(markup)
    except ET.ParseError:
        return None
    nodes: list[tuple[Any, str]] = []
    for node in body.iter():
        if node.text:
            nodes.append((node, "text"))
        for child in node:
            if child.tail:
                nodes.append((child, "tail"))
    values = [getattr(node, field).replace("\r", "\n") for node, field in nodes]
    if "".join(values) != old_text:
        return None
    prefix = 0
    while prefix < min(len(old_text), len(new_text)) and old_text[prefix] == new_text[prefix]:
        prefix += 1
    old_end, new_end = len(old_text), len(new_text)
    while old_end > prefix and new_end > prefix and old_text[old_end - 1] == new_text[new_end - 1]:
        old_end -= 1
        new_end -= 1
    position = 0
    inserted = False
    for (node, field), text in zip(nodes, values, strict=True):
        end = position + len(text)
        if position < old_end and end > prefix:
            before = text[:max(0, prefix - position)]
            after = text[max(0, old_end - position):]
            replacement = before + (new_text[prefix:new_end] if not inserted else "") + after
            setattr(node, field, replacement.replace("\n", "\r"))
            inserted = True
        position = end
    if not inserted:
        return None
    ET.register_namespace("", _XHTML)
    ET.register_namespace("xfa", _XFA)
    return '<?xml version="1.0"?>' + ET.tostring(body, encoding="unicode")


def _patch_text_appearance(document: Any, root: int, old_text: str, new_text: str) -> bool:
    """Patch only changed glyph strings, retaining every original font resource."""
    appearance = _copy_normal_appearance(document, root)
    stream, fragments = _appearance_fragments(document, appearance)
    if not fragments or "".join(item.text for item in fragments) != old_text:
        return False
    prefix = 0
    while prefix < min(len(old_text), len(new_text)) and old_text[prefix] == new_text[prefix]:
        prefix += 1
    old_end, new_end = len(old_text), len(new_text)
    while old_end > prefix and new_end > prefix and old_text[old_end - 1] == new_text[new_end - 1]:
        old_end -= 1
        new_end -= 1
    position = 0
    replacements: list[tuple[int, int, bytes]] = []
    inserted = False
    for fragment in fragments:
        end = position + len(fragment.text)
        if position < old_end and end > prefix:
            before = fragment.text[:max(0, prefix - position)]
            after = fragment.text[max(0, old_end - position):]
            replacement = before + (new_text[prefix:new_end] if not inserted else "") + after
            encoded = _encode_text(replacement, fragment.cmap)
            if encoded is None:
                return False
            replacements.append((fragment.start, fragment.end, encoded.encode("ascii")))
            inserted = True
        position = end
    if not inserted:
        return False
    for start, end, replacement in reversed(replacements):
        stream = stream[:start] + replacement + stream[end:]
    document.update_stream(appearance, stream)
    return True


def _patch_style_appearance(document: Any, root: int, style) -> None:
    """Keep imported text/font commands and change only pre-text box paint."""
    appearance = _copy_normal_appearance(document, root)
    stream = document.xref_stream(appearance)
    before, marker, after = stream.partition(b"BT")
    if not marker:
        return
    rgb = lambda color: (f"{color.red:g} {color.green:g} {color.blue:g}").encode("ascii")
    if style.fill is not None:
        before, changed = re.subn(rb"(?:" + _NUMBER + rb"\s+){2}" + _NUMBER + rb"\s+rg",
                                  rgb(style.fill) + b" rg", before, count=1)
    before, changed = re.subn(rb"(?:" + _NUMBER + rb"\s+){2}" + _NUMBER + rb"\s+RG",
                              rgb(style.border) + b" RG", before, count=1)
    before, changed = re.subn(_NUMBER + rb"\s+w", f"{style.border_width:g} w".encode("ascii"),
                              before, count=1)
    document.update_stream(appearance, before + marker + after)


def _native_da(original: str, style) -> str:
    """Update color while preserving the imported DA font resource token."""
    color = f"{style.border.red:g} {style.border.green:g} {style.border.blue:g}"
    updated, count = re.subn(rf"(?:{_NUMBER.decode()}\s+){{2}}{_NUMBER.decode()}\s+rg",
                             color + " rg", original, count=1)
    return updated if count else original


def _native_ds(original: str, style) -> str:
    color = "#%02x%02x%02x" % tuple(round(value * 255) for value in
                                     (style.border.red, style.border.green, style.border.blue))
    updated, count = re.subn(r"color\s*:\s*#[0-9a-fA-F]{6}", "color:" + color, original, count=1)
    return updated if count else original


@dataclass(frozen=True)
class NativeCoordinates:
    sx: float
    sy: float
    dx: float
    dy: float

    @classmethod
    def observe(cls, document, annot):
        # Same measured native/public transform as the verified M10 adapter;
        # avoids applying page rotation/CropBox twice in the pinned API.
        raw = numbers(document, annot.xref, "Rect")
        rect = annot.rect
        sx, sy = (raw[2] - raw[0]) / rect.width, (raw[3] - raw[1]) / rect.height
        return cls(sx, sy, raw[0] - rect.x0 * sx, raw[3] + rect.y0 * sy)

    def point(self, x, y):
        return PdfPoint((x - self.dx) / self.sx, (self.dy - y) / self.sy)

    def native_point(self, point):
        return point.x * self.sx + self.dx, self.dy - point.y * self.sy

    def native_rect(self, rect):
        return (rect.x0 * self.sx + self.dx, self.dy - rect.y1 * self.sy,
                rect.x1 * self.sx + self.dx, self.dy - rect.y0 * self.sy)


def text_box(document, annot, transform):
    rect = annot.rect
    rd = numbers(document, annot.xref, "RD") if value(document, annot.xref, "RD") != "null" else (0, 0, 0, 0)
    return PdfRect(rect.x0 + rd[0] / transform.sx, rect.y0 + rd[3] / transform.sy,
                   rect.x1 - rd[2] / transform.sx, rect.y1 - rd[1] / transform.sy)


def _cl(document, xref, transform):
    cl = numbers(document, xref, "CL")
    return tuple(transform.point(cl[i], cl[i+1]) for i in range(0, len(cl), 2))


def _union(rect, line):
    return PdfRect(min(rect.x0, *(p.x for p in line)), min(rect.y0, *(p.y for p in line)),
                   max(rect.x1, *(p.x for p in line)), max(rect.y1, *(p.y for p in line)))


def _refresh_page(document: Any, page: Any, page_index: int) -> Any:
    """Refresh when PyMuPDF permits it, without turning a rollback into close.

    The pinned binding asserts when a page is already the latest instance. In
    that case a fresh ``load_page`` gives subsequent operations a valid handle;
    the restored root object is already authoritative.
    """
    try:
        return document.reload_page(page)
    except (AssertionError, AttributeError):
        return document.load_page(page_index)


def _style_trace(style: Any) -> dict[str, Any]:
    return {"border": (style.border.red, style.border.green, style.border.blue),
            "fill": None if style.fill is None else (style.fill.red, style.fill.green, style.fill.blue),
            "text": (style.text.red, style.text.green, style.text.blue),
            "opacity": style.opacity, "border_width": style.border_width,
            "font_family": style.font_family}


def _normalization_snapshot(document: Any, page_index: int, root: int, stage: str,
                            style: Any) -> dict[str, Any]:
    """Structured worker-only diagnostic; never exposes annotation text."""
    page = document.load_page(page_index)
    annot = page.load_annot(root)
    keys = {key: document.xref_get_key(root, key) for key in ("C", "DA", "DS", "BS", "Border", "CA")}
    ap_kind, ap_value = document.xref_get_key(root, "AP")
    match = _NORMAL_AP.search(ap_value) if ap_kind == "dict" else None
    parent = {"exists": match is not None}
    if match is not None:
        stream = document.xref_stream(int(match.group(1))).decode("ascii")
        # The diagnostic normalizes only the one text slot so the remaining
        # digest is a direct proof that the native callout leader, arrow,
        # border, fill, clip and parent geometry were not reconstructed.
        native_geometry = re.sub(r"BT.*?ET", "<TEXT_SLOT>", stream, flags=re.DOTALL)
        native_geometry = native_geometry.replace("q\n/CanonicalText Do\nQ", "<TEXT_SLOT>")
        parent.update(
            bbox=document.xref_get_key(int(match.group(1)), "BBox")[1],
            matrix=document.xref_get_key(int(match.group(1)), "Matrix")[1],
            geometry_stream_sha256=sha256(native_geometry.encode("ascii")).hexdigest(),
            stroke=bool(re.search(r"(?m)^\s*(?:B|B\*|S|s|b|b\*)\s*$", stream)),
            widths=re.findall("(" + _NUMBER.decode() + r")\s+w", stream),
            stroke_colors=re.findall("(" + _NUMBER.decode() + r")\s+(" + _NUMBER.decode()
                                     + r")\s+(" + _NUMBER.decode() + r")\s+RG", stream),
            fill_colors=re.findall("(" + _NUMBER.decode() + r")\s+(" + _NUMBER.decode()
                                   + r")\s+(" + _NUMBER.decode() + r")\s+rg", stream),
            canonical_child="/CanonicalText" in document.xref_object(int(match.group(1))))
    return {"stage": stage, "semantic_style": _style_trace(style), "keys": keys,
            "annot_colors": annot.colors, "annot_border": annot.border,
            "annot_opacity": annot.opacity, "parent": parent}


def authorize(document, command: EditExternalAnnotation):
    page = document.load_page(command.page_index)
    root = int(command.logical_id.rsplit(":", 1)[1])
    if root not in {xref for xref, _, _ in page.annot_xrefs()}:
        raise DomainValidationError(MutationErrorCode.ANNOTATION_NOT_FOUND)
    annot = page.load_annot(root)
    profile = inspect_profile(document, page, annot)
    if (not getattr(profile.capabilities(), CAPABILITY_FOR_EDIT[command.edit])
            or (command.style is not None and not profile.capabilities().editable_style)):
        raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
    return page, annot, profile


def apply_external_edit(document: Any, command: EditExternalAnnotation, *,
                        normalization_trace: list[dict[str, Any]] | None = None) -> None:
    import pymupdf

    page, annot, profile = authorize(document, command)
    root = annot.xref
    original = document.xref_object(root)
    rollback_snapshot = snapshot_native_annotation(
        document, (command.page_index,), (root,), include_normal_appearance=True)
    original_keys = _parse(original)
    transform = NativeCoordinates.observe(document, annot)
    rect = PdfRect(*annot.rect)
    ink = value(document, root, "Subtype") == "/Ink"
    native_line = value(document, root, "Subtype") == "/Line"
    callout = not native_line and value(document, root, "IT") == "/FreeTextCallout"
    box = None if ink or native_line else text_box(document, annot, transform)
    source_has_border, source_border_width = native_border_style(document, annot)
    regenerate = False
    refresh_callout_parent = False
    desired: dict[str, str] = {}
    edit = command.edit
    if edit == ExternalEditKind.MOVE and not callout:
        moved = PdfRect(rect.x0 + command.delta[0], rect.y0 + command.delta[1],
                        rect.x1 + command.delta[0], rect.y1 + command.delta[1])
        dx, dy = moved.x0 - rect.x0, moved.y0 - rect.y0
        if abs(dx) + abs(dy) < 1e-6:
            raise DomainValidationError(MutationErrorCode.INVALID_GEOMETRY)
        desired["Rect"] = array(transform.native_rect(moved))
        allowed = MUTATION_KEYS["line" if native_line else "ink" if ink else "translation"]
        if native_line:
            endpoints = numbers(document, root, "L")
            desired["L"] = array(v + (dx * transform.sx if i % 2 == 0 else -dy * transform.sy)
                                 for i, v in enumerate(endpoints))
        if ink:
            paths = _parse(value(document, root, "InkList"))
            if not isinstance(paths, list) or any(not isinstance(path, list) or len(path) % 2 for path in paths):
                raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
            desired["InkList"] = "[" + " ".join(array(
                float(v) + (dx * transform.sx if i % 2 == 0 else -dy * transform.sy)
                for i, v in enumerate(path)) for path in paths) + "]"
    elif edit in (ExternalEditKind.MOVE, ExternalEditKind.RESIZE, ExternalEditKind.ANCHOR):
        line = _cl(document, root, transform) if callout else ()
        new_box, new_line = box, line
        if edit == ExternalEditKind.MOVE:
            new_box, new_line = move_callout_box(box, line, *command.delta) if callout else (
                PdfRect(box.x0 + command.delta[0], box.y0 + command.delta[1],
                        box.x1 + command.delta[0], box.y1 + command.delta[1]), line)
        elif edit == ExternalEditKind.ANCHOR:
            new_line = callout_line(box, command.anchor) if callout else line
        else:
            new_box = command.rect
            if callout:
                attach = line[-1]
                new_attach = PdfPoint(
                    new_box.x0 + (attach.x - box.x0) * (new_box.x1-new_box.x0)/(box.x1-box.x0),
                    new_box.y0 + (attach.y - box.y0) * (new_box.y1-new_box.y0)/(box.y1-box.y0))
                dx, dy = new_attach.x-attach.x, new_attach.y-attach.y
                new_line = (line[0], *(PdfPoint(p.x+dx, p.y+dy) for p in line[1:]))
        before_union, after_union = _union(box, line) if line else box, _union(new_box, new_line) if line else new_box
        outer = PdfRect(after_union.x0 - (before_union.x0-rect.x0),
                        after_union.y0 - (before_union.y0-rect.y0),
                        after_union.x1 + (rect.x1-before_union.x1),
                        after_union.y1 + (rect.y1-before_union.y1))
        desired["Rect"] = array(transform.native_rect(outer))
        desired["RD"] = array(((new_box.x0-outer.x0)*transform.sx, (outer.y1-new_box.y1)*transform.sy,
                               (outer.x1-new_box.x1)*transform.sx, (new_box.y0-outer.y0)*transform.sy))
        if callout:
            desired["CL"] = array(v for p in new_line for v in transform.native_point(p))
        box = new_box
        allowed = MUTATION_KEYS["callout" if callout else "box"]
        # A Callout geometry edit needs a fresh PyMuPDF parent AP because its
        # native Form contains both box and leader. It never creates the
        # canonical child: only resize is a text-layout normalization.
        regenerate = edit == ExternalEditKind.RESIZE
        refresh_callout_parent = callout and edit in (ExternalEditKind.MOVE, ExternalEditKind.ANCHOR)
    elif edit == ExternalEditKind.TEXT:
        desired["Contents"] = pymupdf.get_pdf_str(command.rich_text.plain)
        desired["RC"] = pymupdf.get_pdf_str(_rich_markup(command.rich_text))
        allowed, regenerate = MUTATION_KEYS["text"], True
    else:
        allowed, regenerate = MUTATION_KEYS["style"], True
    if command.style is not None:
        style = command.style
        # Style means box style; run formatting goes through the TEXT command
        # carrying the complete rich text. No invisible run normalization here.
        desired.update(C=array((style.fill.red, style.fill.green, style.fill.blue)) if style.fill else "null",
                       DA=pymupdf.get_pdf_str(f"{_pdf_rgb(style.border)} rg /{_pdf_font(style.font_family)[1]} {style.font_size:g} Tf"),
                       DS=pymupdf.get_pdf_str(_default_style(style)),
                       BS=f"<</W {style.border_width:g} /S /S>>", CA=f"{style.opacity:g}")
        allowed = allowed | MUTATION_KEYS["style"]
    # These values are appearance inputs only.  They are deliberately not a
    # geometry/fit validation precondition for any external operation.
    rich = command.rich_text or profile.rich_text
    style = command.style or profile.style
    if regenerate and (rich is None or style is None):
        raise DomainValidationError(MutationErrorCode.READ_ONLY_ANNOTATION)
    if regenerate:
        if normalization_trace is not None:
            normalization_trace.append(_normalization_snapshot(
                document, command.page_index, root, "A_before_resize", profile.style))
            normalization_trace.append(_normalization_snapshot(
                document, command.page_index, root, "B_semantic_native_style", style))
        # A first imported resize/text edit must retain an absent border as
        # absent. A supplied style command is the only explicit instruction
        # that may choose a new border width.
        border_width = (style.border_width if command.style is not None or source_has_border
                        else 0.0)
        # The first layout/text/style edit adopts app typography once.  The
        # original root remains the History snapshot, so Undo restores its AP,
        # embedded resources and Acrobat font names exactly.
        desired.update(
            Contents=pymupdf.get_pdf_str(rich.plain),
            RC=pymupdf.get_pdf_str(_rich_markup(rich)),
            C=array((style.fill.red, style.fill.green, style.fill.blue)) if style.fill else "null",
            DA=pymupdf.get_pdf_str(
                f"{_pdf_rgb(style.border)} rg /{_pdf_font(style.font_family)[1]} {style.font_size:g} Tf"),
            DS=pymupdf.get_pdf_str(_default_style(style)),
            BS=f"<</W {border_width:g} /S /S>>", CA=f"{style.opacity:g}")
        allowed = allowed | MUTATION_KEYS["text"] | MUTATION_KEYS["style"]
    desired["M"] = pymupdf.get_pdf_str(datetime.now(timezone.utc).strftime("D:%Y%m%d%H%M%SZ"))
    try:
        for key, encoded in desired.items():
            document.xref_set_key(root, key, encoded)
        if refresh_callout_parent:
            # ``update`` creates the native Callout parent from the changed
            # /Rect, /RD and /CL. Restore the imported root afterwards, then
            # retain only geometry, modification time and the new parent AP.
            page = document.load_page(command.page_index)
            annot = page.load_annot(root)
            annot.update()
            new_ap = document.xref_get_key(root, "AP")[1]
            document.update_object(root, original)
            for key, encoded in {**desired, "AP": new_ap}.items():
                document.xref_set_key(root, key, encoded)
            page = document.load_page(command.page_index)
        elif regenerate:
            document.xref_set_key(root, "AP", "null")
            if normalization_trace is not None:
                normalization_trace.append(_normalization_snapshot(
                    document, command.page_index, root, "C_before_annot_update", style))
                # The diagnostic intentionally loads a separate inspection
                # handle, so use a fresh page rather than reloading the now
                # invalidated earlier handle.
                page = document.load_page(command.page_index)
            else:
                page = document.reload_page(page)
            annot = page.load_annot(root)
            # FreeText does not support ``set_colors``. Its generated AP takes
            # the box paint from these update arguments. Width and style are
            # not ``update`` parameters, so set them on this fresh native
            # shell before regenerating its parent AP, exactly as app-created
            # Text Boxes do. The restored root below remains canonical state.
            annot.set_border(width=border_width, style="S")
            annot.update(opacity=style.opacity,
                         border_color=(style.border.red, style.border.green, style.border.blue),
                         fill_color=(style.fill.red, style.fill.green, style.fill.blue)
                         if style.fill is not None else None)
            if normalization_trace is not None:
                normalization_trace.append(_normalization_snapshot(
                    document, command.page_index, root, "D_after_annot_update", style))
            new_ap = document.xref_get_key(root, "AP")[1]
            # Keep only the authorized appearance and explicit intent fields.
            # update() is not allowed to normalize any other native key.
            document.update_object(root, original)
            for key, encoded in {**desired, "AP": new_ap}.items():
                document.xref_set_key(root, key, encoded)
            if normalization_trace is not None:
                page = document.load_page(command.page_index)
            # PyMuPDF remains the sole owner of the parent AP: its BBox,
            # Matrix, clip, ExtGState, box and Callout leader stay untouched.
            # The dedicated patcher accepts only the one pinned generated text
            # slot and adds an isolated canonical child Form in that slot.
            patch_native_freetext_appearance(document, page, root, rich)
            if normalization_trace is not None:
                normalization_trace.append(_normalization_snapshot(
                    document, command.page_index, root, "E_after_canonical_patch", style))
        after = _parse(document.xref_object(root))
        changed = {key.lstrip("/") for key in original_keys.keys() | after.keys()
                   if original_keys.get(key) != after.get(key)}
        if changed - allowed:
            raise RuntimeError("External mutation exceeded native key allowlist")
        _refresh_page(document, page, command.page_index)
    except Exception:
        try:
            current = snapshot_native_annotation(
                document, (command.page_index,), (root,), include_normal_appearance=True)
            restore_native_annotation(document, rollback_snapshot, current)
        except Exception as rollback_error:
            document.close()
            raise RuntimeError("External edit rollback failed; document closed") from rollback_error
        raise
