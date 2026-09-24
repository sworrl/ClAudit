"""Offscreen smoke test for the PyQt6 tray app: import it, build the dialogs, render the header
meter. Skipped when PyQt6 is not installed (the core stays importable without it). CI runs this on
Linux, macOS, and Windows with QT_QPA_PLATFORM=offscreen."""
import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
QtWidgets = pytest.importorskip("PyQt6.QtWidgets")

import claudit  # noqa: E402
import claudit_gui as g  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_dialogs_construct(app, tmp_path, monkeypatch):
    monkeypatch.setattr(g.ScrubListDialog, "PATH", str(tmp_path / "scrub.txt"))
    monkeypatch.setattr(g.MuteListDialog, "PATH", str(tmp_path / "mute.txt"))
    d = g.ScrubListDialog()
    d.inp.setText("acme-corp")
    d._add()
    assert d._terms() == ["acme-corp"] and (tmp_path / "scrub.txt").read_text().strip() == "acme-corp"
    m = g.MuteListDialog()
    assert m.windowTitle() == "Mute list" and m._terms() == []


def test_header_meter_renders_live_and_fallback(app, tmp_path, monkeypatch):
    monkeypatch.setattr(claudit, "TOKENS_FILE", str(tmp_path / "tokens.json"))
    m = g.Main.__new__(g.Main)          # skip __init__: only the meter path is exercised
    QtWidgets.QMainWindow.__init__(m)
    m.tok_label = QtWidgets.QLabel("")
    m._usage = {"plan": "max", "five_hour": {"pct": 17.0, "resets_at": ""},
                "seven_day": {"pct": 39.0, "resets_at": ""}, "fetched": 0,
                "scoped": [{"name": "Fable", "pct": 62.0, "resets_at": "", "active": True}]}
    m._usage_dirty = True
    m._tok_tick = 10                    # not the startup tick, so no fetcher thread is started
    monkeypatch.setattr(claudit, "BURN_TOKENS", False)
    m._update_tokens()
    assert m.tok_label.text().endswith("5h 17% · 7d 39% · Fable 62% · stale")
    assert "Fable" in m.tok_label.toolTip()
    m._usage, m._usage_dirty = {}, True
    m._update_tokens()
    assert "est." in m.tok_label.text()
