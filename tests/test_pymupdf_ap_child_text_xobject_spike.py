"""Subprocess-only verification of the final isolated M15.1 child-Form probe."""

import json
from pathlib import Path
import subprocess
import sys


def test_pymupdf_ap_child_text_xobject_spike(tmp_path: Path) -> None:
    artifact = tmp_path / "M15.1-pymupdf-ap-child-text-xobject.pdf"
    subprocess.run([sys.executable, "-m", "pdf_annotator.spikes.pymupdf_ap_child_text_xobject", str(artifact)],
                   check=True, timeout=60)
    report = json.loads(artifact.with_name("inspection.json").read_text(encoding="utf-8"))
    parent = report["parent_ap"]
    child = report["child_form"]
    assert report["writer"]["pymupdf_version"] == "1.28.2"
    assert report["callout"]["semantic"]["IT"][1] == "/FreeTextCallout"
    assert report["callout"]["semantic"]["LE"][1] == "/OpenArrow"
    assert report["callout"]["semantic"]["C"][1] == "[1 .95 .3]"
    assert report["callout"]["semantic"]["DA"][1].startswith(".9 .1 .1 rg /FSource")
    assert report["callout"]["semantic"]["DS"][1].endswith("color:#E61A1A")
    assert parent["geometry_stream_unchanged"]
    assert parent["bbox_unchanged"] and parent["matrix_unchanged"]
    assert parent["only_added_xobject_resource"]
    assert parent["old_base14_text_not_painted"]
    assert parent["text_slot"]["source"].startswith("BT")
    assert ".9 .1 .1 RG" in parent["before"]["stream"]
    assert "/CanonicalText Do" in parent["after"]["stream"]
    assert report["semantic_state_unchanged"]
    assert report["checks"]["child_type_form"]
    assert report["checks"]["child_resources_isolated"]
    assert report["checks"]["embedded_type0_fonts"]
    assert report["checks"]["parent_calls_child"]
    assert report["checks"]["natural_text_matrix"]
    assert report["checks"]["bounding_box_sane"]
    assert report["checks"]["render_sensible"]
    assert {font["resource"] for font in child["fonts"]} == {"FSource", "FCourier"}
