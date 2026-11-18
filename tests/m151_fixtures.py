"""Synthetic imported appearances for the M15.1 regressions."""

from html import escape
from pathlib import Path
import sys
from textwrap import wrap


BOX = "imported-box"
CALLOUT = "imported-callout"
OTHER_CALLOUT = "imported-callout-off-page"
CALLOUT_PAGE = 5
BOX_PAGE = 11


def annotation_xref(page, name: str) -> int:
    xref, = (xref for xref, _, nm in page.annot_xrefs() if nm == name)
    return xref


def _imported_text(doc, page, annot, name, box, text, *, line=None, heading="") -> None:
    import pymupdf

    rect = pymupdf.Rect(box)
    xref = annot.xref
    # Exercise the legacy color split and font mapping with a Base-14 font
    # aliased in the import. The alias requires no external font data.
    font = "ImportedSans-Regular" if name == BOX else "Helv"
    family = "Imported Sans" if name == BOX else "Helvetica"
    markup = escape(text).replace("\r", "&#13;")
    if heading:
        markup = f'<span style="font-weight:bold">{escape(heading)}</span>' + markup
    for key, value in {
        "NM": pymupdf.get_pdf_str(name),
        "Contents": pymupdf.get_pdf_str(heading + text),
        "C": "[.988235 .956863 .521576]",
        "BS": "null",
        "DA": pymupdf.get_pdf_str(f"0 G 0.9725 0.3922 0.3922 rg 0 Tc 0 Tw 100 Tz 0 TL 0 Ts 0 Tr /{font} 10 Tf"),
        "DS": pymupdf.get_pdf_str(f"font: {family},sans-serif 10.0pt; text-align:left; color:#F86464 "),
        "RC": pymupdf.get_pdf_str(
            f'<body xmlns="http://www.w3.org/1999/xhtml" style="font-family:{family};'
            f'font-size:10pt;color:#000000"><p>{markup}</p></body>'),
    }.items():
        doc.xref_set_key(xref, key, value)

    # Native, translated Form with its own resources and clipped text. This
    # deliberately starts without the application's canonical child Form.
    normal = int(doc.xref_get_key(xref, "AP/N")[1].split()[0])
    x0, y0, x1, y1 = (float(v) for v in doc.xref_get_key(xref, "Rect")[1].strip("[]").split())
    height = page.rect.height
    content = ["0.988235 0.956863 0.521576 rg", "0.9725 0.3922 0.3922 RG"]
    if line:
        anchor, knee, attachment = line
        end_x, end_y = anchor[0], height - anchor[1]
        content.extend((f"{end_x} {end_y} m", f"{knee[0]} {height - knee[1]} l",
                        f"{attachment[0]} {height - attachment[1]} l", "S"))
        # Explicit wings belong to the native appearance; /LE retains intent.
        direction = pymupdf.Point(knee[0] - anchor[0], anchor[1] - knee[1]).unit
        wing = pymupdf.Point(-direction.y, direction.x) * 4
        tip = pymupdf.Point(end_x, end_y)
        base = tip + direction * 9
        content.extend((f"{(base + wing).x:.4f} {(base + wing).y:.4f} m",
                        f"{end_x} {end_y} l",
                        f"{(base - wing).x:.4f} {(base - wing).y:.4f} l", "S"))
    content.extend((f"{rect.x0 + .5} {height - rect.y1 + .5} {rect.width - 1} {rect.height - 1} re", "B",
                    "q", f"{rect.x0 + 2} {height - rect.y1 + 2} {rect.width - 4} {rect.height - 4} re", "W", "n",
                    "0 g", "BT"))
    font_xref = page.insert_font(fontname="helv")
    resources = f"/{font} {font_xref} 0 R"
    baseline = height - rect.y0 - 12
    if heading:
        bold = page.insert_font(fontname="hebo")
        resources += f" /HeBo {bold} 0 R"
        content.extend(("/HeBo 10 Tf", f"1 0 0 1 {rect.x0 + 2} {baseline} Tm",
                        f"{pymupdf.get_pdf_str(heading)} Tj"))
        baseline -= 12
    content.append(f"/{font} 10 Tf")
    for line_text in wrap(text.strip(), width=int((rect.width - 4) / 5.5)):
        content.extend((f"1 0 0 1 {rect.x0 + 2} {baseline} Tm", f"{pymupdf.get_pdf_str(line_text)} Tj"))
        baseline -= 12
    content.extend(("ET", "Q"))
    doc.update_object(normal, f"<< /Type /XObject /Subtype /Form /FormType 1"
                              f" /BBox [{x0} {y0} {x1} {y1}] /Matrix [1 0 0 1 {-x0} {-y0}]"
                              f" /Resources << /Font << {resources} >> /ProcSet [/PDF /Text] >> >>")
    doc.update_stream(normal, ("\n".join(content) + "\n").encode("ascii"))


def _add_imported_set(doc, page, specs) -> None:
    import pymupdf

    annotations = []
    for _, box, text, line, heading in specs:
        annot = page.add_freetext_annot(box, heading + text, callout=line, border_width=1)
        annot.set_flags(4)
        annot.set_info(title="Example Reviewer", creationDate="D:20200101000000Z")
        annot.set_popup(pymupdf.Rect(725, 35, 880, 130))
        annot.set_open(False)
        annotations.append(annot)
    # Adding another native annotation can refresh earlier appearances on this page.
    for annot, (name, box, text, line, heading) in zip(annotations, specs, strict=True):
        _imported_text(doc, page, annot, name, box, text, line=line, heading=heading)


def create_imported_document(path: Path) -> None:
    """Generate a multi-page input with imported FreeText, Callouts, Ink and replies."""
    import pymupdf

    with pymupdf.open() as doc:
        for index in range(25):
            page = doc.new_page(width=700, height=900)
            page.insert_text((45, 65), f"Synthetic production proof - page {index + 1}", fontsize=13)
            page.insert_text((45, 90), "Page text and unrelated native annotations must survive Save As.")
            ink = page.add_ink_annot([[(65, 120), (100, 132), (127, 119)],
                                      [(68, 155), (93, 160)]])
            ink.set_info(title="Example Reviewer")
            doc.xref_set_key(ink.xref, "NM", pymupdf.get_pdf_str(f"example-ink-{index}"))
            ink.set_popup(pymupdf.Rect(125, 100, 270, 210))
            reply = page.add_text_annot((175, 150), "Synthetic reply")
            reply.set_info(title="Example Reply Author")
            reply.set_irt_xref(ink.xref)
            if index == CALLOUT_PAGE:
                _add_imported_set(doc, page, (
                    (CALLOUT, (370, 418, 565, 518),
                     "\rCheck the archive labels and compare the index with the sample entries. "
                     "Mark any repeated section numbers and send a summary to the example reviewer.",
                     ((288, 375), (358, 465), (370, 465)), "Synthetic review:"),
                    (OTHER_CALLOUT, (498, 195, 704, 294),
                     "Review the sequence of example entries and compare the recorded values "
                     "with the index below. Repeat the check after saving a new copy.",
                     ((305, 325), (486, 248), (498, 248)), ""),
                ))
            if index == BOX_PAGE:
                _add_imported_set(doc, page, (
                    (BOX, (510, 310, 640, 377),
                     "Confirm the sample entries and sort the reference labels across the index.", None, ""),
                    ("editable-box", (220, 220, 430, 295),
                     "Use this example for the text-editing round trip.", None, ""),
                ))
        doc.save(path, deflate=True, no_new_id=True)


if __name__ == "__main__":
    create_imported_document(Path(sys.argv[1]))
