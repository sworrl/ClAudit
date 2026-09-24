"""ClAudit GUI: widgets (split out of claudit_gui.py; see that file for the entry point)."""
import datetime
import math
import os
import re
import sys
import time
from PyQt6 import QtCore, QtGui, QtWidgets
from .common import INTRO_SPAN, KIND_VIZ, LANE_ORDER, SPAWN_DUR, _ease_out_back, chain_color


try:
    from PyQt6.QtOpenGL import (
        QOpenGLShader,
        QOpenGLShaderProgram,
        QOpenGLVertexArrayObject,
    )
    from PyQt6.QtOpenGLWidgets import QOpenGLWidget
    _HAVE_GL = True
except Exception:
    _HAVE_GL = False

if _HAVE_GL:
    class ShaderBanner(QOpenGLWidget):
        """Animated GLSL 'plasma' banner behind the header. Falls back to a solid dark fill if the
        GPU/driver can't give us a 3.2 core context or the shader won't compile — never crashes."""
        _VS = ("#version 150 core\nvoid main(){vec2 p=vec2((gl_VertexID==2)?3.0:-1.0,"
               "(gl_VertexID==1)?3.0:-1.0);gl_Position=vec4(p,0.0,1.0);}")
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
        self.fmt = str   # value formatter (e.g. lambda v: f"${v:.2f}" for the cost chart)

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
            p.drawText(int(x0 + track_w + 8), int(y), 52, int(bh),
                       QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter,
                       self.fmt(val))
        p.end()

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

class TitleChipDelegate(QtWidgets.QStyledItemDelegate):
    """Paints the Title column's leading [Bug][cyber]-style tags as coloured pill chips, then the
    rest of the title as normal (eliding to fit). Owner colouring is preserved from the item's
    foreground role."""
    CHIP = {"cyber": ("#0f2b45", "#4aa3ff"), "aup": ("#3a2c0d", "#d29922"),
            "harness": ("#33232d", "#b58a8a"), "bug": ("#262b36", "#8b94a3")}
    MODEL_CHIP = ("#2b1f45", "#b794f6")            # violet: which model's safeguards flagged it
    MODEL_ROLE = QtCore.Qt.ItemDataRole.UserRole + 3
    TAG_RE = re.compile(r"^\s*((?:\[[A-Za-z]+\])+)\s*(.*)$", re.DOTALL)

    def paint(self, painter, option, index):
        text = index.data() or ""
        m = self.TAG_RE.match(text)
        if not m:
            super().paint(painter, option, index)
            return
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""                                            # background/selection only
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        tags = [(t, self.CHIP.get(t.lower(), ("#262b36", "#8b94a3")))
                for t in re.findall(r"\[([A-Za-z]+)\]", m.group(1))]
        mdl = index.data(self.MODEL_ROLE)
        if mdl:
            tags.append((str(mdl), self.MODEL_CHIP))
        rest = m.group(2)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        f = QtGui.QFont(option.font)
        f.setPixelSize(11)
        f.setBold(True)
        fm = QtGui.QFontMetrics(f)
        x = option.rect.left() + 8
        cy = option.rect.center().y()
        for tag, (bg, fgc) in tags:
            wtx = fm.horizontalAdvance(tag) + 12
            r = QtCore.QRectF(x, cy - 8, wtx, 17)
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            painter.setBrush(QtGui.QColor(bg))
            painter.drawRoundedRect(r, 8, 8)
            painter.setFont(f)
            painter.setPen(QtGui.QColor(fgc))
            painter.drawText(r, QtCore.Qt.AlignmentFlag.AlignCenter, tag)
            x += wtx + 4
        fg = index.data(QtCore.Qt.ItemDataRole.ForegroundRole)
        painter.setFont(option.font)
        painter.setPen(fg.color() if isinstance(fg, QtGui.QBrush) else QtGui.QColor("#e6e8ec"))
        avail = option.rect.right() - 8 - (x + 4)
        painter.drawText(QtCore.QRectF(x + 4, option.rect.top(), max(avail, 10), option.rect.height()),
                         QtCore.Qt.AlignmentFlag.AlignVCenter,
                         QtGui.QFontMetrics(option.font).elidedText(
                             rest, QtCore.Qt.TextElideMode.ElideRight, max(avail, 10)))
        painter.restore()

