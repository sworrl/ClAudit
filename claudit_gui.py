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


def git_commit():
    """Short commit hash of the running checkout (so the GUI shows exactly what's deployed)."""
    try:
        d = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run(["git", "-C", d, "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


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
QTableWidget { background: #1b1e25; alternate-background-color: #181b21;
    gridline-color: #2a2e37; border: 1px solid #2a2e37; border-radius: 8px; }
QTableWidget::item { padding: 5px 6px; }
QTableWidget::item:selected { background: #3a2f63; color: #fff; }
QHeaderView::section { background: #232733; color: #c7cdd6; padding: 7px 8px;
    border: 0; border-right: 1px solid #2a2e37; font-weight: 600; }
QPushButton { background: #2a2f3a; color: #cbd2da; border: 1px solid #353b47;
    border-radius: 7px; padding: 7px 15px; font-weight: 600; }
QPushButton:hover { background: #343c4a; }
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


# ----------------------------- background workers -----------------------------
class Watcher(QtCore.QThread):
    acted = QtCore.pyqtSignal(int, str)    # (count, kind: "auto"|"queued"|"backfill")

    def __init__(self, state, repo, interval, auto, backfill, backfill_interval, backfill_max):
        super().__init__()
        self.state, self.repo, self.interval, self._run = state, repo, interval, True
        self.auto = auto                   # toggled live from the tray menu
        self.backfill = backfill
        self.backfill_interval = backfill_interval
        self.backfill_max = backfill_max
        self.bf_done = 0
        self.last_live = 0.0
        self.last_bf = 0.0
        self.bf_delay = max(4.0, float(backfill_interval))   # seconds between drips, adaptive

    def run(self):
        with STATE_LOCK:
            cs.ensure_baseline(self.state)
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
    """Fetch ALL false-positive issues on the repo (every author, open + closed) + your login."""
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
                ["gh", "issue", "list", "-R", self.repo, "--state", "all", "--limit", "300",
                 "--search", "false positive in:title",
                 "--json", "number,state,title,author,url,createdAt"],
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
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.doubleClicked.connect(self._open_row)
        self.table.clicked.connect(self._open_row)
        self.empty = QtWidgets.QLabel("🔎  Loading false-positive issues…",
                                      self.table.viewport())
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color:#6b7280; font-size:15px; background:transparent;")

        self.f_scope = QtWidgets.QComboBox()
        self.f_scope.addItems(["All issues", "Mine only"])
        self.f_state = QtWidgets.QComboBox()
        self.f_state.addItems(["Open + Closed", "Open only", "Closed only"])
        self.f_search = QtWidgets.QLineEdit()
        self.f_search.setPlaceholderText("Filter by title…")
        self.f_search.setClearButtonEnabled(True)
        for w in (self.f_scope, self.f_state):
            w.currentIndexChanged.connect(self._repopulate)
        self.f_search.textChanged.connect(self._repopulate)
        filt = QtWidgets.QHBoxLayout()
        filt.addWidget(QtWidgets.QLabel("Show:"))
        filt.addWidget(self.f_scope)
        filt.addWidget(self.f_state)
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
        header = QtWidgets.QWidget()
        header.setObjectName("header")
        hl = QtWidgets.QHBoxLayout(header)
        hl.setContentsMargins(14, 9, 14, 9)
        logo = QtWidgets.QLabel()
        if os.path.exists(cs.ICON):
            logo.setPixmap(QtGui.QIcon(cs.ICON).pixmap(26, 26))
        brand = QtWidgets.QLabel("ClAudit")
        brand.setObjectName("brand")
        _c = git_commit()
        sub = QtWidgets.QLabel(f"v{cs.__version__}{(' · ' + _c) if _c else ''} · false-positive block reporter")
        sub.setObjectName("subtle")
        hl.addWidget(logo)
        hl.addSpacing(8)
        hl.addWidget(brand)
        hl.addSpacing(10)
        hl.addWidget(sub)
        hl.addStretch(1)

        self.bf_bar = QtWidgets.QProgressBar()
        self.bf_bar.setObjectName("bf")
        self.bf_bar.setTextVisible(True)
        self.bf_bar.setMinimumHeight(24)
        self.btn_dedup = QtWidgets.QPushButton("👎 Not a dupe")
        self.btn_dedup.setToolTip("On the selected issue, 👎 the dup-bot + post a 'not a duplicate' note (live)")
        self.btn_dedup.clicked.connect(self._dedup_selected)
        bar.insertWidget(2, self.btn_dedup)

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
        self.tabs.currentChanged.connect(lambda i: self._fetch_stats() if i == 1 else None)

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
        self.update_timer.start(60000)
        self._fetch_stats()
        self.refresh()

    def _check_updates(self):
        cur = git_commit()
        if cur and self._head and cur != self._head:   # a real commit/pull, not a transient edit
            self._restart()

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
        self.act_llm = menu.addAction("Claude PII scrubbing")
        self.act_llm.setCheckable(True)
        self.act_llm.setChecked(claudit.LLM_SCRUB)
        self.act_llm.toggled.connect(self._toggle_llm)
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

    # ---- project stats tab ----
    def _build_stats_tab(self):
        w = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(w)
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
        b = QtWidgets.QPushButton("Refresh stats")
        b.clicked.connect(self._fetch_stats)
        v.addWidget(b)
        return w

    def _fetch_stats(self):
        f = RepoStatsFetcher(cs.PROJECT_URL.split("github.com/")[-1])
        f.fetched.connect(self._on_stats)
        f.start()
        self._sf = f

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
        self.tray.showMessage("ClAudit", f"#{num}: 👎'd the dup-bot + posted 'not a duplicate'." if ok
                              else f"#{num}: dedup failed (see logs).")
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
        needle = self.f_search.text().strip().lower()

        rows = []   # (sort_ts, state, issue_label, author, created, title, url)
        pend = set(cs.pending_sigs(self.state))
        for sig in pend:   # your queued-but-unfiled blocks always count as "yours"
            f = self.findings.get(sig, {})
            kind = f.get("kind", "?")
            snippet = (cs.scrub(f.get("prompt", ""))[0])[:80] if f else ""
            title = f"[{kind}] {snippet}"
            if statef != "Closed only" and (not needle or needle in title.lower()):
                rows.append(("9999", "queued", "QUEUED", "you", "—", title, ""))

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
            if needle and needle not in title.lower():
                continue
            created = fmt_ts(it.get("createdAt", ""))
            ded = (self.state.get("__deduped__", {}) or {}).get(str(it["number"]))
            label = f"#{it['number']}" + (" 👎✓" if ded == "not-duplicate" else "")
            rows.append((it.get("createdAt", ""), st, label, author, created, title, it.get("url", "")))

        rows.sort(key=lambda r: r[0], reverse=True)   # newest first

        self.table.setRowCount(len(rows))
        for r, (_, st, num, author, created, title, url) in enumerate(rows):
            is_claudit = "claudit" in title.lower() or title.lower().startswith(("[cyber]", "[aup]", "[bug]"))
            if author == "you" or (self.me and author == self.me):
                owner = "#b794f6"          # yours = purple
            elif is_claudit:
                owner = "#5eead4"          # another ClAudit user = teal
            else:
                owner = None
            for c, val in enumerate(["●", num, author, created, title]):
                item = QtWidgets.QTableWidgetItem(val)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, url)
                item.setToolTip("Click to open in browser" if url else "Not filed yet")
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

    def _on_community(self, items, me):
        self.community, self.me = items, me
        self._repopulate()

    def _open_row(self, idx):
        item = self.table.item(idx.row(), 0)
        url = item.data(QtCore.Qt.ItemDataRole.UserRole) if item else ""
        if url:
            QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _on_acted(self, n, kind):
        icon = (QtGui.QIcon(cs.ICON) if os.path.exists(cs.ICON)
                else QtWidgets.QSystemTrayIcon.MessageIcon.Information)
        if kind == "backfill":   # historical: from your backlog, not just-happened
            self.tray.setToolTip("ClAudit — backfilling (historical)")
            self.tray.showMessage("ClAudit · 📦 HISTORICAL",
                                  f"Backfilled {n} block(s) from your backlog.", icon)
            return   # board_timer handles the list refresh (no per-drip refetch)
        if kind == "auto":       # live: a block that just happened
            self.tray.setToolTip("ClAudit — watching live")
            self.tray.showMessage("ClAudit · 🔴 LIVE",
                                  f"Reported {n} block(s) the moment it happened — {self.repo}.", icon)
        else:
            self.tray.showMessage("ClAudit", f"{n} new block(s) queued — use ‘Report pending’.", icon)
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
