#!/usr/bin/env python3
"""claudit_gui — Qt tray app + window for the false-positive block watcher.

- System-tray icon + menu (Qt StatusNotifier; renders on KDE Wayland).
- Window listing PENDING (detected, not yet filed) and REPORTED issues, with each
  reported issue's live GitHub status (open/closed).
- Double-click a row to see the details ("the working therein"): Request IDs, the
  block message, the prompt hint, and a link to the issue.
- Background watcher detects new blocks and queues them (files NOTHING automatically);
  you file via the tray menu or the Report button.

Run:  python3 claudit_gui.py [--interval 30] [-R owner/repo] [--auto]
"""

import argparse
import datetime
import json
import math
import os
import subprocess
import sys
import threading
import time

from PyQt6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claudit  # noqa: E402  (LLM_SCRUB flag)
import claudit_scan as cs  # noqa: E402

STATE_LOCK = threading.Lock()


REPO_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from PyQt6.QtSvgWidgets import QSvgWidget
    _HAVE_SVG = True
except Exception:
    _HAVE_SVG = False
try:
    sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
    import render_poll as _rp          # reuse render_trend_svg for the in-GUI chart
except Exception:
    _rp = None


def _git(*args, timeout=30):
    return subprocess.run(["git", "-C", REPO_DIR, *args], capture_output=True, text=True, timeout=timeout)


def git_commit():
    """Short commit hash of the running checkout (so the GUI shows exactly what's deployed)."""
    try:
        return _git("rev-parse", "--short", "HEAD", timeout=5).stdout.strip()
    except Exception:
        return ""


def git_pull_if_behind():
    """Look to GitHub and self-update: fetch origin, and if this checkout is strictly BEHIND the
    remote branch and the working tree is clean, fast-forward pull. Returns True if it pulled.
    Never force-updates over local/dirty/diverged state — it only ever fast-forwards."""
    try:
        branch = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=5).stdout.strip() or "main"
        if _git("fetch", "--quiet", "origin", branch).returncode != 0:
            return False
        local = _git("rev-parse", "HEAD", timeout=5).stdout.strip()
        remote = _git("rev-parse", f"origin/{branch}", timeout=5).stdout.strip()
        if not remote or local == remote:
            return False
        behind = _git("merge-base", "--is-ancestor", "HEAD", f"origin/{branch}", timeout=5).returncode == 0
        dirty = bool(_git("status", "--porcelain", timeout=10).stdout.strip())
        if behind and not dirty:
            return _git("pull", "--ff-only", "--quiet", "origin", branch, timeout=60).returncode == 0
    except Exception as e:
        print("update check failed:", e, file=sys.stderr)
    return False


class UpdateChecker(QtCore.QThread):
    """Off-thread: pull new commits from GitHub (if clean+behind), then flag if HEAD moved."""
    updated = QtCore.pyqtSignal()

    def __init__(self, launch_head):
        super().__init__()
        self.launch_head = launch_head

    def run(self):
        git_pull_if_behind()                 # auto-update from GitHub
        cur = git_commit()
        if cur and self.launch_head and cur != self.launch_head:
            self.updated.emit()


def fmt_ts(iso):
    """ISO 8601 UTC (e.g. 2026-06-25T06:45:24Z) -> local 'YYYY-MM-DD HH:MM:SS'."""
    if not iso:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso.replace("T", " ")[:19]

STYLE = """
* { font-size: 14px; }
QWidget { background: #15171c; color: #e6e8ec; }
QMainWindow, QDialog, QMessageBox { background: #15171c; }
QTableWidget { background: #1b1e25; alternate-background-color: #191c23;
    gridline-color: transparent; border: 1px solid #2a2e37; border-radius: 10px; outline: 0; }
QTableWidget::item { padding: 7px 8px; border-bottom: 1px solid #20242c; }
QTableWidget::item:hover { background: #232838; }
QTableWidget::item:selected { background: #3a2f63; color: #fff; }
QHeaderView::section { background: #20242e; color: #aeb6c2; padding: 8px 8px;
    border: 0; border-bottom: 1px solid #2a2e37; font-weight: 600; letter-spacing: 0.3px; }
QTabBar::tab { background: #1b1e25; color: #9aa0a6; padding: 7px 16px; margin-right: 3px;
    border-top-left-radius: 7px; border-top-right-radius: 7px; }
QTabBar::tab:selected { background: #232838; color: #f0f1f3; }
QTabWidget::pane { border: 1px solid #2a2e37; border-radius: 8px; top: -1px; }
QPushButton { background: #2a2f3a; color: #cbd2da; border: 1px solid #353b47;
    border-radius: 8px; padding: 7px 15px; font-weight: 600; }
QPushButton:hover { background: #343c4a; border-color: #44506a; }
QPushButton:disabled { color: #5b616b; background: #20242c; border-color: #262b33; }
QPushButton#primary { background: #8b5cf6; color: #fff; border: 0; }
QPushButton#primary:hover { background: #9d75f8; }
QPushButton#primary:disabled { background: #34304a; color: #7a7596; }
QLabel { color: #9aa0a6; }
QMenu { background: #1e2128; color: #e6e8ec; border: 1px solid #2a2e37; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 5px; }
QMenu::item:selected { background: #3a2f63; }
QMenu::indicator:checked { color: #8b5cf6; }
QScrollBar:vertical { background: #15171c; width: 12px; }
QScrollBar::handle:vertical { background: #343b47; border-radius: 6px; min-height: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; }
QWidget#header { background: #1b1e25; border: 1px solid #2a2e37; border-radius: 8px; }
QLabel#brand { color: #f0f1f3; font-size: 17px; font-weight: 700; }
QLabel#subtle { color: #9aa0a6; font-size: 12px; }
QLabel#brandg { color: #ffffff; font-size: 18px; font-weight: 800; background: transparent; }
QLabel#subg { color: #dfe2e8; font-size: 12px; background: transparent; }
QLabel#statsbar { color: #f2f4f7; font-size: 12px; font-weight: 600; background: transparent; }
QProgressBar#bf { background: #1b1e25; border: 1px solid #2a2e37; border-radius: 7px;
    text-align: center; color: #e6e8ec; font-weight: 600; }
QProgressBar#bf::chunk { background: #8b5cf6; border-radius: 6px; }
QComboBox, QLineEdit { background: #1b1e25; color: #e6e8ec; border: 1px solid #353b47;
    border-radius: 6px; padding: 5px 8px; }
QComboBox::drop-down { border: 0; width: 18px; }
QComboBox QAbstractItemView { background: #1e2128; color: #e6e8ec;
    selection-background-color: #3a2f63; border: 1px solid #2a2e37; }
QLineEdit { selection-background-color: #3a2f63; }
QTabWidget::pane { border: 1px solid #2a2e37; border-radius: 8px; top: -1px; }
QTabBar::tab { background: #1b1e25; color: #9aa0a6; padding: 7px 18px; border: 1px solid #2a2e37;
    border-bottom: 0; border-top-left-radius: 7px; border-top-right-radius: 7px; }
QTabBar::tab:selected { background: #232733; color: #e6e8ec; }
QListWidget { background: #1b1e25; color: #e6e8ec; border: 1px solid #2a2e37; border-radius: 8px; }
QListWidget::item { padding: 4px 6px; }
QListWidget::item:selected { background: #3a2f63; }
"""


try:
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    from PyQt6.QtOpenGL import QOpenGLShaderProgram, QOpenGLShader, QOpenGLVertexArrayObject
    _HAVE_GL = True
except Exception:
    _HAVE_GL = False

