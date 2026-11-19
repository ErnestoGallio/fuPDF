"""Conservative native profiles. Imported appearance data is never normalized here.

Candidate operations and released capabilities are deliberately separate: a
structural match cannot stand in for the ROADMAP M15.1 Acrobat manual gate.
"""

from dataclasses import dataclass
import re
from math import isfinite
from typing import Any
from xml.etree import ElementTree as ET

from ..domain.annotation_inventory import AnnotationCapabilities
from ..domain.rich_text import RichText, RichTextRun
from ..domain.styles import FreeTextStyle, RgbColor

# Add a (profile, operation) pair only with recorded Acrobat round-trip evidence.
# No environment flag, preference or IPC argument can bypass this release gate.
VERIFIED_EXTERNAL_EDITS: frozenset[tuple[str, str]] = frozenset({
    *( ("freetext-plain-v1", operation) for operation in ("move", "resize", "text", "style")),
    *( ("freetext-rich-v1", operation) for operation in ("move", "resize", "text", "style")),
    *( ("callout-rich-v1", operation) for operation in ("move", "resize", "anchor", "text", "style")),
})

_COMMON = frozenset("Type Subtype Rect AP C CA F NM T Subj Contents CreationDate M P Popup IRT RT".split())
_FREE = frozenset("IT RC DA DS RD CL LE BS Border Q Rotate".split())
_INK = frozenset(("InkList", "BS", "Border", "RD"))
_NUMBER = r"[+-]?(?:\d+\.?\d*|\.\d+)"
_XHTML = "http://www.w3.org/1999/xhtml"
_XFA = "http://www.xfa.org/schema/xfa-data/1.0/"


def numbers(document: Any, xref: int, key: str) -> tuple[float, ...]:
    kind, value = document.xref_get_key(xref, key)
    if kind != "array" or not re.fullmatch(r"\[\s*(?:" + _NUMBER + r"\s*)*\]", value):
        raise ValueError("Unsupported native numeric array")
    result = tuple(float(v) for v in re.findall(_NUMBER, value))
    if not all(isfinite(v) for v in result):
        raise ValueError("Nonfinite native geometry")
    return result


def value(document: Any, xref: int, key: str) -> str:
    return document.xref_get_key(xref, key)[1]


@dataclass(frozen=True, slots=True)
class ExternalProfile:
    name: str
    candidate: AnnotationCapabilities
    reason: str
    rich_text: RichText | None = None
    style: FreeTextStyle | None = None

    def capabilities(self, *, deletable: bool = False) -> AnnotationCapabilities:
        from ..domain.external_edits import CAPABILITY_FOR_EDIT
        # M23.1 explicitly enables rigid native Line translation. It preserves
        # the authored AP and is not a claim of a passed Acrobat manual gate.
        # M23.2 explicitly authorizes color-only Highlight editing, independently
        # of the Adobe manual gate. COLOR shares the existing style capability;
        # omit its alias below so it cannot override another type's STYLE gate.
        if self.name == "highlight-color-v1":
            return AnnotationCapabilities(deletable=deletable, editable_style=True)
        permitted = {field: getattr(self.candidate, field) and
                     ((self.name, operation.value) in VERIFIED_EXTERNAL_EDITS
                      or (self.name == "line-translation-v1" and operation.value == "move"))
                     for operation, field in CAPABILITY_FOR_EDIT.items() if operation.value != "color"}
        return AnnotationCapabilities(deletable=deletable, **permitted)


def _css(text: str) -> dict[str, str]:
    result = {}
    for declaration in text.split(";"):
        if not declaration.strip():
            continue
        key, sep, val = declaration.partition(":")
        key, val = key.strip(), val.strip()
        if not sep or not val or key in result:
            raise ValueError("Ambiguous CSS")
        result[key] = val
    return result


def _format(css: dict[str, str], inherited: dict[str, str]) -> dict[str, str]:
    allowed = {"font-family", "font-size", "color", "font-weight", "font-style",
               "font-stretch", "text-align"}
    if set(css) - allowed:
        raise ValueError("Unrepresentable CSS property")
    merged = {**inherited, **css}
    if merged.get("text-align", "left") != "left" or merged.get("font-stretch", "normal") != "normal":
        raise ValueError("Unrepresentable text layout")
    return merged


