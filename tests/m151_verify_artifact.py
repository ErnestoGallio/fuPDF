"""Read-only benchmark semantic check, always launched as its own process."""

from hashlib import sha256
import json
from pathlib import Path
import sys

from tests.m151_helpers import inspect


def main(source: Path, output: Path) -> None:
    import pymupdf
    from pdf_annotator.worker.save_validation import _parse

    manifest = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert sha256(source.read_bytes()).hexdigest() == manifest["source_sha256"]
    assert sha256(output.read_bytes()).hexdigest() == manifest["output_sha256"]
    before, after = inspect(source), inspect(output)
    normalized_style = {"DA", "DS", "BS", "CA"}
    allowed = {
        ("text", "freetext-rich-v1"): {"Contents", "RC", "AP", "M"} | normalized_style,
        ("resize", "callout-rich-v1"): {"Rect", "RD", "CL", "RC", "AP", "M"} | normalized_style,
        ("move", "ink-translation-v1"): {"Rect", "InkList", "M"},
        ("move", "freetext-rich-v1"): {"Rect", "M"},
    }
    edits = manifest["changes"]
    assert {(edit["operation"], edit["profile"]) for edit in edits} == allowed.keys()
    roots = {edit["xref"]: allowed[edit["operation"], edit["profile"]] for edit in edits}
    assert len(edits) == len(roots) == len(allowed)
    oldrows = {a["xref"]: a for a in before["annotations"]}
    newrows = {a["xref"]: a for a in after["annotations"]}
    assert oldrows.keys() == newrows.keys()
    for edit in edits:
        row = oldrows[edit["xref"]]
        assert (row["page"] + 1, row["profile"]) == (edit["page"], edit["profile"])
    checked, packaging = 0, []
    for xref, original in before["objects"].items():
        if int(xref) in roots:
            continue
        parsed = _parse(original["source"])
        # Full Save As expands compressed objects and rebuilds cross references.
        # Their contained native objects are still checked separately below.
        if isinstance(parsed, dict) and parsed.get("/Type") in ("/ObjStm", "/XRef"):
            packaging.append(int(xref))
            continue
        saved = after["objects"][xref]
        assert parsed == _parse(saved["source"]), f"Object changed: {xref}"
        assert original["stream"] == saved["stream"], f"Decoded stream changed: {xref}"
        checked += 1
    changes = {}
    for edit in edits:
        xref = edit["xref"]
        original, saved = oldrows[xref], newrows[xref]
        old, new = original["keys"], saved["keys"]
        actual = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
        assert actual and actual <= roots[xref], (xref, actual)
        if edit["operation"] == "resize":
            assert {"Rect", "CL", "AP"} <= actual
            assert saved["rich_text"] == original["rich_text"]
        if edit["operation"] == "text":
            assert {"Contents", "RC", "AP"} <= actual
            expected_runs = original["rich_text"][:-1] + [
                {**original["rich_text"][-1], "text": original["rich_text"][-1]["text"] + " M15.1"}]
            assert saved["rich_text"] == expected_runs
        # The writer can normalize the syntax of legacy style keys during a
        # layout edit. Their effective style, including the border, must survive.
        assert saved.get("edit_style") == original.get("edit_style")
        changes[xref] = sorted(actual)
    result = {"status": "PASS", "unchanged_existing_objects": checked,
              "unchanged_annotations": len(oldrows) - len(roots),
              "root_changes": changes, "save_serialization_containers": packaging}
    output.with_name(output.stem + "-preservation.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    directory = output.parent / (output.stem + "-visual")
    directory.mkdir(parents=True, exist_ok=True)
    for tag, path in (("before", source), ("final", output)):
        with pymupdf.open(path) as document:
            for edit in edits:
                page = document[edit["page"] - 1]
                xref = edit["xref"]
                annot = page.load_annot(xref)
                clip = annot.rect + (-10, -10, 40, 40)
                page.get_pixmap(matrix=pymupdf.Matrix(2, 2), clip=clip, annots=True).save(
                    directory / f"{tag}-{xref}.png")
    print(json.dumps(result))


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