if _HAVE_GL:
    class ShaderBanner(QOpenGLWidget):
        """Animated GLSL 'plasma' banner behind the header. Falls back to a solid dark fill if the
        GPU/driver can't give us a 3.2 core context or the shader won't compile — never crashes."""
        _VS = "#version 150 core\nvoid main(){vec2 p=vec2((gl_VertexID==2)?3.0:-1.0," \
              "(gl_VertexID==1)?3.0:-1.0);gl_Position=vec4(p,0.0,1.0);}"
        _FS = ("#version 150 core\nout vec4 fragColor;uniform vec2 uRes;uniform float uTime;"
               "void main(){vec2 uv=gl_FragCoord.xy/uRes;vec2 p=uv*vec2(uRes.x/uRes.y,1.0)*3.0;"
               "float t=uTime*0.22;"
               "float v=sin(p.x+t)+sin(p.y*1.2+t*1.1)+sin((p.x+p.y)*0.7+t*0.8)+sin(length(p-1.5)*4.0-t*1.6);"
               "v*=0.22;vec3 deep=vec3(0.07,0.05,0.12),teal=vec3(0.16,0.50,0.46),acc=vec3(0.42,0.28,0.78);"
               "vec3 col=mix(deep,teal,0.5+0.5*sin(v*3.14159));col=mix(col,acc,0.5+0.5*cos(v*2.1));"
               "col*=0.5;fragColor=vec4(col,1.0);}")

        def __init__(self, parent=None):
            super().__init__(parent)
            fmt = QtGui.QSurfaceFormat()
            fmt.setVersion(3, 2)
            fmt.setProfile(QtGui.QSurfaceFormat.OpenGLContextProfile.CoreProfile)
            self.setFormat(fmt)
            self.prog = self.vao = None
            self.ok = False
            self._t0 = time.monotonic()
            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self.update)
            self._timer.start(40)             # ~25 fps, gentle on the CPU/GPU

        def initializeGL(self):
            try:
                self.prog = QOpenGLShaderProgram(self)
                self.prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, self._VS)
                self.prog.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, self._FS)
                self.prog.link()
                self.vao = QOpenGLVertexArrayObject(self)
                self.vao.create()
                self.ok = self.prog.isLinked() and self.vao.isCreated()
            except Exception as e:
                print("shader banner init failed (using fallback):", e, file=sys.stderr)
                self.ok = False

        def paintGL(self):
            try:
                f = self.context().functions()
                if not self.ok:
                    f.glClearColor(0.106, 0.118, 0.145, 1.0)
                    f.glClear(0x00004000)     # GL_COLOR_BUFFER_BIT
                    return
                f.glClearColor(0.07, 0.05, 0.12, 1.0)
                f.glClear(0x00004000)
                self.prog.bind()
                dpr = self.devicePixelRatio()
                self.prog.setUniformValue("uRes", float(self.width() * dpr), float(self.height() * dpr))
                self.prog.setUniformValue("uTime", float(time.monotonic() - self._t0))
                self.vao.bind()
                f.glDrawArrays(0x0004, 0, 3)   # GL_TRIANGLES
                self.vao.release()
                self.prog.release()
            except Exception as e:
                print("shader banner paint failed (using fallback):", e, file=sys.stderr)
                self.ok = False


class AnimatedBanner(QtWidgets.QWidget):
    """Safe animated header (pure QPainter, NO OpenGL) — a slow-drifting gradient with soft moving
    glows. Cannot segfault on any GL stack; works on every machine. This is the default."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("header")
        self.setMinimumHeight(56)
        self._t0 = time.monotonic()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(110)            # ~9 fps: a slow drift needs no more, keeps CPU low

    def showEvent(self, e):               # animate only while visible (paused in the tray)
        super().showEvent(e)
        if not self._timer.isActive():
            self._timer.start(110)

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        t = time.monotonic() - self._t0
        a = 0.5 + 0.5 * math.sin(t * 0.4)
        b = 0.5 + 0.5 * math.sin(t * 0.4 + 2.1)
        g = QtGui.QLinearGradient(0, 0, w, h)
        g.setColorAt(0.0, QtGui.QColor(18, 13, 31))
        g.setColorAt(0.5, QtGui.QColor(int(34 + 26 * a), int(60 + 34 * b), int(62 + 26 * a)))
        g.setColorAt(1.0, QtGui.QColor(int(60 + 34 * b), int(44 + 18 * a), int(104 + 30 * b)))
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(self.rect()), 8, 8)
        p.fillPath(path, g)
        p.setClipPath(path)
        # one soft moving glow (cheaper than two; software rendering pays per radial gradient)
        cx = w * (0.5 + 0.4 * math.sin(t * 0.25))
        cy = h * (0.5 + 0.4 * math.cos(t * 0.3))
        rg = QtGui.QRadialGradient(cx, cy, h * 1.6)
        rg.setColorAt(0.0, QtGui.QColor(139, 92, 246, 50))
        rg.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
        p.fillRect(self.rect(), QtGui.QBrush(rg))
        p.end()


class BreakdownBars(QtWidgets.QWidget):
    """Horizontal bar breakdown of the corpus (QPainter, no GL): open/closed and by kind."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.data = []   # [(label, value, color), ...]

    def set_data(self, rows):
        self.data = rows
        self.update()

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if not self.data:
            p.end()
            return
        mx = max((v for _, v, _ in self.data), default=1) or 1
        x0, pad = 130, 6
        track_w = max(40, self.width() - x0 - 52)
        rowh = (self.height() - pad) / max(1, len(self.data))
        font = QtGui.QFont()
        font.setPointSize(10)
        p.setFont(font)
        for i, (label, val, color) in enumerate(self.data):
            y = pad + i * rowh
            bh = max(8, rowh - 8)
            p.setPen(QtGui.QColor("#aeb6c2"))
            p.drawText(0, int(y), x0 - 10, int(bh),
                       QtCore.Qt.AlignmentFlag.AlignRight | QtCore.Qt.AlignmentFlag.AlignVCenter, label)
            tr = QtCore.QRectF(x0, y, track_w, bh)
            pth = QtGui.QPainterPath()
            pth.addRoundedRect(tr, bh / 2, bh / 2)
            p.fillPath(pth, QtGui.QColor("#20242c"))
            bw = track_w * val / mx
            if bw > 1:
                fr = QtGui.QPainterPath()
                fr.addRoundedRect(QtCore.QRectF(x0, y, bw, bh), bh / 2, bh / 2)
                p.fillPath(fr, QtGui.QColor(color))
            p.setPen(QtGui.QColor("#e6e8ec"))
            p.drawText(int(x0 + track_w + 8), int(y), 44, int(bh),
                       QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter, str(val))
        p.end()


def make_banner():
    """The animated header. Default = safe QPainter AnimatedBanner (no GL). The native GLSL shader
    is opt-in via CLAUDIT_GL=1 because some GL stacks segfault on a QOpenGLWidget context."""
    if os.environ.get("CLAUDIT_GL") == "1" and _HAVE_GL:
        try:
            return ShaderBanner()
        except Exception as e:
            print("GL banner unavailable, using animated fallback:", e, file=sys.stderr)
    try:
        return AnimatedBanner()
    except Exception as e:
        print("animated banner failed, using static header:", e, file=sys.stderr)
        w = QtWidgets.QWidget()
        w.setObjectName("header")
        return w


# ----------------------------- background workers -----------------------------
class Watcher(QtCore.QThread):
    acted = QtCore.pyqtSignal(int, str)    # (count, kind: "auto"|"queued"|"backfill"|"defend")

    DEFEND_INTERVAL = 240                   # seconds between dedup-defender sweeps (fast: 1 search/pass)

    def __init__(self, state, repo, interval, auto, backfill, backfill_interval, backfill_max,
                 defend=True):
        super().__init__()
        self.state, self.repo, self.interval, self._run = state, repo, interval, True
        self.auto = auto                   # toggled live from the tray menu
        self.backfill = backfill
        self.backfill_interval = backfill_interval
        self.backfill_max = backfill_max
        self.defend = defend               # auto-defend dup-bot flags; toggled from the tray menu
        self.reopen = False                # auto-reopen dup-bot-CLOSED issues; opt-in (off by default)
        self.bf_done = 0
        self.last_live = 0.0
        self.last_bf = 0.0
        self.last_defend = 0.0
        self.last_reopen = 0.0
        self.bf_delay = max(4.0, float(backfill_interval))   # seconds between drips, adaptive

    def run(self):
        with STATE_LOCK:
            cs.ensure_baseline(self.state)
            cs.prune_stale_backlog(self.state)   # drop backlog items that can never be filed
            cs.prune_stale_pending(self.state)   # drop queued sigs with no finding (e.g. old harness)
        self.acted.emit(0, "pruned")             # nudge the UI to refresh the backlog count
        while self._run:
            now = time.monotonic()
            # LIVE: new blocks always fire as soon as they're seen (every `interval` secs),
            # never gated by the backfill schedule.
            if now - self.last_live >= self.interval:
                self.last_live = now
                try:
                    with STATE_LOCK:
                        if self.auto:
                            n = cs.auto_cycle(self.state, self.repo, 0, lambda *a: None)
                        else:
                            n = cs.monitor_cycle(self.state, lambda fresh: None)
                    if n:
                        self.acted.emit(n, "auto" if self.auto else "queued")
                except Exception as e:
                    print("live error:", e, file=sys.stderr)
            # BACKFILL: as fast as GitHub allows — speed up on success, back off on rate-limit.
            capped = self.backfill_max and self.bf_done >= self.backfill_max
            if self.backfill and not capped and now - self.last_bf >= self.bf_delay:
                self.last_bf = now
                try:
                    with STATE_LOCK:
                        b, limited = cs.backfill_step(self.state, self.repo, 1, lambda *a: None)
                except Exception as e:
                    b, limited = 0, False
                    print("backfill error:", e, file=sys.stderr)
                if limited:
                    self.bf_delay = min(self.bf_delay * 2, 300)      # exponential back-off
                elif b:
                    self.bf_done += b
                    self.bf_delay = max(self.bf_delay * 0.8, 4.0)    # creep faster while it's safe
                    self.acted.emit(b, "backfill")
            # DEFEND: periodically 👎 + note every dup-bot-flagged issue (idempotent, paced inside).
            if self.defend and now - self.last_defend >= self.DEFEND_INTERVAL:
                self.last_defend = now
                try:
                    with STATE_LOCK:
                        d = cs.defend_all(self.repo, self.state)
                    if d:
                        self.acted.emit(d, "defend")
                except Exception as e:
                    print("defend error:", e, file=sys.stderr)
            # REOPEN: opt-in — reopen issues the dup-bot CLOSED as duplicates (hourly, idempotent).
            if self.reopen and now - self.last_reopen >= 3600:
                self.last_reopen = now
                try:
                    with STATE_LOCK:
                        rr = cs.reopen_dupe_closes(self.repo, self.state)
                    if rr:
                        self.acted.emit(rr, "reopen")
                except Exception as e:
                    print("reopen error:", e, file=sys.stderr)
            for _ in range(2):                                       # ~2s tick
                if not self._run:
                    return
                self.sleep(1)

    def stop(self):
        self._run = False


