"""Windows author preference and fallback without reading a real identity."""

import pytest

from pdf_annotator.app import windows_author


@pytest.mark.parametrize(("display", "login", "expected"), [
    (" Nome Unicode 日本語 ", "login", "Nome Unicode 日本語"),
    ("", "login", "login"),
    (OSError(), "login", "login"),
    ("\x00", "login", "login"),
    ("", "  ", "fuPDF"),
    (AttributeError(), OSError(), "fuPDF"),
])
def test_manual_annotation_author_precedence_and_fallback(monkeypatch, display, login, expected):
    calls = []

    def lookup(kind, value):
        calls.append(kind)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(windows_author, "_display_name", lambda: lookup("display", display))
    monkeypatch.setattr(windows_author, "_login_name", lambda: lookup("login", login))
    assert windows_author.manual_annotation_author() == expected
    assert calls == (["display"] if expected == "Nome Unicode 日本語" else ["display", "login"])
