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


def _snap(d):
    """Shallow-copy a state sub-dict the watcher thread may be mutating, before the GUI iterates it.
    The watcher holds STATE_LOCK during long network/LLM work, so the GUI never blocks on it; instead
    it copies defensively and tolerates the rare 'dict changed size' mid-copy by retrying."""
    for _ in range(5):
        try:
            return dict(d) if d else {}
        except RuntimeError:
            continue
    return {}


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


def _code_changed(a, b):
    """True if any run-affecting file (.py / deps) changed between commits a and b. The counter bot
    pushes docs/counter/poll/trend + README refreshes every couple hours; those must NOT trigger a
    restart. Unsure -> True (restart to be safe)."""
    try:
        out = _git("diff", "--name-only", a, b, timeout=10).stdout
    except Exception:
        return True
    return any(f.strip().endswith(".py") or f.strip() in ("requirements.txt", "pyproject.toml")
               for f in out.splitlines())


class UpdateChecker(QtCore.QThread):
    """Off-thread: pull new commits from GitHub (if clean+behind), then flag if CODE moved. A pull
    that only refreshes docs/counter/poll/trend updates the checkout but does not restart the app."""
    updated = QtCore.pyqtSignal()

    def __init__(self, launch_head):
        super().__init__()
        self.launch_head = launch_head

    def run(self):
        git_pull_if_behind()                 # auto-update from GitHub (stays current either way)
        cur = git_commit()
        if cur and self.launch_head and cur != self.launch_head and _code_changed(self.launch_head, cur):
            self.updated.emit()              # restart only when real code changed


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
        x0, pad = 180, 6   # wide label gutter: "harness withdrawn (false)" / "closed (Anthropic)"
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


# kind -> (color, lane label), in back-to-front lane order for the chrono-line
KIND_VIZ = {
    "cyber":      ("#4aa3ff", "cyber FP"),
    "aup":        ("#d29922", "AUP FP"),
    "harness":    ("#8a5a5a", "harness"),
    "limit":      ("#f0883e", "rate limit"),
    "overloaded": ("#a371f7", "overloaded"),
    "other":      ("#5b6472", "other"),
}
LANE_ORDER = ["cyber", "aup", "harness", "limit", "overloaded", "other"]


def _iso_epoch(ts):
    """ISO-8601 (with trailing Z) -> POSIX seconds; 0.0 if unparseable."""
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


SPAWN_DUR = 0.85       # seconds a point takes to grow in
INTRO_SPAN = 1.9       # stagger window for the first-load build-in (oldest -> newest)


def _ease_out_back(x):
    """Overshoot easing: a point grows past full size then settles, giving a little 'pop'."""
    c1 = 1.70158
    return 1 + (c1 + 1) * (x - 1) ** 3 + c1 * (x - 1) ** 2


def chain_color(key):
    """Stable, vivid colour per work-session chain (deterministic hue from the key). Shared by the
    3D chart threads and the list's chain-graph gutter so a chain is the same colour in both."""
    v = 0
    for ch in str(key):
        v = (v * 131 + ord(ch)) & 0xffffffff
    return QtGui.QColor.fromHsv(v % 360, 150, 235)


class ChainGraphDelegate(QtWidgets.QStyledItemDelegate):
    """Paints column 0 as a git-graph gutter: each work-session chain gets a vertical colour lane,
    members are nodes on it, and the line between members shows the link. One project = one lane, so
    every edge is a clean vertical — no diagonal routing."""
    LANE_W = 12

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = {}        # row -> {"node_lane", "node_color", "lanes": [(lane,color,up,down,isnode)]}
        self.lane_count = 1

    def set_data(self, rows, lane_count):
        self.rows = rows
        self.lane_count = lane_count

    def paint(self, painter, option, index):
        super().paint(painter, option, index)        # selection/background only (cell text is empty)
        if index.column() != 0:
            return
        g = self.rows.get(index.row())
        if not g:
            return
        rect = option.rect
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        ymid = rect.center().y() + 0.5

        def lx(lane):
            return rect.left() + 9 + lane * self.LANE_W

        for lane, color, up, down, _isnode in g["lanes"]:
            painter.setPen(QtGui.QPen(QtGui.QColor(color), 2.0))
            x = lx(lane)
            if up:
                painter.drawLine(QtCore.QPointF(x, rect.top()), QtCore.QPointF(x, ymid))
            if down:
                painter.drawLine(QtCore.QPointF(x, ymid), QtCore.QPointF(x, rect.bottom()))
        nx = lx(g["node_lane"])
        painter.setPen(QtCore.Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QColor(g["node_color"]))
        painter.drawEllipse(QtCore.QPointF(nx, ymid), 4.0, 4.0)
        painter.restore()