def _run(text: str, css: dict[str, str]) -> RichTextRun:
    family = css["font-family"].split(",")
    if len(family) > 2 or (len(family) == 2 and family[1].strip() != "sans-serif"):
        raise ValueError("Unrepresentable font fallback")
    name = family[0].strip().strip("'\"")
    name = {"Courier Std": "Courier"}.get(name, name)
    size = css["font-size"]
    color = css["color"]
    if not re.fullmatch(_NUMBER + "pt", size) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("Unrepresentable CSS value")
    if css.get("font-weight", "normal") not in ("normal", "bold") or css.get("font-style", "normal") not in ("normal", "italic"):
        raise ValueError("Unrepresentable emphasis")
    # Acrobat commonly uses CR in both /Contents and rich-text paragraph
    # nodes.  Keep that semantic separator in the imported model: the
    # canonical child Form handles it as a line break, while /Contents stays
    # lossless through the first normalization.
    return RichTextRun(text, name, float(size[:-2]),
                       RgbColor(*(int(color[i:i+2], 16) / 255 for i in (1, 3, 5))),
                       css.get("font-weight") == "bold", css.get("font-style") == "italic")


def parse_rich_text(markup: str, contents: str) -> RichText:
    if len(markup.encode("utf-8")) > 48 * 1024 or "<!" in markup:
        raise ValueError("Unsupported or oversized rich text")
    body = ET.fromstring(markup)
    if body.tag != f"{{{_XHTML}}}body" or set(body.attrib) - {
            "style", f"{{{_XFA}}}APIVersion", f"{{{_XFA}}}spec"}:
        raise ValueError("Unsupported rich text body")
    paragraphs = list(body)
    if len(paragraphs) != 1 or body.text not in (None, ""):
        raise ValueError("Unrepresentable paragraph layout")
    runs = []
    def walk(node, inherited, tag):
        if node.tag != f"{{{_XHTML}}}{tag}" or set(node.attrib) - {"style", "dir"}:
            raise ValueError("Unsupported rich text element")
        if node.get("dir", "ltr") != "ltr":
            raise ValueError("Unsupported writing direction")
        css = _format(_css(node.get("style", "")), inherited)
        if node.text:
            runs.append(_run(node.text, css))
        for child in node:
            if len(child):
                raise ValueError("Unsupported nested span")
            walk(child, css, "span")
            if child.tail:
                runs.append(_run(child.tail, css))
    walk(paragraphs[0], _format(_css(body.get("style", "")), {}), "p")
    if paragraphs[0].tail not in (None, ""):
        raise ValueError("Unsupported paragraph tail")
    rich = RichText(tuple(runs))
    if rich.plain != contents:
        raise ValueError("Contents and RC disagree")
    return rich


def _text_profile(document, annot) -> tuple[RichText, FreeTextStyle]:
    xref = annot.xref
    # Only the observed DA grammar, including Acrobat's neutral text operators.
    da = value(document, xref, "DA")
    da = re.sub(r"^0 G ", "", da)
    da = da.replace("0 Tc 0 Tw 100 Tz 0 TL 0 Ts 0 Tr ", "")
    da = re.sub(rf"^({_NUMBER}) g ", lambda match:
                " ".join((match[1],) * 3) + " rg ", da)
    match = re.fullmatch(rf"({_NUMBER}) ({_NUMBER}) ({_NUMBER}) rg /([A-Za-z-]+) ({_NUMBER}) Tf", da)
    if match is None:
        raise ValueError("Unrepresentable default appearance")
    r, g, b, font, size = match.groups()
    families = {"Helv": "Helvetica", "Helvetica": "Helvetica", "HeBo": "Helvetica",
                "Cour": "Courier", "Courier": "Courier", "CourierStd": "Courier",
                "TiRo": "Times", "Times-Roman": "Times"}
    if font not in families:
        raise ValueError("Unrepresentable default font")
    color = RgbColor(float(r), float(g), float(b))
    contents = annot.info.get("content", "")
    rc_kind, rc = document.xref_get_key(xref, "RC")
    plain = rc_kind == "null"
    if plain:
        rich = RichText.plain_text(contents, font_family=families[font], font_size=float(size),
                                  color=color, bold=font == "HeBo")
    elif rc_kind == "string":
        rich = parse_rich_text(rc, contents)
    else:
        raise ValueError("Unsupported RC representation")
    ds = value(document, xref, "DS")
    if not (plain and ds == "null") and not re.fullmatch(r"font: (?:bold )?(?:Helvetica|Times|'Courier Std'),sans-serif " +
                       _NUMBER + r"pt;\s*text-align:left;\s*color:#[0-9a-fA-F]{6}\s*", ds):
        raise ValueError("Unrepresentable default style")
    border = annot.border or {}
    if border.get("dashes") or border.get("style") not in (None, "S") or border.get("clouds", -1) > 0:
        raise ValueError("Unrepresentable border")
    fill = numbers(document, xref, "C") if value(document, xref, "C") != "null" else ()
    if len(fill) not in (0, 3):
        raise ValueError("Unrepresentable fill")
    style = FreeTextStyle(color, RgbColor(*fill) if fill else None, rich.runs[0].color,
                          1 if annot.opacity < 0 else annot.opacity,
                          1 if border.get("width", -1) < 0 else border["width"], float(size), families[font])
    return rich, style


