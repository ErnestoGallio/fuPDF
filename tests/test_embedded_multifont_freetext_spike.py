"""Isolated, subprocess-only evidence for the M15.1 embedded-font spike."""

import json
from pathlib import Path
import subprocess
import sys


def test_embedded_multifont_freetext_spike(tmp_path: Path) -> None:
    artifact = tmp_path / "M15.1-embedded-fonts-spike.pdf"
    subprocess.run([sys.executable, "-m", "pdf_annotator.spikes.embedded_multifont_freetext", str(artifact)],
                   check=True, timeout=60)
    inspection = json.loads(artifact.with_name("inspection.json").read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in inspection["annotations"]}
    assert set(by_name) == {"source-only", "multi-run"}
    source = by_name["source-only"]
    multi = by_name["multi-run"]
    assert source["ap_font_uses"] == ["FSource"]
    assert "color:#000000" in source["rc"]
    assert multi["subtype"] == "/FreeText"
    assert multi["contents"] == "Per stampare utilizziamo cout nel programma."
    assert "font-family:Source Sans 3" in multi["rc"]
    assert "font-family:Courier Prime" in multi["rc"]
    assert multi["ap_font_uses"] == ["FSource", "FCourier", "FSource"]
    assert multi["run_encoding_matches"]
    assert [run["resource"] for run in multi["encoded_runs"]] == ["FSource", "FCourier", "FSource"]
    assert all(run["hex"] for run in multi["encoded_runs"])
    assert source["run_encoding_matches"]
    assert source["text_paint_black"]
    assert multi["text_paint_black"]
    assert multi["box_state"]["C"] == ["array", "[1 .95 .3]"]
    assert multi["box_state"]["DA"][1].startswith(".9 .1 .1 rg /FSource")
    assert multi["box_state"]["DS"][1].endswith("color:#E61A1A")
    assert multi["box_state"]["BS"] == ["dict", "<</W 2/S/S>>"]
    assert multi["box_state"]["CA"][0] in {"int", "float"}
    assert multi["box_state"]["CA"][1] == "1"
    assert multi["box_state"]["IC"] == ["null", "null"]
    assert source["box_state"] == multi["box_state"]
    assert {font["subtype"] for font in multi["fonts"]} == {"Type0"}
    assert all(font["to_unicode"] and font["font_file_xref"] for font in multi["fonts"])
    assert "Per\u00a0stampare" in multi["text"]