class ChronoLine(QtWidgets.QWidget):
    """Pseudo-3D timeline of every filed ClAudit issue. Pure QPainter perspective projection — NO
    OpenGL, so it cannot segfault on any GL stack. Each kind is a depth lane; issues plot along a
    tilted time axis. New issues grow in place with a ripple as they post; the first view builds in
    oldest -> newest. Hover a point for its number, author, time, and title; your own issues are
    ringed and the newest carries a reticle. Drag to rotate, wheel to zoom, and the Cinematic button
    flies the camera along each lane in turn. Emits openIssue(url) on double-click."""
    openIssue = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(380)
        self.setMouseTracking(True)          # hover hit-testing needs moves with no button held
        self.items = []                      # [{epoch,kind,author,title,num,state,url,mine}] by epoch
        self.lanes = []                      # kinds actually present, back-to-front
        self.tmin = self.tmax = 0.0
        self.HOME = (0.45, 0.50, 1.0)        # the legible default angle the camera eases back to
        self.yaw, self.pitch, self.zoom = self.HOME
        self._drag = None
        self._auto = False                   # still by default: a tab left open must not burn CPU
        self._fly = False
        self._flt = 0.0                      # cinematic-tour clock (seconds)
        self._last_act = 0.0                 # monotonic of last user input (for idle auto-recenter)
        self._bg = 0.0                        # abstract-background animation phase
        self._bgpix = None                    # cached gradient+nebula (costly); refreshed as it drifts
        self._bgpix_t = -99.0
        self._frame = 0
        self.hover = -1
        self._pts = []                       # [(sx, sy, idx, r)] cached each paint for hit-testing
        self._spawn = {}                     # num -> (start_monotonic, is_new_post) for grow-in anim
        self._known = set()                  # issue numbers already seen (new-post detection)
        self._primed = False
        self._intro_played = False
        # deterministic starfield for the abstract background (no Math.random needed)
        self._stars = [((i * 73 % 1000) / 1000.0, (i * 37 % 1000) / 1000.0,
                        1.0 + (i * 13 % 4) * 0.5, 0.15 + (i % 6) * 0.06, (i * 0.7) % 6.28)
                       for i in range(72)]
        self._f_lane = QtGui.QFont(); self._f_lane.setPointSize(8)   # reused each frame, not realloc'd
        self._f_ui = QtGui.QFont(); self._f_ui.setPointSize(9)
        self._f_new = QtGui.QFont(); self._f_new.setPointSize(8); self._f_new.setBold(True)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)                # 20fps active; ~5fps idle for the ambient background

    # ---- data ----
    def set_items(self, items):
        self.items = sorted(items, key=lambda d: d["epoch"])
        present = {d["kind"] for d in self.items}
        self.lanes = [k for k in LANE_ORDER if k in present] or ["other"]
        if self.items:
            self.tmin = self.items[0]["epoch"]
            self.tmax = self.items[-1]["epoch"]
        now = time.monotonic()
        nums = {d["num"] for d in self.items if d.get("num") is not None}
        if not self._primed:
            self._known = set(nums)          # first load: baseline, don't flag everything as 'new'
            self._primed = bool(nums)
        else:
            for d in self.items:             # genuine new posts -> pop in place with a ripple + NEW
                n = d.get("num")
                if n is not None and n not in self._known:
                    self._spawn[n] = (now, True)
            self._known |= nums
        if self.items and not self._intro_played and self.isVisible():
            self._start_intro(now)
        self.hover = -1
        self.update()

    def _start_intro(self, now):
        """First time the chart is seen: stagger every point's grow-in oldest -> newest."""
        self._intro_played = True
        n = len(self.items)
        for rank, d in enumerate(self.items):
            num = d.get("num")
            if num is not None:
                self._spawn[num] = (now + (rank / max(1, n - 1)) * INTRO_SPAN, False)

    # ---- animation ----
    def _tick(self):
        now = time.monotonic()
        self._bg += 0.05                     # abstract background drifts continuously, gently
        self._frame += 1
        if self._spawn:
            for num in [k for k, (s, _p) in self._spawn.items() if now - s > SPAWN_DUR]:
                del self._spawn[num]
        returning = False
        if not self._fly and not self._auto and self._drag is None and not self._spawn \
                and now - self._last_act > 2.5:
            returning = self._ease_home()    # idle: glide the camera back to the readable angle
        if self._fly:
            self._flt += 0.05
        elif self._auto and self._drag is None:
            self.yaw += 0.0022               # slow auto-orbit when untouched
        # the background drifts while the scene animates; when fully idle we stop repainting (0% CPU)
        if self._fly or self._auto or bool(self._spawn) or returning:
            self.update()

    def _ease_home(self):
        """Glide yaw/pitch/zoom back toward HOME by the shortest path. Returns True while moving."""
        hy, hp, hz = self.HOME
        dy = (self.yaw - hy + math.pi) % (2 * math.pi) - math.pi   # shortest angular path
        self.yaw = hy + dy
        if abs(dy) + abs(self.pitch - hp) + abs(self.zoom - hz) < 0.004:
            self.yaw, self.pitch, self.zoom = self.HOME
            return False
        self.yaw += (hy - self.yaw) * 0.10
        self.pitch += (hp - self.pitch) * 0.10
        self.zoom += (hz - self.zoom) * 0.10
        return True

    def showEvent(self, e):
        super().showEvent(e)
        if not self._timer.isActive():
            self._timer.start(50)
        if self.items and not self._intro_played:
            self._start_intro(time.monotonic())   # play the build-in the first time it's revealed
            self.update()

    def hideEvent(self, e):
        super().hideEvent(e)
        self._timer.stop()                   # don't spin while the tab/tray is hidden

    def set_fly(self, on):
        self._fly = bool(on)
        self._last_act = time.monotonic()
        if on:
            self._auto = False
            self._flt = 0.0                  # start the tour at the first lane
        self.update()

    # ---- camera ----
    def _camera(self):
        """(yaw, pitch, zoom, panx, panz). Cinematic mode flies along one lane at a time: it pans
        the focused lane across the centre (back and forth, lane after lane) so each row is read in
        turn. Off-mode returns the static base camera."""
        if not self._fly:
            return self.yaw, self.pitch, self.zoom, 0.0, 0.0
        t = self._flt
        n = len(self.lanes) or 1
        seg = 4.6                            # seconds spent flying along one lane
        i = int(t / seg)
        lane = i % n
        u = (t - i * seg) / seg              # 0..1 along this lane

        def lzf(idx):
            return -0.85 + 1.7 * idx / max(1, n - 1)

        forward = (i % 2 == 0)               # boustrophedon: L->R, then R->L, then L->R ...
        panx = (-1.15 + 2.3 * u) if forward else (1.15 - 2.3 * u)
        blend = min(1.0, u / 0.22)
        blend = blend * blend * (3 - 2 * blend)        # smoothstep the lane-to-lane glide
        panz = lzf((i - 1) % n) + (lzf(lane) - lzf((i - 1) % n)) * blend
        yaw = 0.36 + 0.05 * math.sin(t * 0.3)
        return yaw, 0.30, 1.55, panx, panz

    def _project(self, x, y, z, cam, cx, cy, scale):
        yaw, pitch, _zoom, panx, panz = cam
        x, z = x - panx, z - panz
        cyw, syw = math.cos(yaw), math.sin(yaw)
        xr, zr = x * cyw + z * syw, -x * syw + z * cyw
        cp, sp = math.cos(pitch), math.sin(pitch)
        yr, zr2 = y * cp - zr * sp, y * sp + zr * cp
        f = 3.2 / (3.2 + zr2)
        return cx + xr * f * scale, cy - yr * f * scale, f

    # ---- interaction ----
    def mousePressEvent(self, e):
        self._drag = (e.position().x(), e.position().y(), self.yaw, self.pitch)
        self._last_act = time.monotonic()

    def mouseMoveEvent(self, e):
        if self._drag is not None and (e.buttons() & QtCore.Qt.MouseButton.LeftButton):
            x0, y0, yaw0, pitch0 = self._drag
            self.yaw = yaw0 + (e.position().x() - x0) * 0.010
            self.pitch = max(-0.15, min(1.15, pitch0 + (e.position().y() - y0) * 0.008))
            self._fly = False                # taking the stick cancels fly mode
            self._last_act = time.monotonic()
            self.update()
            return
        mx, my = e.position().x(), e.position().y()
        best, bd = -1, 14.0 ** 2
        for sx, sy, idx, _r in self._pts:
            d = (sx - mx) ** 2 + (sy - my) ** 2
            if d < bd:
                bd, best = d, idx
        if best != self.hover:
            self.hover = best
            self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor if best >= 0
                           else QtCore.Qt.CursorShape.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, e):
        self._drag = None

    def leaveEvent(self, e):
        if self.hover != -1:
            self.hover = -1
            self.update()

    def wheelEvent(self, e):
        self.zoom = max(0.4, min(3.0, self.zoom * (1.0 + e.angleDelta().y() / 1200.0)))
        self._last_act = time.monotonic()
        self.update()

    def mouseDoubleClickEvent(self, e):
        if 0 <= self.hover < len(self.items):
            url = self.items[self.hover].get("url")
            if url:
                self.openIssue.emit(url)      # double-click a point -> open that issue
                return
        if self._fly:
            self._fly = False
        else:
            self._auto = not self._auto
        self.update()

    # ---- paint ----
    def _height(self, d, recency):
        """Y of a point: open issues ascend with recency; a real-action close lifts above the line;
        a dismissed/ignored close sinks to the floor."""
        c = d.get("closure", "open")
        if c == "done":
            return 0.10 + 0.34 * recency + 0.20
        if c == "dismissed":
            return 0.015
        return 0.06 + 0.34 * recency

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        self._draw_background(p, w, h)
        if not self.items:
            p.setPen(QtGui.QColor("#6b7280"))
            p.drawText(self.rect(), QtCore.Qt.AlignmentFlag.AlignCenter,
                       "No real cyber/AUP false positives yet — the timeline fills as ClAudit files them.")
            p.end()
            return
        cam = self._camera()
        cx, cy = w * 0.46, h * 0.60
        scale = min(w, h) * 0.38 * cam[2]
        span = max(1.0, self.tmax - self.tmin)
        nz = len(self.lanes)
        lane_i = {k: i for i, k in enumerate(self.lanes)}
        mono = time.monotonic()
        done_col, dismiss_col = QtGui.QColor("#3fb950"), QtGui.QColor("#5b6472")

        def tx(epoch):
            return -1.25 + 2.3 * (epoch - self.tmin) / span

        def lz(kind):
            return -0.7 + 1.4 * lane_i.get(kind, nz - 1) / max(1, nz - 1)

        def proj(x, y, z):
            return self._project(x, y, z, cam, cx, cy, scale)

        # lane rails + labels
        for k in self.lanes:
            z = lz(k)
            a, b = proj(-1.3, 0, z), proj(1.3, 0, z)
            p.setPen(QtGui.QPen(QtGui.QColor(40, 46, 58), 1))
            p.drawLine(QtCore.QPointF(a[0], a[1]), QtCore.QPointF(b[0], b[1]))
            col = QtGui.QColor(KIND_VIZ[k][0])
            p.setPen(QtGui.QColor(col.red(), col.green(), col.blue(), 170))
            p.setFont(self._f_lane)
            p.drawText(QtCore.QPointF(a[0] - 4, a[1] + 3), KIND_VIZ[k][1])

        # month gridlines, labels on the front rail
        zf, zb = lz(self.lanes[-1]), lz(self.lanes[0])
        t0 = datetime.datetime.fromtimestamp(self.tmin, tz=datetime.timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0)
        m = (t0.replace(day=28) + datetime.timedelta(days=8)).replace(day=1)
        for _ in range(24):
            ep = m.timestamp()
            if ep > self.tmax:
                break
            if ep >= self.tmin:
                x = tx(ep)
                a, b = proj(x, 0, zb), proj(x, 0, zf)
                p.setPen(QtGui.QPen(QtGui.QColor(30, 36, 46), 1))
                p.drawLine(QtCore.QPointF(a[0], a[1]), QtCore.QPointF(b[0], b[1]))
                p.setPen(QtGui.QColor("#6b7280"))
                p.drawText(QtCore.QPointF(b[0] - 10, b[1] + 16), m.strftime("%b %y"))
            m = (m.replace(day=28) + datetime.timedelta(days=8)).replace(day=1)

        # points: a stem from the rail up to each issue, height = age/closure; depth-sorted; cached
        newest = len(self.items) - 1
        self._pts = []
        drawn = []
        for i, d in enumerate(self.items):
            recency = (d["epoch"] - self.tmin) / span
            y = self._height(d, recency)
            z = lz(d["kind"])
            x = tx(d["epoch"])
            sx, sy, f = proj(x, y, z)
            fx, fy, _ff = proj(x, 0, z)
            drawn.append((f, sx, sy, fx, fy, i, d))

        # chain threads: link issues from the same work session into a visible string (weaving across
        # lanes). Hovering any point lights its whole chain. Drawn under the dots.
        by_chain = {}
        for _f, sx, sy, _fx, _fy, _i, d in drawn:
            if d.get("chain"):
                by_chain.setdefault(d["chain"], []).append((d["epoch"], sx, sy))
        hover_chain = (self.items[self.hover].get("chain")
                       if 0 <= self.hover < len(self.items) else None)
        for ck, pts in by_chain.items():
            if len(pts) < 2:
                continue
            hot = (ck == hover_chain)
            if not hot and len(pts) > 20:    # giant chains fan into noise at rest; show them on hover
                continue
            pts.sort(key=lambda t: t[0])
            cc = self._chain_color(ck)
            p.setPen(QtGui.QPen(QtGui.QColor(cc.red(), cc.green(), cc.blue(), 225 if hot else 32),
                                2.4 if hot else 1.0))
            path = QtGui.QPainterPath()
            path.moveTo(pts[0][1], pts[0][2])
            for _e, px, py in pts[1:]:
                path.lineTo(px, py)
            p.drawPath(path)

        drawn.sort(key=lambda t: t[0])       # far (small depth factor) first
        for f, sx, sy, fx, fy, i, d in drawn:
            grow, ripple, is_post = 1.0, None, False
            sp = self._spawn.get(d.get("num"))
            if sp is not None:
                el = mono - sp[0]
                if el < 0:
                    continue                 # intro: this point has not grown in yet (skip + no hit)
                if el < SPAWN_DUR:
                    ripple = el / SPAWN_DUR
                    grow = _ease_out_back(ripple)
                    is_post = sp[1]
            closure = d.get("closure", "open")
            col = (done_col if closure == "done" else
                   dismiss_col if closure == "dismissed" else QtGui.QColor(KIND_VIZ[d["kind"]][0]))
            mine, hovered = d["mine"], (i == self.hover)
            fog = max(0.4, min(1.0, (f - 0.5) / 0.8))           # atmospheric depth: far = dimmer
            r = max(1.8, 3.3 * f) * (1.8 if hovered else 1.0) * (1.2 if mine else 1.0) * grow
            # stem from the rail up to the point (the ascending-height read)
            sa = int((90 if closure == "open" else 60) * fog)
            p.setPen(QtGui.QPen(QtGui.QColor(col.red(), col.green(), col.blue(), sa), 1.0))
            p.drawLine(QtCore.QPointF(fx, fy), QtCore.QPointF(sx, sy))
            if ripple is not None:                              # expanding spawn ring
                rr = r + ripple * (34 if is_post else 18)
                p.setPen(QtGui.QPen(QtGui.QColor(col.red(), col.green(), col.blue(),
                                                 int((1 - ripple) * (190 if is_post else 110))), 1.6))
                p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
                p.drawEllipse(QtCore.QPointF(sx, sy), rr, rr)
            if hovered or ripple is not None or closure == "done":   # glow only where it earns it
                gr = r * (4 if (hovered or ripple is not None) else 3)
                ga = 170 if ripple is not None else (150 if hovered else 110)
                g = QtGui.QRadialGradient(sx, sy, gr)
                g.setColorAt(0.0, QtGui.QColor(col.red(), col.green(), col.blue(), ga))
                g.setColorAt(1.0, QtGui.QColor(col.red(), col.green(), col.blue(), 0))
                p.setBrush(QtGui.QBrush(g)); p.setPen(QtCore.Qt.PenStyle.NoPen)
                p.drawEllipse(QtCore.QPointF(sx, sy), gr, gr)
            base_a = 255 if (hovered or closure == "open") else (220 if closure == "done" else 140)
            p.setBrush(QtGui.QColor(col.red(), col.green(), col.blue(), int(base_a * fog)))
            if mine and r >= 2.4:            # your issues: white ring (skip on tiny far dots: invisible + costly)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, int((200 if hovered else 130) * fog)), 1.3))
            else:
                p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawEllipse(QtCore.QPointF(sx, sy), r, r)
            if closure == "done":                               # real action: a ✓ above the point
                p.setFont(self._f_new); p.setPen(QtGui.QColor(180, 255, 200, int(230 * fog)))
                p.drawText(QtCore.QPointF(sx - 4, sy - r - 4), "✓")
            elif closure == "dismissed":                        # ignored/dismissed: a faint ✕
                p.setFont(self._f_lane); p.setPen(QtGui.QColor(150, 160, 175, int(150 * fog)))
                p.drawText(QtCore.QPointF(sx - 3, sy - r - 3), "✕")
            if i == newest and ripple is None:
                self._draw_reticle(p, sx, sy, r, col)
            if is_post and ripple is not None:                  # 'NEW' tag that fades and rises
                p.setFont(self._f_new)
                p.setPen(QtGui.QColor(255, 255, 255, int((1 - ripple) * 255)))
                p.drawText(QtCore.QPointF(sx - 11, sy - r - 8 - ripple * 7), "NEW")
            self._pts.append((sx, sy, i, r))

        if 0 <= self.hover < len(self.items):
            self._draw_dropline(p, proj, tx, lz)
            self._draw_tooltip(p, w, h)

        # footer
        p.setPen(QtGui.QColor("#8b94a3"))
        p.setFont(self._f_ui)
        mode = "FLY" if self._fly else ("orbit" if self._auto else "live")
        opn = sum(1 for d in self.items if d.get("closure", "open") == "open")
        p.drawText(12, h - 12, f"{len(self.items)} real false positives · {opn} open · {mode} · "
                                "height = age · ✓ fixed · ✕ dismissed · threads = work-session chains "
                                "(hover lights one)")
        p.end()

    def _draw_background(self, p, w, h):
        """Abstract drifting backdrop: deep gradient + two slow nebula blobs (cached to a pixmap and
        refreshed only as they drift — the per-pixel radial fills are far too costly every frame),
        plus a live parallax starfield (cheap)."""
        t = self._bg
        if (self._bgpix is None or self._bgpix.width() != w or self._bgpix.height() != h
                or abs(t - self._bgpix_t) > 0.25):
            pix = QtGui.QPixmap(w, h)
            pp = QtGui.QPainter(pix)
            pp.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            grad = QtGui.QLinearGradient(0, 0, w, h)
            grad.setColorAt(0.0, QtGui.QColor(15, 17, 27))
            grad.setColorAt(0.55, QtGui.QColor(10, 12, 19))
            grad.setColorAt(1.0, QtGui.QColor(7, 8, 13))
            pp.fillRect(0, 0, w, h, grad)
            for hue, x0, y0, sp, rad in (((96, 70, 150), 0.30, 0.34, 0.11, 0.55),
                                         ((150, 110, 60), 0.74, 0.64, 0.08, 0.60)):
                cxp = w * (x0 + 0.10 * math.sin(t * sp))
                cyp = h * (y0 + 0.10 * math.cos(t * sp * 0.8))
                rr = min(w, h) * rad
                g = QtGui.QRadialGradient(cxp, cyp, rr)
                g.setColorAt(0.0, QtGui.QColor(hue[0], hue[1], hue[2], 30))
                g.setColorAt(1.0, QtGui.QColor(hue[0], hue[1], hue[2], 0))
                pp.setBrush(QtGui.QBrush(g)); pp.setPen(QtCore.Qt.PenStyle.NoPen)
                pp.drawEllipse(QtCore.QPointF(cxp, cyp), rr, rr)
            pp.end()
            self._bgpix, self._bgpix_t = pix, t
        p.drawPixmap(0, 0, self._bgpix)
        for fx, fy, sz, sp, ph in self._stars:
            x = (fx + t * sp * 0.02) % 1.0
            a = 40 + int(45 * (0.5 + 0.5 * math.sin(t * 0.8 + ph)))
            p.setBrush(QtGui.QColor(150, 165, 200, a)); p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.drawEllipse(QtCore.QPointF(x * w, fy * h), sz, sz)

    def _chain_color(self, key):
        return chain_color(key)              # shared with the list gutter so colours match

    def _draw_reticle(self, p, sx, sy, r, col):
        """Static marker on the newest issue so the latest is findable without animation."""
        rr = r + 5
        p.setPen(QtGui.QPen(QtGui.QColor(col.red(), col.green(), col.blue(), 150), 1.0))
        p.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        p.drawEllipse(QtCore.QPointF(sx, sy), rr, rr)
        for dx, dy in ((rr + 3, 0), (-rr - 3, 0), (0, rr + 3), (0, -rr - 3)):
            p.drawPoint(QtCore.QPointF(sx + dx, sy + dy))

    def _draw_dropline(self, p, proj, tx, lz):
        """Drop a faint plumb line from the hovered point to its lane rail, labelled with the date."""
        d = self.items[self.hover]
        sx = sy = None
        for px, py, idx, _r in self._pts:
            if idx == self.hover:
                sx, sy = px, py
                break
        if sx is None:
            return
        rx, ry, _f = proj(tx(d["epoch"]), 0, lz(d["kind"]))
        p.setPen(QtGui.QPen(QtGui.QColor(150, 160, 175, 110), 1, QtCore.Qt.PenStyle.DashLine))
        p.drawLine(QtCore.QPointF(sx, sy), QtCore.QPointF(rx, ry))
        p.setPen(QtGui.QColor("#aeb6c2"))
        p.setFont(self._f_lane)
        p.drawText(QtCore.QPointF(rx - 18, ry + 14),
                   datetime.datetime.fromtimestamp(d["epoch"]).astimezone().strftime("%b %d"))

    def _draw_tooltip(self, p, w, h):
        d = self.items[self.hover]
        col = QtGui.QColor(KIND_VIZ[d["kind"]][0])
        when = datetime.datetime.fromtimestamp(d["epoch"]).astimezone().strftime("%Y-%m-%d %H:%M")
        num = f"#{d['num']}" if d.get("num") else "(queued)"
        cstate = {"open": "open", "done": "fixed ✓", "dismissed": "dismissed ✕"}.get(
            d.get("closure", "open"), d.get("state", "?"))
        rows = [f"{num} · [{d['kind']}] · {cstate}",
                f"by {d['author']}" + ("  · you" if d["mine"] else ""),
                when]
        words, line = d["title"].split(), ""
        for wd in words:                     # wrap the title to <=46 chars, max 3 lines
            if len(line) + len(wd) + 1 > 46:
                rows.append(line); line = wd
                if len(rows) >= 6:
                    break
            else:
                line = (line + " " + wd).strip()
        if line and len(rows) < 7:
            rows.append(line)
        p.setFont(self._f_ui)
        fm = QtGui.QFontMetrics(self._f_ui)
        tw = max(fm.horizontalAdvance(r) for r in rows) + 20
        th = len(rows) * (fm.height() + 2) + 12
        sx = sy = 0
        for px, py, idx, _r in self._pts:
            if idx == self.hover:
                sx, sy = px, py
                break
        bx = min(max(8, sx + 14), w - tw - 8)
        by = min(max(8, sy - th - 10), h - th - 8)
        panel = QtGui.QPainterPath()
        panel.addRoundedRect(QtCore.QRectF(bx, by, tw, th), 8, 8)
        p.fillPath(panel, QtGui.QColor(18, 22, 30, 242))
        p.setPen(QtGui.QPen(QtGui.QColor(col.red(), col.green(), col.blue(), 210), 1.2))
        p.drawPath(panel)
        y = by + fm.ascent() + 7
        for i, row in enumerate(rows):
            if i == 0:
                p.setPen(col.lighter(125))
            elif i == 1:
                p.setPen(QtGui.QColor("#e6e8ec") if d["mine"] else QtGui.QColor("#aeb6c2"))
            elif i == 2:
                p.setPen(QtGui.QColor("#8b94a3"))
            else:
                p.setPen(QtGui.QColor("#cdd3dc"))
            p.drawText(int(bx + 10), int(y), row)
            y += fm.height() + 2


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
        self.dwell = False                 # dwell auto-file: hold new Request IDs, LLM-judge+compose,
                                           # then file each as its own linked bespoke issue (opt-in)
        self.reopen = False                # auto-reopen dup-bot-CLOSED issues; opt-in (off by default)
        self._transient_mark = ""          # newest overloaded/rate-limit ts we've alerted on
        self.bf_done = 0
        self.last_live = 0.0
        self.last_bf = 0.0
        self.last_defend = 0.0
        self.last_reopen = 0.0
        self.last_transient = 0.0
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
                        if self.dwell:
                            n = cs.dwell_cycle(self.state, self.repo, 0, lambda *a: None)
                        elif self.auto:
                            n = cs.auto_cycle(self.state, self.repo, 0, lambda *a: None)
                        else:
                            n = cs.monitor_cycle(self.state, lambda fresh: None)
                    if n:
                        self.acted.emit(n, "dwell" if self.dwell else ("auto" if self.auto else "queued"))
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
            # RATE-LIMIT ALERT: a NEW overloaded/rate-limit error means a session got throttled.
            # Toast it (informational only; ClAudit never auto-types into your session).
            if now - self.last_transient >= 12:
                self.last_transient = now
                try:
                    ts = cs.newest_transient_ts()
                    if ts and ts > self._transient_mark:
                        first = self._transient_mark == ""
                        self._transient_mark = ts            # prime on first run; don't alert old ones
                        if not first:
                            self.acted.emit(0, "ratelimit")
                except Exception as e:
                    print("rate-limit check error:", e, file=sys.stderr)
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