class Reporter(QtCore.QThread):
    done = QtCore.pyqtSignal(int)

    def __init__(self, state, repo):
        super().__init__()
        self.state, self.repo = state, repo

    def run(self):
        with STATE_LOCK:
            n = cs.file_pending(self.state, self.repo, False, 1, lambda *a: None)
        self.done.emit(n)


class CommunityFetcher(QtCore.QThread):
    """Fetch EVERY ClAudit-filed issue on the repo (all authors, open + closed) + your login.
    Keyed on the 'Filed automatically by ClAudit' body marker — so it shows ALL kinds (cyber, aup,
    harness) and bespoke titles, not just ones with 'false positive' in the title."""
    fetched = QtCore.pyqtSignal(list, str)

    def __init__(self, repo):
        super().__init__()
        self.repo = repo

    def run(self):
        items, me = [], ""
        try:
            me = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                                capture_output=True, text=True).stdout.strip()
        except Exception:
            pass
        try:
            out = subprocess.run(
                ["gh", "issue", "list", "-R", self.repo, "--state", "all", "--limit", "600",
                 "--search", '"Filed automatically by ClAudit"',
                 "--json", "number,state,stateReason,title,author,url,createdAt"],
                capture_output=True, text=True, check=True).stdout
            items = json.loads(out)
        except Exception as e:
            print("community fetch failed:", e, file=sys.stderr)
        self.fetched.emit(items, me)


class DedupWorker(QtCore.QThread):
    """Manual per-issue dedup: 👎 the dup-bot + post a 'not a duplicate' note on ONE issue (live)."""
    done = QtCore.pyqtSignal(int, bool)

    def __init__(self, state, repo, num):
        super().__init__()
        self.state, self.repo, self.num = state, repo, num

    def run(self):
        ok = False
        try:
            with STATE_LOCK:
                ok = cs.mark_not_duplicate(self.state, self.repo, self.num)
        except Exception as e:
            print("dedup error:", e, file=sys.stderr)
        self.done.emit(self.num, ok)


class ReopenOneWorker(QtCore.QThread):
    """Reopen ONE issue + post the 'not a duplicate' note (live)."""
    done = QtCore.pyqtSignal(int, bool)

    def __init__(self, repo, num):
        super().__init__()
        self.repo, self.num = repo, num

    def run(self):
        ok = False
        try:
            ok = cs.reopen_one(self.repo, self.num)
        except Exception as e:
            print("reopen error:", e, file=sys.stderr)
        self.done.emit(self.num, ok)


class DefendAllWorker(QtCore.QThread):
    """Defend EVERY dup-bot-flagged open issue (👎 + 'not a duplicate' note), idempotent + paced."""
    progress = QtCore.pyqtSignal(int, bool)   # (issue number, reaction landed)
    finished_n = QtCore.pyqtSignal(int)       # total defended this sweep

    def __init__(self, state, repo):
        super().__init__()
        self.state, self.repo = state, repo

    def run(self):
        n = 0
        try:
            with STATE_LOCK:
                n = cs.defend_all(self.repo, self.state,
                                  on_done=lambda num, ok: self.progress.emit(num, ok))
        except Exception as e:
            print("defend_all error:", e, file=sys.stderr)
        self.finished_n.emit(n)


class ScrubListDialog(QtWidgets.QDialog):
    """View / add / remove terms in the local PII denylist (~/.claude/claudit/scrub.txt)."""
    PATH = os.path.expanduser("~/.claude/claudit/scrub.txt")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PII denylist")
        self.resize(440, 480)
        if os.path.exists(cs.ICON):
            self.setWindowIcon(QtGui.QIcon(cs.ICON))
        v = QtWidgets.QVBoxLayout(self)
        info = QtWidgets.QLabel(
            "Names, orgs, hostnames, codenames — anything the regex can't know — are scrubbed from "
            "<b>every</b> report before it's filed. Word-boundary, case-insensitive. This file is "
            "<b>local only</b> and never committed.")
        info.setObjectName("subtle")
        info.setWordWrap(True)
        v.addWidget(info)
        self.lst = QtWidgets.QListWidget()
        self.lst.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        v.addWidget(self.lst, 1)
        row = QtWidgets.QHBoxLayout()
        self.inp = QtWidgets.QLineEdit()
        self.inp.setPlaceholderText("add a term and press Enter…")
        self.inp.returnPressed.connect(self._add)
        badd = QtWidgets.QPushButton("Add")
        badd.setObjectName("primary")
        badd.clicked.connect(self._add)
        brem = QtWidgets.QPushButton("Remove selected")
        brem.clicked.connect(self._remove)
        row.addWidget(self.inp, 1)
        row.addWidget(badd)
        row.addWidget(brem)
        v.addLayout(row)
        self.count = QtWidgets.QLabel("")
        self.count.setObjectName("subtle")
        v.addWidget(self.count)
        self._load()

    def _terms(self):
        return [self.lst.item(i).text() for i in range(self.lst.count())]

    def _load(self):
        self.lst.clear()
        if os.path.exists(self.PATH):
            for line in open(self.PATH):
                t = line.strip()
                if t and not t.startswith("#"):
                    self.lst.addItem(t)
        self.lst.sortItems()
        self.count.setText(f"{self.lst.count()} term(s) · {self.PATH}")

    def _save(self):
        terms = self._terms()
        os.makedirs(os.path.dirname(self.PATH), exist_ok=True)
        with open(self.PATH, "w") as fh:
            fh.write("\n".join(terms) + ("\n" if terms else ""))
        claudit._EXTRA = None              # invalidate cache so the running watcher reloads it
        self.count.setText(f"{len(terms)} term(s) · {self.PATH}")

    def _add(self):
        t = self.inp.text().strip()
        if t and not self.lst.findItems(t, QtCore.Qt.MatchFlag.MatchFixedString):
            self.lst.addItem(t)
            self.lst.sortItems()
            self.inp.clear()
            self._save()

    def _remove(self):
        for it in self.lst.selectedItems():
            self.lst.takeItem(self.lst.row(it))
        self._save()


class RepoStatsFetcher(QtCore.QThread):
    """Fetch ClAudit's own repo stats: stars (+ who starred), forks, watchers, owner followers."""
    fetched = QtCore.pyqtSignal(dict)

    def __init__(self, repo):
        super().__init__()
        self.repo = repo

    def run(self):
        d = {"stargazers": [], "followers": []}
        try:
            j = subprocess.run(["gh", "api", f"repos/{self.repo}", "--jq",
                                "{stars:.stargazers_count, forks:.forks_count, watchers:.subscribers_count, "
                                "issues:.open_issues_count, updated:.pushed_at}"],
                               capture_output=True, text=True).stdout
            d.update(json.loads(j or "{}"))
        except Exception as e:
            print("stats fetch failed:", e, file=sys.stderr)
        try:
            sg = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github.star+json",
                                 f"repos/{self.repo}/stargazers?per_page=100",
                                 "--jq", "[.[] | {login:.user.login, at:.starred_at}]"],
                                capture_output=True, text=True).stdout
            d["stargazers"] = json.loads(sg or "[]")
        except Exception:
            pass
        try:
            owner = self.repo.split("/")[0]
            o = subprocess.run(["gh", "api", f"users/{owner}", "--jq",
                                "{followers:.followers, following:.following, public_repos:.public_repos}"],
                               capture_output=True, text=True).stdout
            d["owner"] = json.loads(o or "{}")
            fl = subprocess.run(["gh", "api", f"users/{owner}/followers?per_page=100", "--jq", "[.[].login]"],
                                capture_output=True, text=True).stdout
            d["followers"] = json.loads(fl or "[]")
        except Exception:
            pass
        self.fetched.emit(d)