def _mappable_appearance(document, xref):
    """Reject resource features the fixed writer cannot faithfully reproduce."""
    from .save_validation import _parse, Ref
    def resolved(item):
        return _parse(document.xref_object(item.number)) if isinstance(item, Ref) else item
    ap = resolved(_parse(value(document, xref, "AP")))
    if not isinstance(ap, dict) or set(ap) != {"/N"} or not isinstance(ap["/N"], Ref):
        raise ValueError("Unsupported appearance dictionary")
    form = resolved(ap["/N"])
    if set(form) - {"/Type", "/Subtype", "/FormType", "/BBox", "/Matrix", "/Resources", "/Length", "/Filter", "/DecodeParms"}:
        raise ValueError("Unknown appearance keys")
    matrix = form.get("/Matrix", [1, 0, 0, 1, 0, 0])
    if len(matrix) != 6 or tuple(float(v) for v in matrix[:4]) != (1, 0, 0, 1):
        raise ValueError("Unrepresentable appearance transform")
    resources = resolved(form.get("/Resources"))
    if not isinstance(resources, dict) or set(resources) - {"/Font", "/ProcSet"}:
        raise ValueError("Unrepresentable appearance resources")
    fonts = resolved(resources.get("/Font"))
    if not isinstance(fonts, dict) or not fonts:
        raise ValueError("Missing appearance fonts")
    for reference in fonts.values():
        font = resolved(reference)
        name = font.get("/BaseFont", "").replace("#20", " ")
        # Base-14 Acrobat fonts and the observed pinned-writer fallback fonts.
        # No arbitrary external embedded font is mapped to a UI family.
        if not re.fullmatch(r"/(?:Helvetica(?:-Bold|-Oblique|-BoldOblique)?|Times(?:-Roman|-Bold|-Italic|-BoldItalic)?|Courier(?:Std)?(?:-Bold|-Oblique|-BoldOblique)?|Nimbus Sans (?:Regular|Bold|Italic|Bold Italic)|Charis SIL(?: Regular| Bold| Italic| Bold Italic)?)", name):
            raise ValueError("Unrepresentable appearance font")


def _default_appearance_color(document: Any, annot: Any, fallback: RgbColor) -> RgbColor:
    """Return the FreeText border/default color from its native ``/DA``.

    Acrobat FreeText uses ``/C`` for the fill while its border/default color is
    carried by ``/DA`` (and mirrored by ``/DS``).  ``Annot.colors['stroke']``
    is not a reliable representation of that split for imported annotations.
    """
    da = value(document, annot.xref, "DA")
    rgb = re.search(rf"({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+rg", da)
    if rgb is not None:
        return RgbColor(*(float(rgb.group(index)) for index in (1, 2, 3)))
    gray = re.search(rf"(?:^|\s)({_NUMBER})\s+g(?:\s|$)", da)
    if gray is not None:
        component = float(gray.group(1))
        return RgbColor(component, component, component)
    return fallback