class NotifyWatcher(QtCore.QThread):
    """Poll GitHub notifications for new comments / @mentions on the ClAudit-relevant repos. Public
    activity, so anyone running the GUI sees the engagement on issues they take part in."""
    got = QtCore.pyqtSignal(list)
    REPOS = {"anthropics/claude-code", "sworrl/ClAudit"}

    def run(self):
        items = []
        try:
            j = json.loads(subprocess.run(
                ["gh", "api", "notifications", "--jq",
                 "[.[] | {id:.id, reason:.reason, title:.subject.title, type:.subject.type, "
                 "url:.subject.url, repo:.repository.full_name}]"],
                capture_output=True, text=True, timeout=30).stdout or "[]")
            items = [n for n in j if n.get("repo") in self.REPOS]
        except Exception as e:
            print("notify fetch failed:", e, file=sys.stderr)
        self.got.emit(items)


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
        self._chain_delegate = ChainGraphDelegate(self.table)   # column-0 chain-link gutter
        self.table.setItemDelegateForColumn(0, self._chain_delegate)
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
        self.f_kind.addItems(["All kinds", "cyber", "aup"])   # harness is excluded from the list
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
        self.watcher.dwell = bool(cs.load_config().get("dwell_autofile"))   # opt-in dwell auto-filer
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
        self._seen_notifs = set()
        self._notif_primed = False             # first poll seeds 'seen' (no toast flood of old items)
        self.notif_timer = QtCore.QTimer(self)
        self.notif_timer.timeout.connect(self._poll_notifs)
        self.notif_timer.start(150000)         # every 2.5 min: new comments / @mentions
        self._poll_notifs()
        self._fetch_stats()
        self._fetch_poll()
        self.refresh()

    def _poll_notifs(self):
        self._nw = NotifyWatcher()
        self._nw.got.connect(self._on_notifs)
        self._nw.start()

    def _on_notifs(self, items):
        new = [n for n in items if n.get("id") not in self._seen_notifs]
        for n in items:
            self._seen_notifs.add(n.get("id"))
        if not self._notif_primed:             # first run: seed seen, don't toast old unread
            self._notif_primed = True
            return
        icon = (QtGui.QIcon(cs.ICON) if os.path.exists(cs.ICON)
                else QtWidgets.QSystemTrayIcon.MessageIcon.Information)
        for n in new[:5]:                      # cap a burst
            reason = (n.get("reason") or "activity").replace("_", " ")
            repo = n.get("repo", "")
            title = (n.get("title") or "")[:70]
            self.tray.showMessage(f"ClAudit · 💬 {reason}",
                                  f"{repo}\n{title}", icon)
            self._log(f"💬 {reason} · {repo}: {title}")

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
        self.act_dwell = menu.addAction("Dwell auto-file (LLM judge + 15-min batch + cross-link)")
        self.act_dwell.setCheckable(True)
        self.act_dwell.setChecked(bool(cs.load_config().get("dwell_autofile")))
        self.act_dwell.toggled.connect(self._toggle_dwell)
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

    def _toggle_dwell(self, on):
        if not self.watcher:
            return
        self.watcher.dwell = on
        if on:                              # the dwell filer needs the LLM to judge + compose
            cs.GATE = claudit.BURN_TOKENS = claudit.LLM_SCRUB = True
            self.act_llm.setChecked(True)
        cfg = cs.load_config()
        cfg["dwell_autofile"] = on
        cs.save_config(cfg)
        mins = cs.DWELL_SECONDS // 60
        self.tray.showMessage("ClAudit", f"Dwell auto-file ENABLED — new blocks wait ~{mins} min, the "
                              "LLM judges + writes each, then files one linked bespoke issue per Request "
                              "ID. No manual push." if on else "Dwell auto-file disabled.")

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
        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)

        top = QtWidgets.QWidget()
        tl = QtWidgets.QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        head = QtWidgets.QHBoxLayout()
        head.addWidget(QtWidgets.QLabel("Real false positives over time (cyber + AUP). Height = age; ✓ = fixed "
                                        "(real action), ✕ = dismissed/ignored. New ones grow in as they post."))
        head.addStretch(1)
        self.fly_btn = QtWidgets.QPushButton("🎬 Cinematic")
        self.fly_btn.setCheckable(True)
        self.fly_btn.setToolTip("Fly the camera along each lane in turn, reading every row")
        self.fly_btn.toggled.connect(lambda on: self.chrono.set_fly(on))
        head.addWidget(self.fly_btn)
        tl.addLayout(head)
        self.chrono = ChronoLine()
        self.chrono.openIssue.connect(lambda url: QtGui.QDesktopServices.openUrl(QtCore.QUrl(url)))
        tl.addWidget(self.chrono, 1)
        split.addWidget(top)

        bot = QtWidgets.QWidget()
        bl = QtWidgets.QVBoxLayout(bot)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(QtWidgets.QLabel("Watcher activity — filed, defended, reopened, closures — newest first."))
        self.activity = QtWidgets.QListWidget()
        bl.addWidget(self.activity, 1)
        b = QtWidgets.QPushButton("Clear")
        b.clicked.connect(lambda: self.activity.clear())
        bl.addWidget(b)
        split.addWidget(bot)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        v.addWidget(split)
        self._load_chrono()
        return w

    def _load_chrono(self):
        """Feed the chrono-line from the filed community issues. ONLY real cyber/AUP API false
        positives — the harness (auto-mode-classifier) reports are excluded. Each point carries its
        closure: open, done (closed COMPLETED = real action), or dismissed (closed any other way)."""
        if not hasattr(self, "chrono"):
            return
        chain_of = self._chain_map()         # issue number -> work-session chain key
        items = []
        for it in self.community:
            ep = _iso_epoch(it.get("createdAt", ""))
            if not ep:
                continue
            title = it.get("title", "") or ""
            tl = title.lower()
            kind = next((k for k in ("cyber", "aup") if f"[{k}]" in tl), None)
            if kind is None:                 # skip harness/other: not real server-side false positives
                continue
            state = (it.get("state", "") or "").lower()
            reason = (it.get("stateReason", "") or "").upper()
            closure = "open" if state == "open" else ("done" if reason == "COMPLETED" else "dismissed")
            author = (it.get("author") or {}).get("login", "") or "?"
            items.append({
                "epoch": ep, "kind": kind, "author": author, "title": title,
                "num": it.get("number"), "state": state, "closure": closure,
                "url": it.get("url", ""), "mine": author == self.me,
                "chain": chain_of.get(str(it.get("number"))),
            })
        self.chrono.set_items(items)

    def _chain_map(self):
        """issue number -> chain key (the work session it belongs to). From the local issues DB
        (every ClAudit-filed issue records its project) plus the authoritative dwell chains."""
        chain = {}
        try:
            with open(cs.ISSUES_DB, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    num, projs = str(rec.get("issue") or ""), rec.get("projects") or []
                    if num and projs:
                        chain[num] = projs[0]
        except OSError:
            pass
        for proj, nums in _snap(self.state.get("__proj_chain__")).items():
            for n in nums:
                chain[str(n)] = proj         # dwell chains are authoritative
        return chain

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
            if self.community:                       # per-kind current point from the board
                oa = ca = har = 0
                for it in self.community:
                    t = (it.get("title", "") or "").lower()
                    is_open = (it.get("state", "") or "").lower() == "open"
                    if "[harness]" in t:
                        har += 1
                    elif "[cyber]" in t or "[aup]" in t:
                        oa += is_open
                        ca += not is_open
                when = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                hist = list(hist) + [{"t": when, "open_api": oa, "closed_api": ca, "harness": har}]
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
            self.findings = cs.scan(ttl=20)[0]   # reuse the watcher's recent scan; don't re-walk per refresh
        QtCore.QMetaObject.invokeMethod(self, "_repopulate", QtCore.Qt.ConnectionType.QueuedConnection)

    def _build_chain_graph(self, rows, DOT):
        """Lay the visible rows into git-graph lanes: each multi-member chain gets one vertical lane,
        members are nodes, the line between them is the link. Returns (per-row paint data, lane count)."""
        groups = {}
        for i, row in enumerate(rows):
            if row[8]:
                groups.setdefault(row[8], []).append(i)
        multi = {ck: sorted(v) for ck, v in groups.items() if len(v) > 1}
        lanes, lane_end = {}, []                  # greedy interval packing, ordered by first member
        for ck in sorted(multi, key=lambda c: multi[c][0]):
            lo, hi = multi[ck][0], multi[ck][-1]
            lane = next((i for i, e in enumerate(lane_end) if e < lo), len(lane_end))
            if lane == len(lane_end):
                lane_end.append(hi)
            else:
                lane_end[lane] = hi
            lanes[ck] = lane
        seg, active = {}, {}
        for ck, mrows in multi.items():
            lane, col, mset = lanes[ck], chain_color(ck).name(), set(mrows)
            for r in range(mrows[0], mrows[-1] + 1):
                seg.setdefault(r, []).append((lane, col, r > mrows[0], r < mrows[-1], r in mset))
                active.setdefault(r, set()).add(lane)
        nlanes = len(lane_end)
        graph = {}
        for i, row in enumerate(rows):
            if row[8] in lanes:
                node_lane = lanes[row[8]]
            else:                                 # singleton/no-chain: a free lane so it dodges through-lines
                act = active.get(i, set())
                node_lane = next((L for L in range(nlanes + 1) if L not in act), nlanes)
            graph[i] = {"node_lane": node_lane, "node_color": DOT.get(row[1], "#6b7280"),
                        "lanes": seg.get(i, [])}
        return graph, nlanes

    @QtCore.pyqtSlot()
    def _repopulate(self):
        DOT = {"open": "#3fb950", "closed": "#a371f7", "queued": "#d29922", "dwelling": "#5eead4"}
        scope = self.f_scope.currentText()
        statef = self.f_state.currentText()
        kindf = self.f_kind.currentText()
        dedupf = self.f_dedup.currentText()
        needle = self.f_search.text().strip().lstrip("#").lower()
        deduped = self.state.get("__deduped__", {}) or {}
        chain_of = self._chain_map()    # issue number -> work-session chain key (for the graph gutter)

        rows = []   # (sort_ts, state, label, author, created, title, url, why, chain_key)
        pend = set(cs.pending_sigs(self.state))
        for sig in pend:   # your queued-but-unfiled blocks always count as "yours"
            f = self.findings.get(sig)
            if not f:       # stale queued sig (aged out, or harness now log-only) — don't show "[?]"
                continue
            kind = f.get("kind", "?")
            snippet = (cs.scrub(f.get("prompt", ""))[0])[:80]
            title = f"[{kind}] {snippet}"
            if statef != "Closed only" and (not needle or needle in title.lower()):
                rows.append(("9999", "queued", "QUEUED", "you", "—", title, "", "", None))

        # Dwelling: new Request IDs the dwell auto-filer is holding before it files them as their own
        # linked bespoke issues. Show the countdown + which chain (work session) each belongs to.
        hold = _snap(self.state.get("__dwell_hold__"))   # copy before iterating (watcher mutates it)
        if hold and statef != "Closed only":
            chains = _snap(self.state.get("__proj_chain__"))
            reqmap = {o["req"]: f for f in self.findings.values()
                      for o in f.get("occ", []) if o.get("req")}
            now = time.time()
            hold_proj = {}                               # proj -> count of dwelling reqs (the pending chain)
            for req in hold:
                fnd = reqmap.get(req)
                hold_proj[cs._proj_of(fnd) if fnd else "?"] = hold_proj.get(
                    cs._proj_of(fnd) if fnd else "?", 0) + 1
            for req, t0 in hold.items():
                fnd = reqmap.get(req)
                kind = fnd.get("kind", "?") if fnd else "?"
                snippet = (cs.scrub(fnd.get("prompt", ""))[0])[:70] if fnd else req
                title = f"[{kind}] {snippet}"
                if needle and needle not in title.lower() and needle not in req.lower():
                    continue
                mins = max(0, int((cs.DWELL_SECONDS - (now - t0)) // 60))
                proj = cs._proj_of(fnd) if fnd else "?"
                filed_sibs = chains.get(proj, [])
                pending_sibs = hold_proj.get(proj, 1)
                if filed_sibs:
                    chain = "🔗 chain: " + ", ".join(f"#{n}" for n in filed_sibs[-4:])
                    if pending_sibs > 1:
                        chain += f" (+{pending_sibs - 1} more dwelling)"
                elif pending_sibs > 1:
                    chain = f"🔗 new chain forming — {pending_sibs} dwelling together"
                else:
                    chain = "first in a new chain"
                rows.append(("9998", "dwelling", "⏳ DWELL", "you",
                             f"files in ~{mins}m", f"{title}  ·  {chain}", "", chain, proj))

        for it in self.community:
            st = it.get("state", "").lower()
            author = (it.get("author") or {}).get("login", "—")
            title = it.get("title", "")
            if "[harness]" in title.lower():
                continue                     # harness = withdrawn auto-classifier noise; never list it
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
                         it.get("url", ""), why, chain_of.get(str(it.get("number")))))

        rows.sort(key=lambda r: r[0], reverse=True)   # newest first
        graph, nlanes_ = self._build_chain_graph(rows, DOT)
        self._chain_delegate.set_data(graph, nlanes_)

        self.table.setRowCount(len(rows))
        for r, (_, _st, num, author, created, title, url, why, _chain) in enumerate(rows):
            is_claudit = "claudit" in title.lower() or title.lower().startswith(("[cyber]", "[aup]", "[bug]"))
            if author == "you" or (self.me and author == self.me):
                owner = "#b794f6"          # yours = purple
            elif is_claudit:
                owner = "#5eead4"          # another ClAudit user = teal
            else:
                owner = None
            tip = why or ("Click to open in browser" if url else "Not filed yet")
            for c, val in enumerate(["", num, author, created, title]):   # col 0 painted by the delegate
                item = QtWidgets.QTableWidgetItem(val)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, url)
                item.setToolTip(tip)
                if c != 0 and owner:
                    item.setForeground(QtGui.QColor(owner))
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        nlanes = self._chain_delegate.lane_count
        self.table.setColumnWidth(0, 16 + max(1, nlanes) * ChainGraphDelegate.LANE_W)
        self.table.setColumnWidth(4, max(340, self.table.columnWidth(4)))
        self.empty.setGeometry(self.table.viewport().rect())
        self.empty.setText("No issues match this filter.")
        self.empty.setVisible(len(rows) == 0)
        real = [it for it in self.community if "[harness]" not in (it.get("title", "") or "").lower()]
        nopen = sum(1 for it in real if it.get("state", "").lower() == "open")
        mine = sum(1 for it in real if (it.get("author") or {}).get("login") == self.me)
        self.status.setText(
            f"{len(real)} real false positives · {nopen} open · showing {len(rows)} &nbsp;|&nbsp; "
            f"<span style='color:#b794f6'>■ yours ({mine})</span> &nbsp; "
            f"<span style='color:#5eead4'>■ other ClAudit</span>")
        self.btn_report.setEnabled(len(pend) > 0)
        self.act_pending.setText(f"Report {len(pend)} pending")
        self.act_pending.setEnabled(len(pend) > 0)
        self._update_stats_bar(nopen)
        self._load_chrono()   # ERROR_LOG was just rewritten by the scan that triggered this repopulate

    def _update_stats_bar(self, nopen):
        if not hasattr(self, "stats_bar"):
            return
        c = self.community
        # split closures honestly: harness reports are ClAudit's OWN withdrawn false reports
        # (auto-mode-classifier, log-only), NOT issues Anthropic closed. Count them apart.
        kinds = {"cyber": 0, "aup": 0, "harness": 0}
        harness_withdrawn = 0   # closed harness = withdrawn false reports
        real_closed = 0         # closed cyber/aup/bespoke = actually closed by Anthropic
        for it in c:
            t = (it.get("title", "") or "").lower()
            closed = (it.get("state", "") or "").lower() == "closed"
            k = next((x for x in kinds if f"[{x}]" in t), None)
            if k:
                kinds[k] += 1
            if closed:
                if k == "harness":
                    harness_withdrawn += 1
                else:
                    real_closed += 1
        defended = sum(1 for v in _snap(self.state.get("__deduped__")).values() if v == "not-duplicate")
        reopened = sum(1 for v in _snap(self.state.get("__reopened__")).values()
                       if not str(v).startswith("review"))
        today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        nday = sum(1 for it in c if (it.get("createdAt", "") or "")[:10] == today)
        self.stats_bar.setText(
            f"<span style='color:#3fb950'>● {nopen} open</span> &nbsp; "
            f"<span style='color:#a371f7'>● {real_closed} closed by Anthropic</span> &nbsp;|&nbsp; "
            f"cyber {kinds['cyber']} · aup {kinds['aup']} &nbsp;|&nbsp; "
            f"<span style='color:#b58a8a'>⊘ {harness_withdrawn} harness withdrawn (false)</span> &nbsp;|&nbsp; "
            f"🛡 {defended} &nbsp; ♻ {reopened} &nbsp; "
            f"<span style='color:#5eead4'>+{nday} today</span>")
        self.stats_bar.setToolTip(
            f"{real_closed} cyber/aup reports actually closed by Anthropic.\n"
            f"{harness_withdrawn} harness (auto-mode-classifier) reports were ClAudit's own false "
            "reports, withdrawn as log-only — they do NOT count as Anthropic closing a ticket.")
        if hasattr(self, "breakdown"):
            self.breakdown.set_data([
                ("open", nopen, "#3fb950"),
                ("closed (Anthropic)", real_closed, "#a371f7"),
                ("cyber", kinds["cyber"], "#4aa3ff"),
                ("aup", kinds["aup"], "#d29922"),
                ("harness withdrawn (false)", harness_withdrawn, "#b58a8a"),
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
        if kind == "ratelimit":  # a session got throttled (transient, not your usage limit)
            self.tray.showMessage("ClAudit · ⏳ RATE LIMITED",
                                  "A session hit a server-side rate limit (transient, not your usage "
                                  "limit). It usually clears in seconds; press continue when it does.",
                                  icon)
            self._log("⏳ rate limit hit (transient)")
            return
        if kind == "reopen":     # reopened dup-bot-closed issues
            self.tray.showMessage("ClAudit · ♻ REOPENED",
                                  f"Reopened {n} issue(s) the dup-bot wrongly closed as duplicate.", icon)
            self._log(f"♻ reopened {n} dup-bot-closed issue(s)")
            return
        if kind == "pruned":     # stale backlog reconciled at startup — just refresh the count
            self._update_bf()
            return
        if kind == "dwell":      # dwell auto-filer: ripe Request IDs, LLM-judged, filed + cross-linked
            self.tray.setToolTip("ClAudit — dwell auto-filing")
            self.tray.showMessage("ClAudit · 🕒 DWELL FILED",
                                  f"Filed {n} bespoke report(s) after the dwell (one per Request ID, "
                                  f"cross-linked) — {self.repo}.", icon)
            self._log(f"🕒 dwell-filed {n} linked bespoke report(s)")
        elif kind == "auto":     # live: a block that just happened
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
    if cfg.get("dwell_autofile"):                        # dwell mode = LLM judges + composes each report
        cs.GATE = claudit.BURN_TOKENS = claudit.LLM_SCRUB = True
        if "dwell_seconds" in cfg:
            cs.DWELL_SECONDS = int(cfg["dwell_seconds"])
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