class PollWorker(QtCore.QThread):
    """Fetch the community-poll tally, or cast/switch the user's vote, off the UI thread."""
    done = QtCore.pyqtSignal(dict)

    def __init__(self, vote=None):
        super().__init__()
        self.vote = vote   # None -> just read counts; else 'plus'/'minus'/'eyes'

    def run(self):
        try:
            counts = cs.poll_vote(self.vote) if self.vote else cs.poll_counts()
        except Exception as e:
            print("poll:", e, file=sys.stderr)
            counts = {}
        self.done.emit(counts or {})


class IssueDetailFetcher(QtCore.QThread):
    """Build one issue's full picture: local ClAudit record + the live GitHub timeline."""
    fetched = QtCore.pyqtSignal(dict)

    def __init__(self, repo, num):
        super().__init__()
        self.repo, self.num = repo, num

    def run(self):
        d = {"num": self.num, "events": [], "reqs": [], "kind": "", "title": "",
             "state": "", "reason": "", "url": f"https://github.com/{self.repo}/issues/{self.num}"}
        try:
            for r in cs.load_issue_rows():       # our local record (issues.jsonl)
                if str(r.get("url", "")).rsplit("/", 1)[-1] == str(self.num):
                    d["reqs"], d["kind"] = r.get("reqs", []), r.get("kind", "")
                    break
        except Exception:
            pass
        comments, created = [], ""
        try:
            j = json.loads(subprocess.run(
                ["gh", "issue", "view", str(self.num), "-R", self.repo, "--json",
                 "title,state,stateReason,url,createdAt,comments"],
                capture_output=True, text=True).stdout or "{}")
            d.update(title=j.get("title", ""), state=(j.get("state", "") or "").lower(),
                     reason=(j.get("stateReason") or ""), url=j.get("url") or d["url"])
            comments, created = j.get("comments") or [], j.get("createdAt", "")
        except Exception as e:
            print("detail fetch failed:", e, file=sys.stderr)
        try:
            tl = json.loads(subprocess.run(
                ["gh", "api", f"repos/{self.repo}/issues/{self.num}/timeline", "--paginate"],
                capture_output=True, text=True).stdout or "[]")
        except Exception:
            tl = []
        ev = [(created, "📤", "Filed by ClAudit")] if created else []
        for c in comments:
            who, b = (c.get("author") or {}).get("login", "?"), (c.get("body", "") or "").lower()
            if "possible duplicate" in b or "closed as a duplicate" in b:
                ev.append((c.get("createdAt", ""), "🤖", "Dup-bot flagged as duplicate"))
            elif "not a duplicate" in b:
                ev.append((c.get("createdAt", ""), "🛡", "ClAudit defended — not a duplicate"))
            elif "recurred" in b:
                ev.append((c.get("createdAt", ""), "🔁", "Recurred — new Request IDs added"))
            else:
                ev.append((c.get("createdAt", ""), "💬", f"Comment by {who}"))
        for t in (tl if isinstance(tl, list) else []):
            e, who, at = t.get("event"), (t.get("actor") or {}).get("login", "?"), t.get("created_at", "")
            if e == "labeled" and (t.get("label") or {}).get("name") == "duplicate":
                ev.append((at, "🏷", f"Labeled 'duplicate' by {who}"))
            elif e == "closed":
                ev.append((at, "🔒", f"Closed by {who}" + (f" ({d['reason']})" if d.get("reason") else "")))
            elif e == "reopened":
                ev.append((at, "♻", f"Reopened by {who}"))
        ev.sort(key=lambda x: x[0] or "")
        d["events"] = ev
        d["defended"] = any("not a duplicate" in (c.get("body", "") or "").lower() for c in comments)
        self.fetched.emit(d)


class IssueDetailDialog(QtWidgets.QDialog):
    """Click-through detail: status, kind, Request IDs, full timeline, force-defend, + Open on GitHub."""
    defended = QtCore.pyqtSignal()        # tell the parent to refresh the board

    def __init__(self, repo, num, state, parent=None):
        super().__init__(parent)
        self.repo, self.num, self.state = repo, num, state
        self._url = f"https://github.com/{repo}/issues/{num}"
        self.setWindowTitle(f"Issue #{num}")
        self.resize(580, 560)
        if os.path.exists(cs.ICON):
            self.setWindowIcon(QtGui.QIcon(cs.ICON))
        v = QtWidgets.QVBoxLayout(self)
        self.hdr = QtWidgets.QLabel("Loading…")
        self.hdr.setObjectName("brand")
        self.hdr.setWordWrap(True)
        v.addWidget(self.hdr)
        self.meta = QtWidgets.QLabel("")
        self.meta.setObjectName("subtle")
        self.meta.setWordWrap(True)
        self.meta.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.meta)
        v.addWidget(QtWidgets.QLabel("Timeline"))
        self.tl = QtWidgets.QListWidget()
        v.addWidget(self.tl, 1)
        row = QtWidgets.QHBoxLayout()
        self.btn_defend = QtWidgets.QPushButton("🛡 Defend (not a duplicate)")
        self.btn_defend.setToolTip("Force-post the 👎 + 'not a duplicate' note on this issue (live)")
        self.btn_defend.setEnabled(False)
        self.btn_defend.clicked.connect(self._force_defend)
        row.addWidget(self.btn_defend)
        row.addStretch(1)
        self.btn_open = QtWidgets.QPushButton("Open on GitHub ↗")
        self.btn_open.setObjectName("primary")
        self.btn_open.clicked.connect(
            lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(self._url)))
        row.addWidget(self.btn_open)
        v.addLayout(row)
        self._load()

    def _load(self):
        self.btn_defend.setEnabled(False)
        self.btn_defend.setText("🛡 Defend (not a duplicate)")
        self._f = IssueDetailFetcher(self.repo, self.num)
        self._f.fetched.connect(self._fill)
        self._f.start()

    def _fill(self, d):
        self._url = d.get("url") or self._url
        badge = "🟢 OPEN" if d.get("state") == "open" else f"🟣 CLOSED ({d.get('reason') or '—'})"
        self.hdr.setText(f"#{d['num']}  ·  {badge}\n{d.get('title', '')}")
        reqs = d.get("reqs") or []
        self.meta.setText(f"Kind: <b>{d.get('kind') or '—'}</b> &nbsp;·&nbsp; "
                          f"Request IDs: {', '.join(reqs) if reqs else '—'}")
        self.tl.clear()
        for at, icon, text in d.get("events", []):
            self.tl.addItem(f"{icon}  {fmt_ts(at)}  —  {text}")
        if not d.get("events"):
            self.tl.addItem("No timeline events found.")
        if d.get("defended"):
            self.btn_defend.setEnabled(False)
            self.btn_defend.setText("🛡 Defended ✓")
        else:
            self.btn_defend.setEnabled(True)
            self.btn_defend.setText("🛡 Defend (not a duplicate)")

    def _force_defend(self):
        self.btn_defend.setEnabled(False)
        self.btn_defend.setText("🛡 Defending…")
        self._dw = DedupWorker(self.state, self.repo, self.num)
        self._dw.done.connect(self._on_defended)
        self._dw.start()

    def _on_defended(self, num, ok):
        self.defended.emit()       # refresh the board's 👎✓ markers
        self._load()               # re-fetch -> timeline shows the new 'defended' event, button -> ✓