def native_border_style(document: Any, annot: Any) -> tuple[bool, float]:
    """Read a simple visible FreeText border without conflating it with ``/C``.

    Acrobat commonly leaves ``/BS`` absent while its normal appearance paints
    the box with ``B``. PyMuPDF then reports ``border.width == -1`` and exposes
    the fill as ``colors['stroke']``. For the bounded M15.1 profile, inspect
    only a one-token parent paint operator and its preceding line width; no
    arbitrary AP parsing or reconstruction is performed.
    """
    try:
        width = float((annot.border or {}).get("width", -1.0))
    except (TypeError, ValueError):
        width = -1.0
    if width > 0:
        return True, width
    try:
        kind, appearance = document.xref_get_key(annot.xref, "AP")
        match = re.search(r"/N\s+(\d+)\s+0\s+R", appearance) if kind == "dict" else None
        if match is None:
            return False, 0.0
        stream = document.xref_stream(int(match.group(1))).decode("ascii")
    except (RuntimeError, UnicodeDecodeError, ValueError):
        return False, 0.0
    if not re.search(r"(?m)^\s*(?:B|B\*|S|s|b|b\*)\s*$", stream):
        return False, 0.0
    widths = re.findall(rf"(?m)^\s*({_NUMBER})\s+w\s*$", stream)
    return True, float(widths[-1]) if widths else 1.0


def _fallback_text_profile(document: Any, annot: Any) -> tuple[RichText, FreeTextStyle]:
    """Editable baseline for a valid native FreeText whose cosmetics are opaque.

    The original dictionary/AP remains authoritative for operations that do not
    touch text or style.  This model exists only to give the shared plain-text
    editor a deterministic, bounded presentation when Acrobat CSS/DA exceeds
    the app's optional rich-text mapper.
    """
    colors = getattr(annot, "colors", {}) or {}

    def color(value: Any, default: RgbColor) -> RgbColor:
        try:
            values = tuple(float(item) for item in value or ())
            if len(values) == 3 and all(0 <= item <= 1 for item in values):
                return RgbColor(*values)
        except (TypeError, ValueError):
            pass
        return default

    border = _default_appearance_color(document, annot,
                                       color(colors.get("stroke"), RgbColor(.15, .15, .15)))
    fill_values = colors.get("fill")
    fill = color(fill_values, border) if fill_values else None
    raw_opacity = getattr(annot, "opacity", -1.0)
    try:
        opacity = 1.0 if float(raw_opacity) < 0 else min(1.0, max(0.0, float(raw_opacity)))
    except (TypeError, ValueError):
        opacity = 1.0
    has_border, width = native_border_style(document, annot)
    width = width if has_border else .01
    text = RgbColor(0, 0, 0)
    contents = str((annot.info or {}).get("content") or "")
    style = FreeTextStyle(border, fill, text, opacity, width, 10.0, "Helvetica")
    return RichText.plain_text(contents, font_family=style.font_family,
                               font_size=style.font_size, color=text), style


def _native_family(value: str) -> tuple[str, bool, bool]:
    """Canonicalize an imported CSS/PDF family without loading its font."""
    name = value.split(",", 1)[0].strip().strip("'\"")
    name = re.sub(r"^[A-Z]{6}\+", "", name)
    lowered = name.casefold()
    bold = bool(re.search(r"(?:\b|[- ])(?:bold|black|semibold)(?:\b|[- ])", lowered))
    italic = bool(re.search(r"(?:\b|[- ])(?:italic|oblique)(?:\b|[- ])", lowered))
    family = re.sub(r"(?:\b|[- ])(?:bold|black|semibold|italic|oblique|regular)(?:\b|[- ])",
                    " ", name, flags=re.I)
    family = re.sub(r"\s+", " ", family).strip(" -") or name
    return family.casefold(), bold, italic