class DwellRingDelegate(QtWidgets.QStyledItemDelegate):
    """Paints a small circular countdown on ⏳ DWELL rows (Created column): a teal arc fills as the
    dwell elapses, so 'how close to filing' is visible at a glance. Non-dwell rows paint normally."""
    FRAC_ROLE = QtCore.Qt.ItemDataRole.UserRole + 2

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        if index.data(self.FRAC_ROLE) is not None:
            s.setWidth(s.width() + 26)                           # room for the ring
        return s

    def paint(self, painter, option, index):
        frac = index.data(self.FRAC_ROLE)
        if frac is None:
            super().paint(painter, option, index)
            return
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QtWidgets.QApplication.style()
        style.drawControl(QtWidgets.QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        painter.save()
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        cy = option.rect.center().y()
        ring = QtCore.QRectF(option.rect.left() + 8, cy - 7, 14, 14)
        painter.setPen(QtGui.QPen(QtGui.QColor("#2a2e37"), 2.4))
        painter.drawEllipse(ring)                                # track
        painter.setPen(QtGui.QPen(QtGui.QColor("#5eead4"), 2.4,
                                  QtCore.Qt.PenStyle.SolidLine, QtCore.Qt.PenCapStyle.RoundCap))
        painter.drawArc(ring, 90 * 16, -int(min(max(float(frac), 0.0), 1.0) * 360 * 16))
        fg = index.data(QtCore.Qt.ItemDataRole.ForegroundRole)
        painter.setPen(fg.color() if isinstance(fg, QtGui.QBrush) else QtGui.QColor("#9aa0a6"))
        painter.setFont(option.font)
        painter.drawText(QtCore.QRectF(ring.right() + 6, option.rect.top(),
                                       option.rect.width() - 30, option.rect.height()),
                         QtCore.Qt.AlignmentFlag.AlignVCenter, index.data() or "")
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

class Sparkline(QtWidgets.QWidget):
    """Tiny inline trend line for the header: cumulative reports over the last 30 days, drawn as a
    soft gradient-filled polyline. Pure QPainter; repaints only when the data actually changes."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._pts = []
        self.setFixedSize(92, 26)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_TranslucentBackground)

    def set_series(self, values):
        vals = list(values)[-30:]
        if vals != self._pts:
            self._pts = vals
            self.setToolTip(f"reports over the last {len(vals)} days")
            self.update()

    def paintEvent(self, _e):
        if len(self._pts) < 2:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h, pad = self.width(), self.height(), 3
        lo, hi = min(self._pts), max(self._pts)
        span = (hi - lo) or 1
        step = (w - 2 * pad) / (len(self._pts) - 1)
        pts = [QtCore.QPointF(pad + i * step, h - pad - (v - lo) / span * (h - 2 * pad))
               for i, v in enumerate(self._pts)]
        fill = QtGui.QPainterPath()
        fill.moveTo(pts[0].x(), h - 1)
        for pt in pts:
            fill.lineTo(pt)
        fill.lineTo(pts[-1].x(), h - 1)
        grad = QtGui.QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QtGui.QColor(94, 234, 212, 90))
        grad.setColorAt(1.0, QtGui.QColor(94, 234, 212, 0))
        p.fillPath(fill, grad)
        pen = QtGui.QPen(QtGui.QColor("#5eead4"), 1.6)
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawPolyline(*pts)
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor("#5eead4"))
        p.drawEllipse(pts[-1], 2.2, 2.2)         # live tip

class ToggleSwitch(QtWidgets.QAbstractButton):
    """A graphically-pleasing iOS-style on/off switch (pure QPainter, animated). Emits toggled(bool)."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(48, 26)
        self._p = 0.0                                  # knob position 0..1
        self._anim = QtCore.QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(150)
        self._anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        self.toggled.connect(lambda on: (self._anim.stop(), self._anim.setEndValue(1.0 if on else 0.0),
                                         self._anim.start()))

    def set_silent(self, on):                          # set state without animating or emitting
        self.blockSignals(True)
        self.setChecked(on)
        self.blockSignals(False)
        self._p = 1.0 if on else 0.0
        self.update()

    def getKnob(self):
        return self._p

    def setKnob(self, v):
        self._p = v
        self.update()

    knob = QtCore.pyqtProperty(float, getKnob, setKnob)

    def sizeHint(self):
        return QtCore.QSize(48, 26)

    def paintEvent(self, _e):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w, h, t = self.width(), self.height(), self._p
        r = h / 2

        def mix(a, b):
            return int(a + (b - a) * t)
        off, on = (58, 63, 75), (63, 185, 80)          # grey -> green
        p.setBrush(QtGui.QColor(mix(off[0], on[0]), mix(off[1], on[1]), mix(off[2], on[2])))
        p.setPen(QtCore.Qt.PenStyle.NoPen)
        p.drawRoundedRect(QtCore.QRectF(0, 0, w, h), r, r)
        kx = r + (w - 2 * r) * t
        p.setBrush(QtGui.QColor(245, 247, 250))
        p.drawEllipse(QtCore.QPointF(kx, h / 2), r - 3.5, r - 3.5)
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