# --------------------------------- main window --------------------------------
class Main(QtWidgets.QMainWindow):
    COLS = ["", "Issue", "Author", "Created", "Title"]

    def __init__(self, repo, interval, auto, backfill, backfill_interval, backfill_max):
        super().__init__()
        self.repo, self.state = repo, cs.load_state()
        self.findings, self.community, self.me = {}, [], ""
        self.setWindowTitle(f"ClAudit v{cs.__version__} — false-positive blocks")
        self.resize(880, 460)
        if os.path.exists(cs.ICON):
            self.setWindowIcon(QtGui.QIcon(cs.ICON))

        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setWordWrap(False)
        self.table.doubleClicked.connect(self._show_detail)   # double-click a row -> full detail
        self.table.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._row_menu)
        self.empty = QtWidgets.QLabel("🔎  Loading false-positive issues…",
                                      self.table.viewport())
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color:#6b7280; font-size:15px; background:transparent;")

        self.f_scope = QtWidgets.QComboBox()
        self.f_scope.addItems(["All issues", "Mine only"])
        self.f_state = QtWidgets.QComboBox()
        self.f_state.addItems(["Open + Closed", "Open only", "Closed only"])
        self.f_kind = QtWidgets.QComboBox()
        self.f_kind.addItems(["All kinds", "cyber", "aup", "harness"])
        self.f_dedup = QtWidgets.QComboBox()
        self.f_dedup.addItems(["Any", "Defended", "Not defended"])
        self.f_search = QtWidgets.QLineEdit()
        self.f_search.setPlaceholderText("Filter by title or #number…")
        self.f_search.setClearButtonEnabled(True)
        for w in (self.f_scope, self.f_state, self.f_kind, self.f_dedup):
            w.currentIndexChanged.connect(self._repopulate)
        self.f_search.textChanged.connect(self._repopulate)
        filt = QtWidgets.QHBoxLayout()
        filt.addWidget(QtWidgets.QLabel("Show:"))
        filt.addWidget(self.f_scope)
        filt.addWidget(self.f_state)
        filt.addWidget(self.f_kind)
        filt.addWidget(self.f_dedup)
        filt.addWidget(self.f_search, 1)

        self.status = QtWidgets.QLabel("Loading…")
        btn_refresh = QtWidgets.QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh)
        self.btn_report = QtWidgets.QPushButton("Report pending")
        self.btn_report.setObjectName("primary")
        self.btn_report.clicked.connect(self.report_pending)

        self.bf_label = QtWidgets.QLabel("Backfill: —")
        self.bf_label.setObjectName("subtle")
        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self.status, 1)
        bar.addWidget(self.bf_label)
        bar.addWidget(self.btn_report)
        bar.addWidget(btn_refresh)
        header = make_banner()
        header.setMinimumHeight(56)
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(14, 9, 14, 9)
        logo = QtWidgets.QLabel()
        if os.path.exists(cs.ICON):
            logo.setPixmap(QtGui.QIcon(cs.ICON).pixmap(28, 28))
        brand = QtWidgets.QLabel("ClAudit")
        brand.setObjectName("brandg")
        _c = git_commit()
        sub = QtWidgets.QLabel(f"v{cs.__version__}{(' · ' + _c) if _c else ''}")
        sub.setObjectName("subg")
        hl.addWidget(logo)
        hl.addSpacing(8)
        hl.addWidget(brand)
        hl.addSpacing(8)
        hl.addWidget(sub)
        hl.addStretch(1)
        self.stats_bar = QtWidgets.QLabel("")
        self.stats_bar.setObjectName("statsbar")
        self.stats_bar.setTextFormat(QtCore.Qt.TextFormat.RichText)
        hl.addWidget(self.stats_bar)

        self.bf_bar = QtWidgets.QProgressBar()
        self.bf_bar.setObjectName("bf")
        self.bf_bar.setTextVisible(True)
        self.bf_bar.setMinimumHeight(24)
        self.btn_dedup = QtWidgets.QPushButton("👎 Not a dupe")
        self.btn_dedup.setToolTip("On the selected issue, 👎 the dup-bot + post a 'not a duplicate' note (live)")
        self.btn_dedup.clicked.connect(self._dedup_selected)
        bar.insertWidget(2, self.btn_dedup)
        self.btn_defend = QtWidgets.QPushButton("🛡 Defend all")
        self.btn_defend.setToolTip("👎 + 'not a duplicate' note on EVERY dup-bot-flagged open issue "
                                   "(idempotent, paced, live)")
        self.btn_defend.clicked.connect(self._defend_all)
        bar.insertWidget(3, self.btn_defend)

        board = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(board)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.bf_bar)
        bl.addLayout(filt)
        bl.addWidget(self.table, 1)
        bl.addLayout(bar)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(board, "Issues")
        self.tabs.addTab(self._build_stats_tab(), "Project")
        self.tabs.addTab(self._build_activity_tab(), "Activity")
        self.tabs.currentChanged.connect(
            lambda i: (self._fetch_stats(), self._fetch_poll()) if i == 1 else None)

        root = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(root)
        lay.addWidget(header)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

        self._build_tray(auto, backfill)
        self.watcher = Watcher(self.state, repo, interval, auto, backfill, backfill_interval, backfill_max)
        self.watcher.acted.connect(self._on_acted)
        self.watcher.start()
        self.bf_timer = QtCore.QTimer(self)
        self.bf_timer.timeout.connect(self._update_bf)
        self.bf_timer.start(1000)
        self.board_timer = QtCore.QTimer(self)        # refresh the board as backfill posts
        self.board_timer.timeout.connect(self.refresh)
        self.board_timer.start(45000)
        # self-restart on a REAL update (a new commit / git pull) — not on every local edit
        self._head = git_commit()
        self.update_timer = QtCore.QTimer(self)
        self.update_timer.timeout.connect(self._check_updates)
        self.update_timer.start(180000)        # every 3 min: fetch GitHub + ff-pull if behind
        self._fetch_stats()
        self._fetch_poll()
        self.refresh()

    def _check_updates(self):
        # fetch + ff-pull from GitHub off the UI thread; restart if HEAD moved
        self._uc = UpdateChecker(self._head)
        self._uc.updated.connect(self._restart)
        self._uc.start()

    def _restart(self):
        self.tray.showMessage("ClAudit", "Update detected — restarting with the new version…")
        if self.watcher:
            self.watcher.stop()
            self.watcher.wait(2000)
        cs._release_singleton()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _update_bf(self):
        w = self.watcher
        filed = sum(1 for s, r in self.state.items() if not s.startswith("__") and r.get("issue"))
        backlog = cs.backlog_size(self.state)
        total = filed + backlog
        self.bf_bar.setMaximum(max(total, 1))
        self.bf_bar.setValue(filed)
        if not w or not w.backfill:
            self.bf_bar.setFormat(f"Backfill OFF · {filed}/{total} reported")
            self.bf_label.setText("")
            return
        if backlog == 0:
            self.bf_bar.setFormat(f"Backfill DONE · all {filed} reported")
            self.bf_label.setText("")
            return
        nxt = max(0, w.bf_delay - (time.monotonic() - w.last_bf))
        self.bf_bar.setFormat(f"Backfilling  {filed}/{total}  ·  {backlog} left  ·  "
                              f"next post in {nxt:.0f}s  (~{w.bf_delay:.0f}s each)")
        self.bf_label.setText("")

    # ---- tray ----
    def _build_tray(self, auto, backfill):
        self.tray = QtWidgets.QSystemTrayIcon(self)
        self.tray.setIcon(QtGui.QIcon(cs.ICON) if os.path.exists(cs.ICON)
                          else self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_MessageBoxWarning))
        self.tray.setToolTip("ClAudit watcher")
        menu = QtWidgets.QMenu()
        self.act_pending = menu.addAction("Report 0 pending")
        self.act_pending.triggered.connect(self.report_pending)
        self.act_auto = menu.addAction("Auto-post new blocks")
        self.act_auto.setCheckable(True)
        self.act_auto.setChecked(auto)
        self.act_auto.toggled.connect(self._toggle_auto)
        self.act_backfill = menu.addAction("Backfill old blocks (slow drip)")
        self.act_backfill.setCheckable(True)
        self.act_backfill.setChecked(backfill)
        self.act_backfill.toggled.connect(self._toggle_backfill)
        self.act_defend = menu.addAction("Auto-defend dup-bot flags")
        self.act_defend.setCheckable(True)
        self.act_defend.setChecked(True)   # Watcher defaults defend=True
        self.act_defend.toggled.connect(self._toggle_defend)
        self.act_reopen = menu.addAction("Auto-reopen dup-bot closes")
        self.act_reopen.setCheckable(True)
        self.act_reopen.setChecked(False)  # opt-in: reopening closes is aggressive
        self.act_reopen.toggled.connect(self._toggle_reopen)
        self.act_llm = menu.addAction("Claude PII scrubbing")
        self.act_llm.setCheckable(True)
        self.act_llm.setChecked(claudit.LLM_SCRUB)
        self.act_llm.toggled.connect(self._toggle_llm)
        menu.addSeparator()
        menu.addAction("🔒 Edit PII denylist…", self._edit_scrub)
        menu.addAction("Show window", self.showNormal)
        menu.addAction("Refresh", self.refresh)
        menu.addAction("Open repo issues",
                       lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(
                           f"https://github.com/{self.repo}/issues?q=is:issue+author:@me")))
        menu.addSeparator()
        menu.addAction("Quit", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda r: self.setVisible(not self.isVisible())
                                    if r == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    def _edit_scrub(self):
        ScrubListDialog(self).exec()

    def _toggle_auto(self, on):
        if not self.watcher:
            return
        self.watcher.auto = on
        self.tray.showMessage("ClAudit", "Auto-post ENABLED — new blocks file automatically."
                              if on else "Auto-post disabled — blocks queue for review.")

    def _toggle_llm(self, on):
        claudit.LLM_SCRUB = on
        cfg = cs.load_config()
        cfg["llm_scrub"] = on
        cs.save_config(cfg)
        self.tray.showMessage("ClAudit", f"Claude PII scrubbing {'ON' if on else 'OFF'} (saved).")

    def _toggle_backfill(self, on):
        if not self.watcher:
            return
        self.watcher.backfill = on
        self.tray.showMessage("ClAudit", f"Backfill ENABLED — drip-filing old backlog "
                              f"(1 / {self.watcher.backfill_interval:g} min)." if on
                              else "Backfill paused.")

    def _toggle_defend(self, on):
        if not self.watcher:
            return
        self.watcher.defend = on
        if on:
            self.watcher.last_defend = 0.0   # sweep on the next tick
        self.tray.showMessage("ClAudit", "Auto-defend ENABLED — every dup-bot flag gets 👎 + a "
                              "'not a duplicate' note automatically." if on
                              else "Auto-defend paused.")

    def _toggle_reopen(self, on):
        if not self.watcher:
            return
        self.watcher.reopen = on
        if on:
            self.watcher.last_reopen = 0.0   # sweep on the next tick
        self.tray.showMessage("ClAudit", "Auto-reopen ENABLED — issues the dup-bot CLOSED as "
                              "duplicates get reopened (your own closes are left alone)." if on
                              else "Auto-reopen paused.")

    # ---- project stats tab ----
    def _build_poll_panel(self):
        box = QtWidgets.QGroupBox("🔮 Will Anthropic fix it?  —  community vote")
        g = QtWidgets.QVBoxLayout(box)
        self.poll_total = QtWidgets.QLabel("Loading the vote…")
        self.poll_total.setObjectName("subtle")
        self.poll_total.setWordWrap(True)
        g.addWidget(self.poll_total)
        row = QtWidgets.QHBoxLayout()
        self.poll_btns = {}
        for key, _content, emoji, meaning in cs.POLL_OPTS:
            b = QtWidgets.QPushButton(f"{emoji} {meaning}\n—")
            b.setMinimumHeight(48)
            b.clicked.connect(lambda _, k=key: self._cast_vote(k))
            self.poll_btns[key] = b
            row.addWidget(b)
        g.addLayout(row)
        return box

    def _fetch_poll(self):
        self._pw = PollWorker()
        self._pw.done.connect(self._on_poll)
        self._pw.start()

    def _cast_vote(self, key):
        for b in self.poll_btns.values():
            b.setEnabled(False)
        self.poll_total.setText("Casting your vote on the pinned issue…")
        self._pw = PollWorker(vote=key)
        self._pw.done.connect(self._on_poll)
        self._pw.start()

    def _on_poll(self, c):
        for b in self.poll_btns.values():
            b.setEnabled(True)
        total = c.get("total", 0)
        t = total or 1
        for key, _content, emoji, meaning in cs.POLL_OPTS:
            n = c.get(key, 0)
            self.poll_btns[key].setText(f"{emoji} {meaning}\n{round(100 * n / t)}%  ·  {n}")
        self.poll_total.setText(
            f"{total} vote(s) · click an option to cast or switch your vote "
            "(one 👍/👎/👀 reaction on the pinned issue)")

    def _build_activity_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addWidget(QtWidgets.QLabel("Everything the watcher does — filed, defended, reopened, "
                                     "closures detected — newest first."))
        self.activity = QtWidgets.QListWidget()
        v.addWidget(self.activity, 1)
        b = QtWidgets.QPushButton("Clear")
        b.clicked.connect(lambda: self.activity.clear())
        v.addWidget(b)
        return w

    def _log(self, msg):
        """Prepend a timestamped line to the in-app activity feed (capped at 300)."""
        if not hasattr(self, "activity"):
            return
        ts = datetime.datetime.now().astimezone().strftime("%H:%M:%S")
        self.activity.insertItem(0, f"{ts}   {msg}")
        while self.activity.count() > 300:
            self.activity.takeItem(self.activity.count() - 1)

    def _build_stats_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
        v.addWidget(self._build_poll_panel())
        charts = QtWidgets.QHBoxLayout()
        if _HAVE_SVG:
            box = QtWidgets.QGroupBox("📈 Reports over time")
            bl = QtWidgets.QVBoxLayout(box)
            self.trend_svg = QSvgWidget()
            self.trend_svg.setMinimumHeight(180)
            bl.addWidget(self.trend_svg)
            charts.addWidget(box, 3)
        bbox = QtWidgets.QGroupBox("📊 Breakdown")
        bbl = QtWidgets.QVBoxLayout(bbox)
        self.breakdown = BreakdownBars()
        bbl.addWidget(self.breakdown)
        charts.addWidget(bbox, 2)
        v.addLayout(charts)
        self.stats_summary = QtWidgets.QLabel("Loading project stats…")
        self.stats_summary.setObjectName("brand")
        self.stats_summary.setWordWrap(True)
        v.addWidget(self.stats_summary)
        cols = QtWidgets.QHBoxLayout()
        self.lst_stars = QtWidgets.QListWidget()
        self.lst_followers = QtWidgets.QListWidget()
        for title, lst in (("⭐ Stargazers", self.lst_stars), ("👥 Your followers", self.lst_followers)):
            c = QtWidgets.QVBoxLayout()
            c.addWidget(QtWidgets.QLabel(title))
            c.addWidget(lst)
            cw = QtWidgets.QWidget()
            cw.setLayout(c)
            cols.addWidget(cw)
        v.addLayout(cols, 1)
        legend = QtWidgets.QLabel(
            "Stargazers by recency: <span style='color:#3fb950'>■ today</span> &nbsp;"
            "<span style='color:#5eead4'>■ this week</span> &nbsp;"
            "<span style='color:#4aa3ff'>■ this month</span> &nbsp;"
            "<span style='color:#a371f7'>■ this quarter</span> &nbsp;"
            "<span style='color:#6b7280'>■ older</span>")
        legend.setObjectName("subtle")
        v.addWidget(legend)
        brow = QtWidgets.QHBoxLayout()
        b = QtWidgets.QPushButton("Refresh stats")
        b.clicked.connect(self._fetch_stats)
        bscrub = QtWidgets.QPushButton("🔒 Edit PII denylist…")
        bscrub.setToolTip("Manage the local scrub.txt — names/orgs/hostnames redacted from every report")
        bscrub.clicked.connect(self._edit_scrub)
        brow.addWidget(b)
        brow.addWidget(bscrub)
        brow.addStretch(1)
        v.addLayout(brow)
        return w

    def _fetch_stats(self):
        f = RepoStatsFetcher(cs.PROJECT_URL.split("github.com/")[-1])
        f.fetched.connect(self._on_stats)
        f.start()
        self._sf = f
        self._load_trend()

    def _load_trend(self):
        """Render the reports-over-time chart in the Project tab: the committed history plus a live
        point from the current board, drawn with the same renderer the README uses."""
        if not (_HAVE_SVG and hasattr(self, "trend_svg")):
            return
        try:
            hist = []
            hp = os.path.join(REPO_DIR, "docs", "counter-history.json")
            if os.path.exists(hp):
                hist = json.load(open(hp))
            if self.community:                       # append a current point from the board
                total = len(self.community)
                nopen = sum(1 for it in self.community if it.get("state", "").lower() == "open")
                when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                hist = list(hist) + [{"t": when, "open": nopen, "total": total,
                                      "closed": max(0, total - nopen)}]
            if _rp is not None and hist:
                svg = _rp.render_trend_svg(hist).encode()
            else:                                    # fallback: the committed SVG
                sp = os.path.join(REPO_DIR, "docs", "trend.svg")
                svg = open(sp, "rb").read() if os.path.exists(sp) else b""
            if svg:
                self.trend_svg.load(QtCore.QByteArray(svg))
        except Exception as e:
            print("trend load failed:", e, file=sys.stderr)

    def _on_stats(self, d):
        o = d.get("owner", {}) or {}
        self.stats_summary.setText(
            f"⭐ {d.get('stars', 0)} stars  ·  🍴 {d.get('forks', 0)} forks  ·  👁 {d.get('watchers', 0)} watchers"
            f"  ·  👥 {o.get('followers', 0)} followers  ·  📦 {o.get('public_repos', '?')} repos")
        self.lst_stars.clear()
        now = datetime.datetime.now(datetime.timezone.utc)
        # newest stars first, color-coded by recency
        gazers = sorted(d.get("stargazers", []), key=lambda s: s.get("at") or "", reverse=True)
        for s in gazers:
            at = s.get("at") or ""
            item = QtWidgets.QListWidgetItem(f"⭐ {s.get('login', '?')}   {at[:10]}")
            try:
                days = (now - datetime.datetime.fromisoformat(at.replace("Z", "+00:00"))).days
                color = ("#3fb950" if days <= 1 else "#5eead4" if days <= 7 else
                         "#4aa3ff" if days <= 30 else "#a371f7" if days <= 90 else "#6b7280")
                item.setForeground(QtGui.QColor(color))
                item.setToolTip(f"starred {days}d ago")
            except Exception:
                pass
            self.lst_stars.addItem(item)
        if not gazers:
            self.lst_stars.addItem("(no stars yet — be the first!)")
        self.lst_followers.clear()
        for fl in d.get("followers", []):
            self.lst_followers.addItem(f"👤 {fl}")

    # ---- manual per-issue dedup ----
    def _dedup_selected(self):
        row = self.table.currentRow()
        item = self.table.item(row, 1) if row >= 0 else None
        num = (item.text().lstrip("#").split()[0] if item else "")
        if not num.isdigit():
            QtWidgets.QMessageBox.information(self, "ClAudit", "Select one of your own issue rows (a #number) first.")
            return
        if QtWidgets.QMessageBox.question(
                self, "Not a duplicate",
                f"👎 the dup-bot and post a 'not a duplicate' note on #{num}?\n"
                "This is a live action on the public repo, made under your account.") \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.btn_dedup.setEnabled(False)
        self._dw = DedupWorker(self.state, self.repo, int(num))
        self._dw.done.connect(self._on_deduped)
        self._dw.start()

    def _on_deduped(self, num, ok):
        self.btn_dedup.setEnabled(True)
        self._log(f"{'🛡 defended' if ok else '⚠ defend failed on'} #{num}")
        icon = (QtGui.QIcon(cs.ICON) if os.path.exists(cs.ICON)
                else QtWidgets.QSystemTrayIcon.MessageIcon.Information)
        if ok:
            self.tray.showMessage(
                "ClAudit · 👎 dedup posted",
                f"Posted 👎 on the dup-bot + a 'not a duplicate' note on issue "
                f"{self.repo}#{num}.", icon)
        else:
            self.tray.showMessage(
                "ClAudit · dedup NOT posted",
                f"The 👎 did not land on #{num} (no dup-bot comment, or the reaction "
                "failed). Check the logs.", icon)
        self.refresh()

    def _defend_all(self):
        if QtWidgets.QMessageBox.question(
                self, "Defend all flagged issues",
                f"👎 the dup-bot + post a 'not a duplicate' note on EVERY open issue on {self.repo} "
                "that it flagged?\n\nThis is a live, bulk action under your account. It's paced and "
                "idempotent (already-defended issues are skipped, no double-posts).") \
                != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.btn_defend.setEnabled(False)
        self.btn_defend.setText("🛡 Defending…")
        self._defw = DefendAllWorker(self.state, self.repo)
        self._defw.progress.connect(self._on_defend_one)
        self._defw.finished_n.connect(self._on_defend_done)
        self._defw.start()

    def _on_defend_one(self, num, ok):
        self.btn_defend.setText(f"🛡 Defending #{num}…")

    def _on_defend_done(self, n):
        self.btn_defend.setEnabled(True)
        self.btn_defend.setText("🛡 Defend all")
        icon = (QtGui.QIcon(cs.ICON) if os.path.exists(cs.ICON)
                else QtWidgets.QSystemTrayIcon.MessageIcon.Information)
        self.tray.showMessage("ClAudit · 🛡 dedup defended",
                              f"Defended {n} flagged issue(s) on {self.repo} (👎 + 'not a duplicate')."
                              if n else "No new flagged issues to defend — all caught up.", icon)
        self._log(f"🛡 defend-all: defended {n} flagged issue(s)" if n
                  else "🛡 defend-all: nothing to do")
        self.refresh()

    # ---- data ----
    def refresh(self):
        threading.Thread(target=self._scan_then, daemon=True).start()
        f = CommunityFetcher(self.repo)
        f.fetched.connect(self._on_community)
        f.start()
        self._fetcher = f  # keep ref

    def _scan_then(self):
        with STATE_LOCK:
            self.findings = cs.scan()[0]
        QtCore.QMetaObject.invokeMethod(self, "_repopulate", QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.pyqtSlot()
    def _repopulate(self):
        DOT = {"open": "#3fb950", "closed": "#a371f7", "queued": "#d29922"}
        scope = self.f_scope.currentText()
        statef = self.f_state.currentText()
        kindf = self.f_kind.currentText()
        dedupf = self.f_dedup.currentText()
        needle = self.f_search.text().strip().lstrip("#").lower()
        deduped = self.state.get("__deduped__", {}) or {}

        rows = []   # (sort_ts, state, issue_label, author, created, title, url)
        pend = set(cs.pending_sigs(self.state))
        for sig in pend:   # your queued-but-unfiled blocks always count as "yours"
            f = self.findings.get(sig)
            if not f:       # stale queued sig (aged out, or harness now log-only) — don't show "[?]"
                continue
            kind = f.get("kind", "?")
            snippet = (cs.scrub(f.get("prompt", ""))[0])[:80]
            title = f"[{kind}] {snippet}"
            if statef != "Closed only" and (not needle or needle in title.lower()):
                rows.append(("9999", "queued", "QUEUED", "you", "—", title, "", ""))

        for it in self.community:
            st = it.get("state", "").lower()
            author = (it.get("author") or {}).get("login", "—")
            title = it.get("title", "")
            if scope == "Mine only" and self.me and author != self.me:
                continue
            if statef == "Open only" and st != "open":
                continue
            if statef == "Closed only" and st != "closed":
                continue
            if kindf != "All kinds" and f"[{kindf}]" not in title.lower():
                continue
            is_defended = deduped.get(str(it["number"])) == "not-duplicate"
            if dedupf == "Defended" and not is_defended:
                continue
            if dedupf == "Not defended" and is_defended:
                continue
            if needle and needle not in title.lower() and needle not in str(it.get("number", "")):
                continue
            created = fmt_ts(it.get("createdAt", ""))
            ded = (self.state.get("__deduped__", {}) or {}).get(str(it["number"]))
            reopened = (self.state.get("__reopened__", {}) or {}).get(str(it["number"]))
            label = f"#{it['number']}" + (" 👎✓" if ded == "not-duplicate" else "")
            why = ""
            if st == "closed":
                reason = (it.get("stateReason") or "").lower()
                why = {"not_planned": "closed: not planned (often = duplicate)",
                       "duplicate": "closed as DUPLICATE — not actually a dupe",
                       "completed": "closed: completed"}.get(reason, f"closed ({reason or '—'})")
                if reopened and not str(reopened).startswith("review"):
                    why += " · ♻ reopened by ClAudit"
            rows.append((it.get("createdAt", ""), st, label, author, created, title,
                         it.get("url", ""), why))

        rows.sort(key=lambda r: r[0], reverse=True)   # newest first

        self.table.setRowCount(len(rows))
        for r, (_, st, num, author, created, title, url, why) in enumerate(rows):
            is_claudit = "claudit" in title.lower() or title.lower().startswith(("[cyber]", "[aup]", "[bug]"))
            if author == "you" or (self.me and author == self.me):
                owner = "#b794f6"          # yours = purple
            elif is_claudit:
                owner = "#5eead4"          # another ClAudit user = teal
            else:
                owner = None
            tip = why or ("Click to open in browser" if url else "Not filed yet")
            for c, val in enumerate(["●", num, author, created, title]):
                item = QtWidgets.QTableWidgetItem(val)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, url)
                item.setToolTip(tip)
                if c == 0:
                    item.setForeground(QtGui.QColor(DOT.get(st, "#6b7280")))
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                elif owner:
                    item.setForeground(QtGui.QColor(owner))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.table.setColumnWidth(4, max(340, self.table.columnWidth(4)))
        self.empty.setGeometry(self.table.viewport().rect())
        self.empty.setText("No issues match this filter.")
        self.empty.setVisible(len(rows) == 0)
        nopen = sum(1 for it in self.community if it.get("state", "").lower() == "open")
        mine = sum(1 for it in self.community if (it.get("author") or {}).get("login") == self.me)
        self.status.setText(
            f"{len(self.community)} issues · {nopen} open · showing {len(rows)} &nbsp;|&nbsp; "
            f"<span style='color:#b794f6'>■ yours ({mine})</span> &nbsp; "
            f"<span style='color:#5eead4'>■ other ClAudit</span>")
        self.btn_report.setEnabled(len(pend) > 0)
        self.act_pending.setText(f"Report {len(pend)} pending")
        self.act_pending.setEnabled(len(pend) > 0)
        self._update_stats_bar(nopen)

    def _update_stats_bar(self, nopen):
        if not hasattr(self, "stats_bar"):
            return
        c = self.community
        nclosed = len(c) - nopen
        kinds = {"cyber": 0, "aup": 0, "harness": 0}
        for it in c:
            t = (it.get("title", "") or "").lower()
            for k in kinds:
                if f"[{k}]" in t:
                    kinds[k] += 1
                    break
        deduped = self.state.get("__deduped__", {}) or {}
        defended = sum(1 for v in deduped.values() if v == "not-duplicate")
        reopened = sum(1 for v in (self.state.get("__reopened__", {}) or {}).values()
                       if not str(v).startswith("review"))
        today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        nday = sum(1 for it in c if (it.get("createdAt", "") or "")[:10] == today)
        self.stats_bar.setText(
            f"<span style='color:#3fb950'>● {nopen} open</span> &nbsp; "
            f"<span style='color:#a371f7'>● {nclosed} closed</span> &nbsp;|&nbsp; "
            f"cyber {kinds['cyber']} · aup {kinds['aup']} · harness {kinds['harness']} &nbsp;|&nbsp; "
            f"🛡 {defended} &nbsp; ♻ {reopened} &nbsp; "
            f"<span style='color:#5eead4'>+{nday} today</span>")
        if hasattr(self, "breakdown"):
            self.breakdown.set_data([
                ("open", nopen, "#3fb950"),
                ("closed", nclosed, "#a371f7"),
                ("cyber", kinds["cyber"], "#4aa3ff"),
                ("aup", kinds["aup"], "#d29922"),
                ("harness", kinds["harness"], "#8a5a5a"),
                ("defended", defended, "#5eead4"),
            ])

    def _on_community(self, items, me):
        self.community, self.me = items, me
        self._repopulate()

    def _show_detail(self, idx):
        self._open_detail_num(self._row_num(idx.row()))

    def _row_num(self, row):
        item = self.table.item(row, 1) if row >= 0 else None
        num = (item.text().lstrip("#").split()[0] if item else "")
        return int(num) if num.isdigit() else 0

    def _open_detail_num(self, num):
        if not num:
            return
        dlg = IssueDetailDialog(self.repo, num, self.state, self)
        dlg.defended.connect(self.refresh)
        dlg.exec()

    def _row_menu(self, pos):
        num = self._row_num(self.table.indexAt(pos).row())
        if not num:
            return
        m = QtWidgets.QMenu(self)
        m.addAction("🔍 Details", lambda: self._open_detail_num(num))
        m.addAction("🛡 Defend (not a duplicate)", lambda: self._quick_defend(num))
        m.addAction("♻ Reopen (if closed)", lambda: self._quick_reopen(num))
        m.addSeparator()
        m.addAction("↗ Open on GitHub", lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl(f"https://github.com/{self.repo}/issues/{num}")))
        m.exec(self.table.viewport().mapToGlobal(pos))

    def _quick_defend(self, num):
        self._log(f"🛡 defending #{num}…")
        self._dwq = DedupWorker(self.state, self.repo, num)
        self._dwq.done.connect(self._on_deduped)
        self._dwq.start()

    def _quick_reopen(self, num):
        self._log(f"♻ reopening #{num}…")
        self._rwq = ReopenOneWorker(self.repo, num)
        self._rwq.done.connect(lambda nn, ok: (
            self._log(f"♻ reopened #{nn}" if ok else f"♻ #{nn}: already open / not reopenable"),
            self.refresh()))
        self._rwq.start()

    def _on_acted(self, n, kind):
        icon = (QtGui.QIcon(cs.ICON) if os.path.exists(cs.ICON)
                else QtWidgets.QSystemTrayIcon.MessageIcon.Information)
        if kind == "backfill":   # historical: from your backlog, not just-happened
            self.tray.setToolTip("ClAudit — backfilling (historical)")
            self.tray.showMessage("ClAudit · 📦 HISTORICAL",
                                  f"Backfilled {n} block(s) from your backlog.", icon)
            self._log(f"📦 backfilled {n} historical block(s)")
            return   # board_timer handles the list refresh (no per-drip refetch)
        if kind == "defend":     # auto-defended dup-bot flags
            self.tray.showMessage("ClAudit · 🛡 DEFENDED",
                                  f"Auto-defended {n} dup-bot-flagged issue(s) (👎 + note).", icon)
            self._log(f"🛡 auto-defended {n} dup-bot-flagged issue(s)")
            return
        if kind == "reopen":     # reopened dup-bot-closed issues
            self.tray.showMessage("ClAudit · ♻ REOPENED",
                                  f"Reopened {n} issue(s) the dup-bot wrongly closed as duplicate.", icon)
            self._log(f"♻ reopened {n} dup-bot-closed issue(s)")
            return
        if kind == "pruned":     # stale backlog reconciled at startup — just refresh the count
            self._update_bf()
            return
        if kind == "auto":       # live: a block that just happened
            self.tray.setToolTip("ClAudit — watching live")
            self.tray.showMessage("ClAudit · 🔴 LIVE",
                                  f"Reported {n} block(s) the moment it happened — {self.repo}.", icon)
            self._log(f"🔴 filed {n} LIVE block(s)")
        else:
            self.tray.showMessage("ClAudit", f"{n} new block(s) queued — use ‘Report pending’.", icon)
            self._log(f"📥 queued {n} new block(s)")
        self.refresh()

    def report_pending(self):
        if not cs.pending_sigs(self.state):
            return
        if QtWidgets.QMessageBox.question(
                self, "Report pending",
                f"File {len(cs.pending_sigs(self.state))} block(s) to {self.repo}?\n"
                "These are public GitHub issues.") != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.btn_report.setEnabled(False)
        self.reporter = Reporter(self.state, self.repo)
        self.reporter.done.connect(lambda n: (self.tray.showMessage("ClAudit", f"Reported {n} issue(s)."),
                                              self.refresh()))
        self.reporter.start()

    def closeEvent(self, e):     # close to tray, keep running
        e.ignore()
        self.hide()
        self.tray.showMessage("ClAudit", "Still running in the tray.")

    def _quit(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher.wait(1500)
        QtWidgets.QApplication.quit()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("-R", "--repo", default=cs.DEFAULT_REPO)
    p.add_argument("--auto", action="store_true", help="auto-file new blocks (default: queue for review)")
    p.add_argument("--backfill", action="store_true", help="slowly drip-file the baselined backlog")
    p.add_argument("--backfill-interval", dest="backfill_interval", type=float, default=10,
                   help="starting seconds between backfilled issues; auto-adapts to GitHub limits (default 10)")
    p.add_argument("--backfill-max", dest="backfill_max", type=int, default=0,
                   help="stop backfilling after N issues (0 = no cap)")
    p.add_argument("--llm-scrub", dest="llm_scrub", action="store_true",
                   help="force Claude-assisted PII scrubbing on (skip the startup prompt)")
    p.add_argument("--burn-tokens", dest="burn_tokens", action="store_true",
                   help="bespoke LLM-written titles/bodies — the strongest PII defense")
    p.add_argument("--hidden", action="store_true", help="start minimized to tray")
    args = p.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)
    if os.path.exists(cs.ICON):
        app.setWindowIcon(QtGui.QIcon(cs.ICON))   # ClAudit icon on every window + modal titlebar

    # Claude-assisted PII scrubbing: CLI flag > saved choice > ask at startup (with "remember").
    cfg = cs.load_config()
    if args.llm_scrub:
        claudit.LLM_SCRUB = True
    elif "llm_scrub" in cfg:
        claudit.LLM_SCRUB = bool(cfg["llm_scrub"])
    else:
        box = QtWidgets.QMessageBox(QtWidgets.QMessageBox.Icon.Question, "ClAudit — PII scrubbing",
                                    "Enable Claude-assisted PII scrubbing?\n\nUses the `claude` CLI to catch "
                                    "names, org abbreviations, and hostnames the regex can't (slower, uses "
                                    "tokens). Strongly recommended before posting publicly.")
        if os.path.exists(cs.ICON):
            box.setIconPixmap(QtGui.QIcon(cs.ICON).pixmap(56, 56))
        remember = QtWidgets.QCheckBox("Remember my choice")
        box.setCheckBox(remember)
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
        box.setDefaultButton(QtWidgets.QMessageBox.StandardButton.Yes)
        on = box.exec() == QtWidgets.QMessageBox.StandardButton.Yes
        claudit.LLM_SCRUB = on
        if remember.isChecked():
            cfg["llm_scrub"] = on
            cs.save_config(cfg)
    if args.burn_tokens or cfg.get("burn_tokens"):
        claudit.BURN_TOKENS = claudit.LLM_SCRUB = True   # bespoke LLM reports need the LLM
    if not cs.acquire_singleton():
        QtWidgets.QMessageBox.warning(None, "ClAudit",
                                      "Another ClAudit watcher is already running.\nThis instance will exit.")
        return
    w = Main(args.repo, args.interval, args.auto, args.backfill, args.backfill_interval,
             args.backfill_max)
    if not args.hidden:
        w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