def _normalised_imported_rich_text(document: Any, annot: Any) -> RichText:
    """Map native rich runs to the two app roles on first typographic edit.

    This is deliberately a semantic mapping, not an embedded-font importer.
    The dominant visible family becomes Helvetica and every distinct secondary
    family becomes Courier, while all representable per-run attributes remain.
    """
    xref = annot.xref
    contents = str((annot.info or {}).get("content") or "")
    da = value(document, xref, "DA")
    match = re.search(rf"/([^\s/]+)\s+({_NUMBER})\s+Tf", da)
    default_family = match.group(1) if match else "Imported"
    default_size = float(match.group(2)) if match else 10.0
    color_match = re.search(rf"({_NUMBER})\s+({_NUMBER})\s+({_NUMBER})\s+rg", da)
    default_color = (RgbColor(*(float(color_match.group(index)) for index in (1, 2, 3)))
                     if color_match else RgbColor(0, 0, 0))
    kind, markup = document.xref_get_key(xref, "RC")
    raw_runs: list[tuple[str, str, float, RgbColor, bool, bool]] = []
    if kind == "null":
        family, bold, italic = _native_family(default_family)
        raw_runs.append((contents, family, default_size, default_color, bold, italic))
    elif kind == "string":
        body = ET.fromstring(markup)
        if body.tag != f"{{{_XHTML}}}body":
            raise ValueError("Unsupported native rich-text root")

        def css(text: str) -> dict[str, str]:
            result = {}
            for item in text.split(";"):
                key, separator, val = item.partition(":")
                if separator and key.strip() and val.strip():
                    result[key.strip().casefold()] = val.strip()
            return result

        defaults = {"font-family": default_family, "font-size": f"{default_size:g}pt",
                    "color": "#%02x%02x%02x" % tuple(round(v * 255) for v in
                                                          (default_color.red, default_color.green,
                                                           default_color.blue))}

        def append(text: str | None, inherited: dict[str, str]) -> None:
            if not text:
                return
            family, named_bold, named_italic = _native_family(inherited.get("font-family", default_family))
            size = inherited.get("font-size", f"{default_size:g}pt")
            color = inherited.get("color", defaults["color"])
            if not re.fullmatch(_NUMBER + "pt", size) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                raise ValueError("Unsupported native rich-text run")
            raw_runs.append((text, family, float(size[:-2]),
                             RgbColor(*(int(color[index:index + 2], 16) / 255 for index in (1, 3, 5))),
                             named_bold or inherited.get("font-weight", "normal").casefold() in ("bold", "700"),
                             named_italic or inherited.get("font-style", "normal").casefold() in ("italic", "oblique")))

        def walk(node: Any, inherited: dict[str, str]) -> None:
            current = {**inherited, **css(node.get("style", ""))}
            append(node.text, current)
            for child in node:
                walk(child, current)
                append(child.tail, current)

        walk(body, {**defaults, **css(body.get("style", ""))})
    else:
        raise ValueError("Unsupported native rich-text representation")
    if not raw_runs or "".join(item[0] for item in raw_runs) != contents:
        raise ValueError("Native rich text disagrees with Contents")
    counts: dict[str, int] = {}
    order: list[str] = []
    for text, family, *_attributes in raw_runs:
        if family not in counts:
            counts[family] = 0
            order.append(family)
        counts[family] += sum(not character.isspace() for character in text)
    dominant = max(order, key=lambda family: (counts[family], -order.index(family)))
    return RichText(tuple(RichTextRun(text, "Helvetica" if family == dominant else "Courier", size,
                                      color, bold, italic)
                          for text, family, size, color, bold, italic in raw_runs))


def _normalised_imported_text_profile(document: Any, annot: Any) -> tuple[RichText, FreeTextStyle]:
    rich = _normalised_imported_rich_text(document, annot)
    border = _default_appearance_color(document, annot, rich.runs[0].color)
    fill_values = numbers(document, annot.xref, "C") if value(document, annot.xref, "C") != "null" else ()
    fill = RgbColor(*fill_values) if len(fill_values) == 3 else None
    has_border, width = native_border_style(document, annot)
    opacity = 1.0 if float(getattr(annot, "opacity", -1.0)) < 0 else float(annot.opacity)
    first = rich.runs[0]
    return rich, FreeTextStyle(border, fill, first.color, max(0.0, min(1.0, opacity)),
                               max(.01, width) if has_border else .01, first.font_size, first.font_family)


