"""Subprocess-only checks for the isolated complete FreeTextCallout AP spike."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skip(reason=("Historical rejected spike: the full-custom Callout AP failed the Acrobat "
                          "interoperability gate and was superseded by the native-parent child-Form design."))
def test_custom_callout_ap_spike(tmp_path: Path) -> None:
    artifact = tmp_path / "M15.1-custom-callout-ap-spike.pdf"
    subprocess.run([sys.executable, "-m", "pdf_annotator.spikes.custom_callout_ap", str(artifact)],
                   check=True, timeout=60)
    report = json.loads(artifact.with_name("inspection.json").read_text(encoding="utf-8"))
    by_name = {record["name"]: record for record in report["annotations"]}
    assert set(by_name) == {"simple-freetext", "rich-callout", "edge-callout"}
    assert report["verification"]["structural"]
    assert report["verification"]["mupdf_render_sensible"]
    assert report["verification"]["poppler_render_sensible"]
    simple = by_name["simple-freetext"]
    assert simple["dictionary"]["Subtype"][1] == "/FreeText"
    assert simple["ap_form"]["Type"][1] == "/XObject"
    assert simple["ap_form"]["Subtype"][1] == "/Form"
    assert all(token in simple["operators"] for token in ("re", "B", "W", "BT", "ET"))
    callout = by_name["rich-callout"]
    assert callout["dictionary"]["IT"][1] == "/FreeTextCallout"
    assert callout["dictionary"]["LE"][1] == "/OpenArrow"
    assert callout["dictionary"]["RD"][1] == "[.5 .5 .5 .5]"
    assert callout["dictionary"]["CA"][1] == ".82"
    assert "Source Sans 3" in callout["dictionary"]["RC"][1]
    assert "Courier Prime" in callout["dictionary"]["RC"][1]
    assert {font["resource"] for font in callout["fonts"]} == {"FSource", "FCourier"}
    assert all(font["to_unicode"] and font["font_file_xref"] for font in callout["fonts"])
    assert all(token in callout["operators"] for token in ("m", "l", "S"))
    edge = by_name["edge-callout"]
    assert edge["dictionary"]["CL"][1] != "null"
    assert edge["arrow_geometry"][0][0] > 592
    assert edge["arrow_geometry"][0][1] < 30
