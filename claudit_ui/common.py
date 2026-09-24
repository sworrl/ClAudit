"""ClAudit GUI: common (split out of claudit_gui.py; see that file for the entry point)."""
import datetime
import os
import subprocess
import sys
import threading
import time
from PyQt6 import QtGui
import claudit_scan as cs


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

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # repo root (claudit_ui/ is one below)

try:
    from PyQt6.QtSvgWidgets import QSvgWidget
    _HAVE_SVG = True
except Exception:
    QSvgWidget = None          # main_window imports the name; it only uses it when _HAVE_SVG
    _HAVE_SVG = False

try:
    sys.path.insert(0, os.path.join(REPO_DIR, "scripts"))
    import render_poll as _rp  # reuse render_trend_svg for the in-GUI chart
except Exception:
    _rp = None

# ---- watchdog: a detached supervisor that relaunches the GUI if it ever crashes ----
GUI_SCRIPT = os.path.join(REPO_DIR, "claudit_gui.py")

WATCHDOG_PID = os.path.join(cs.STATE_DIR, "watchdog.pid")

QUIT_FLAG = os.path.join(cs.STATE_DIR, "watchdog_quit.flag")

def _pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError):
        return False

def _read_pid(path):
    try:
        return int(open(path).read().strip() or "0")
    except (OSError, ValueError):
        return 0

def watchdog_running():
    pid = _read_pid(WATCHDOG_PID)
    return bool(pid and pid != os.getpid() and _pid_alive(pid))

def _launch(extra=()):
    subprocess.Popen([sys.executable, GUI_SCRIPT, *extra], cwd=REPO_DIR, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)

def spawn_watchdog():
    """Start the detached supervisor (idempotent — a no-op if one is already alive)."""
    os.makedirs(cs.STATE_DIR, exist_ok=True)
    try:
        os.remove(QUIT_FLAG)                       # a fresh run cancels any pending 'quit on purpose'
    except OSError:
        pass
    if not watchdog_running():
        _launch(["--watchdog"])

def run_watchdog():
    """Supervisor loop (runs in its own detached process). Watches the GUI singleton lock and
    relaunches the GUI on a crash. Exits cleanly when watchdog mode is turned off in config or the
    GUI quit on purpose (QUIT_FLAG)."""
    os.makedirs(cs.STATE_DIR, exist_ok=True)
    with open(WATCHDOG_PID, "w") as fh:
        fh.write(str(os.getpid()))
    misses = 0
    try:
        while True:
            time.sleep(4)
            if not cs.load_config().get("watchdog"):
                return                             # disabled at runtime
            if os.path.exists(QUIT_FLAG):
                return                             # GUI quit intentionally
            if _pid_alive(_read_pid(cs.LOCK_FILE)):
                misses = 0
                continue
            misses += 1
            if misses < 3:                         # tolerate the brief lock gap during an execv restart
                continue
            misses = 0
            _launch()                              # crashed → bring it back (visible)
            time.sleep(8)                          # let the new GUI re-grab the singleton lock
    finally:
        if _read_pid(WATCHDOG_PID) == os.getpid():
            try:
                os.remove(WATCHDOG_PID)
            except OSError:
                pass

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
QProgressBar#bf::chunk { border-radius: 6px;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #7c3aed, stop:0.55 #8b5cf6, stop:1 #c084fc); }
QFrame#setrow { background: transparent; border-radius: 8px; padding: 2px; }
QFrame#setrow:hover { background: #232838; }
QGroupBox { border: 1px solid #2a2e37; border-radius: 10px; margin-top: 12px; padding-top: 10px;
    font-weight: 700; color: #cdd3dc; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #b794f6; }
QSlider::groove:horizontal { height: 6px; background: #2a2e37; border-radius: 3px; }
QSlider::sub-page:horizontal { background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
    stop:0 #7c3aed, stop:1 #c084fc); border-radius: 3px; }
QSlider::handle:horizontal { width: 16px; height: 16px; margin: -6px 0; border-radius: 8px;
    background: #e6e8ec; border: 2px solid #8b5cf6; }
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
