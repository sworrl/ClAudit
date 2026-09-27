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
    m._usage_last_fetch = 4e9           # nothing has poked since; no fetcher thread is started
    m._tok_tick = 10                    # not the startup tick either
    monkeypatch.setattr(claudit, "BURN_TOKENS", False)
    m._update_tokens()
    assert m.tok_label.text().endswith("5h 17% · 7d 39% · Fable 62% · stale")
    assert "Fable" in m.tok_label.toolTip()
    m._usage, m._usage_dirty = {}, True
    m._update_tokens()
    assert "est." in m.tok_label.text()


def test_usage_bars_paint(app):
    bars = g.UsageBars()
    bars.resize(520, 120)
    bars.set_rows([{"label": "5-hour window", "pct": 18.0, "resets": "in 4h 13m", "active": False},
                   {"label": "7-day window", "pct": 40.0, "resets": "in 2h 23m", "active": False},
                   {"label": "7-day Fable", "pct": 92.0, "resets": "in 2h 23m", "active": True}])
    assert bars.minimumHeight() == g.UsageBars.ROW_H * 3 + 8
    img = bars.grab().toImage()
    assert not img.isNull() and img.width() == 520
    bars.set_rows([])
    assert bars.rows == [] and not bars.grab().toImage().isNull()
    assert claudit.usage_tooltip({}, "watching live", "9.9.9").startswith("ClAudit v9.9.9: watching live")
