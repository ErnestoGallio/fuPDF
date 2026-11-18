from pathlib import Path
import runpy
import struct
import tomllib
from types import SimpleNamespace

from PySide6.QtGui import QColor, QImage

from pdf_annotator.app.branding import ICON_PATH, apply_branding
from pdf_annotator.app.canonical_fonts import _FILES, _FONT_DIR
from pdf_annotator.services.review_contract.prompt_profiles import DEFAULT_PROMPT_PROFILE, PROMPT_PROFILES
from pdf_annotator.worker.canonical_text_ap import _FONTS


ROOT = Path(__file__).resolve().parents[1]
PREPARE = runpy.run_path(str(ROOT / "packaging/windows/prepare.py"))


def test_resources_and_branding_ignore_current_directory(app, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    apply_branding(app)
    assert app.applicationName() == "fuPDF"
    assert not app.windowIcon().isNull()
    assert ICON_PATH.is_file()
    assert len(_FILES) == 8
    assert all((_FONT_DIR / name).is_file() for name in _FILES)
    assert all(path.is_file() for _, path in _FONTS.values())


def test_ico_has_all_decodable_transparent_frames_and_is_reproducible():
    blob = ICON_PATH.with_suffix(".ico").read_bytes()
    assert blob == PREPARE["icon_bytes"](ICON_PATH)
    assert struct.unpack_from("<HHH", blob) == (0, 1, 7)
    dimensions = []
    for index in range(7):
        width, height, _, _, planes, bits, length, offset = struct.unpack_from(
            "<BBBBHHII", blob, 6 + 16 * index)
        image = QImage.fromData(blob[offset:offset + length], "PNG")
        assert not image.isNull() and image.hasAlphaChannel()
        assert image.pixelColor(0, 0).alpha() == 0
        assert (planes, bits) == (1, 32)
        assert (image.width(), image.height()) == (width or 256, height or 256)
        size = image.width()
        margin = round(size / 16)
        visible = [(x, y) for y in range(size) for x in range(size)
                   if image.pixelColor(x, y).alpha() >= 128]
        xs, ys = zip(*visible)
        left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
        assert min(left, top, size - 1 - right, size - 1 - bottom) >= margin
        assert abs(left - (size - 1 - right)) <= 1
        assert abs(top - (size - 1 - bottom)) <= 1
        logo_width, logo_height = right - left + 1, bottom - top + 1
        assert logo_height >= size - 2 * margin - 2
        assert abs(logo_width - logo_height * 732 / 765) <= 2
        dimensions.append(size)
    assert tuple(dimensions) == PREPARE["SIZES"]


def test_icon_crop_keeps_faint_fringe_and_ignores_distant_noise():
    image = QImage(20, 18, QImage.Format.Format_RGBA8888)
    image.fill(0)
    color = QColor(25, 80, 200, 255)
    fringe = QColor(25, 80, 200, 1)
    for y in range(6, 11):
        for x in range(7, 12):
            image.setPixelColor(x, y, color)
    image.setPixelColor(6, 8, fringe)
    image.setPixelColor(0, 0, fringe)
    cropped = PREPARE['crop_icon_content'](image)
    assert (cropped.width(), cropped.height()) == (9, 9)
    assert cropped.pixelColor(2, 2) == color
    assert cropped.pixelColor(1, 4) == fringe
    assert cropped.pixelColor(0, 0).alpha() == 0


def test_windows_metadata_comes_from_project_version():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    actual = (ROOT / "packaging/windows/version_info.txt").read_text(encoding="utf-8")
    assert actual == PREPARE["version_resource"](project["project"]["version"])
    assert "CompanyName" not in actual and "LegalCopyright" not in actual


def test_prepare_rejects_missing_resources_before_writing(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError, match="Required packaging resource missing"):
        PREPARE["prepare"](tmp_path)
    assert not list(tmp_path.rglob("*.ico"))


def test_build_configuration_contains_no_developer_checkout_path():
    for filename in ("fupdf.spec", "build.ps1", "launcher.py", "prepare.py"):
        text = (ROOT / "packaging/windows" / filename).read_text(encoding="utf-8")
        assert "C:/Users/" not in text and "C:\\Users\\" not in text


def test_windows_bundle_includes_the_registered_review_prompt(monkeypatch):
    spec = ROOT / "packaging/windows/fupdf.spec"
    monkeypatch.setenv("FUPDF_BUILD_MODE", "Windowed")
    config = runpy.run_path(str(spec), init_globals={
        "SPECPATH": str(spec.parent),
        "Analysis": lambda *args, **kwargs: SimpleNamespace(datas=kwargs["datas"], pure=[], binaries=[], scripts=[]),
        "PYZ": lambda *args: None,
        "EXE": lambda *args, **kwargs: None,
        "COLLECT": lambda *args, **kwargs: None,
    })
    prompt = PROMPT_PROFILES[DEFAULT_PROMPT_PROFILE].path
    assert prompt.is_file()
    assert (prompt, prompt.parent.relative_to(ROOT / "src")) in {
        (Path(source), Path(destination)) for source, destination in config["a"].datas
    }


def test_system_crt_spec_excludes_only_known_dlls(monkeypatch):
    spec = ROOT / "packaging/windows/fupdf.spec"
    entries = [
        ("VCRUNTIME140.dll", "source", "BINARY"),
        (r"PySide6\MSVCP140_1.DLL", "source", "BINARY"),
        ("shiboken6/vcruntime140_1.dll", "source", "BINARY"),
        ("PySide6/Qt6Core.dll", "source", "BINARY"),
        ("libcrypto-3.dll", "source", "BINARY"),
    ]
    for system_crt in ("0", "1"):
        monkeypatch.setenv("FUPDF_SYSTEM_VC_RUNTIME", system_crt)
        monkeypatch.setenv("FUPDF_BUILD_MODE", "Windowed")
        config = runpy.run_path(str(spec), init_globals={
            "SPECPATH": str(spec.parent),
            "Analysis": lambda *args, **kwargs: SimpleNamespace(
                datas=[], pure=[], binaries=entries, scripts=[]),
            "PYZ": lambda *args: None,
            "EXE": lambda *args, **kwargs: None,
            "COLLECT": lambda *args, **kwargs: None,
        })
        assert config["a"].binaries == (entries[3:] if system_crt == "1" else entries)