def inspect_profile(document: Any, page: Any, annot: Any) -> ExternalProfile:
    """Classify native edit types without retroactively validating their baseline.

    A mapper may be unable to describe every Acrobat CSS/resource detail, but
    that is not a reason to downgrade a valid FreeText, Callout or Ink to
    read-only.  The worker writes only operation-specific allowlisted keys and
    retains every untouched native key verbatim.
    """
    denied = lambda reason: ExternalProfile("unsupported", AnnotationCapabilities(), reason)
    try:
        xref = annot.xref
        subtype = value(document, xref, "Subtype")
        if subtype not in ("/FreeText", "/Ink", "/Line", "/Highlight"):
            return denied("No M15.1 edit equivalent for this subtype")
        keys = set(document.xref_get_keys(xref))
        flags = int(value(document, xref, "F")) if "F" in keys else 0
        if flags & ~4:
            return denied("Hidden, locked, read-only or display-dependent flags")
        if value(document, xref, "IRT") != "null" or value(document, xref, "RT") != "null":
            return denied("Reply/group member has dependent semantics")
        popup = value(document, xref, "Popup")
        if popup != "null":
            if (not re.fullmatch(r"\d+ 0 R", popup)
                    or value(document, int(popup.split()[0]), "Subtype") != "/Popup"
                    or value(document, int(popup.split()[0]), "Parent") != f"{xref} 0 R"):
                return denied("Unverified popup relationship")
        for other, _, _ in page.annot_xrefs():
            if (value(document, other, "IRT") == f"{xref} 0 R"
                    and value(document, other, "RT") not in ("null", "/R")):
                return denied("Incoming group or unknown reply relationship")
        rect = annot.rect
        if rect.is_empty or rect.is_infinite or not all(isfinite(item) for item in rect):
            return denied("Invalid non-finite native geometry")
        if subtype == "/Highlight":
            import pymupdf
            quads = numbers(document, xref, "QuadPoints")
            if not quads or len(quads) % 8 or not all(q.is_convex for q in
                    (pymupdf.Quad(annot.vertices[i:i+4])
                      for i in range(0, len(annot.vertices), 4))):
                return denied("Invalid Highlight quads")
            return ExternalProfile("highlight-color-v1", AnnotationCapabilities(editable_style=True),
                                   "M23.2 native Highlight color; Acrobat manual gate pending")
        if subtype == "/Line":
            endpoints = numbers(document, xref, "L")
            if len(endpoints) != 4 or endpoints[:2] == endpoints[2:]:
                return denied("Invalid Line endpoints")
            return ExternalProfile("line-translation-v1", AnnotationCapabilities(movable=True),
                                   "M23.1 native L/Rect translation; authored appearance preserved")
        if subtype == "/Ink":
            paths = annot.vertices
            if not paths or any(len(path) < 2 for path in paths):
                return denied("Invalid Ink strokes")
            if any(not isfinite(v) for path in paths for point in path for v in point):
                return denied("Invalid Ink coordinates")
            return ExternalProfile("ink-translation-v1", AnnotationCapabilities(movable=True),
                                   "Native Ink baseline; translate InkList and Rect without rewriting AP")
        intent = value(document, xref, "IT")
        if intent not in ("null", "/FreeText", "/FreeTextCallout"):
            return denied("No M15.1 edit equivalent for this FreeText intent")
        if value(document, xref, "RD") != "null":
            rd = numbers(document, xref, "RD")
            if len(rd) != 4:
                return denied("Invalid native text insets")
        callout = intent == "/FreeTextCallout"
        if callout:
            if len(numbers(document, xref, "CL")) not in (4, 6) or len(numbers(document, xref, "RD")) != 4:
                return denied("Invalid callout line or insets")
        try:
            rich, style = _text_profile(document, annot)
        except (ValueError, KeyError, ET.ParseError):
            try:
                rich, style = _normalised_imported_text_profile(document, annot)
            except (ValueError, KeyError, ET.ParseError, TypeError):
                rich, style = _fallback_text_profile(document, annot)
                reason = "Opaque native text uses a one-family normalization preview; gate pending"
            else:
                reason = ("Imported text mapped to canonical Source Sans 3/Courier Prime roles "
                          "on first layout edit; gate pending")
        else:
            reason = "Mapped Acrobat text/style and native geometry; Acrobat gate pending"
        return ExternalProfile("callout-rich-v1" if callout else
            "freetext-plain-v1" if value(document, xref, "RC") == "null" else "freetext-rich-v1",
            AnnotationCapabilities(movable=True, resizable=True, editable_text=True,
                                   editable_style=True, movable_anchor=callout),
            reason, rich, style)
    except (ValueError, TypeError, RuntimeError, OverflowError, AttributeError, IndexError, RecursionError):
        return denied("Malformed or unrepresentable native profile")
