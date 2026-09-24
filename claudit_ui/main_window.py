"""ClAudit GUI: main window (split out of claudit_gui.py; see that file for the entry point)."""
import datetime
import json
import os
import sys
import threading
import time
from PyQt6 import QtCore, QtGui, QtWidgets
import claudit
import claudit_scan as cs
from .common import QSvgWidget, QUIT_FLAG, REPO_DIR, STATE_LOCK, _HAVE_SVG, _iso_epoch, _rp, _snap, chain_color, fmt_ts, git_commit, spawn_watchdog
from .dialogs import IssueDetailDialog, MuteListDialog, ScrubListDialog
from .widgets import BreakdownBars, ChainGraphDelegate, ChronoLine, DwellRingDelegate, Sparkline, TitleChipDelegate, ToggleSwitch, make_banner
from .workers import ClosureWorker, CommunityFetcher, DedupWorker, DefendAllWorker, NotifyWatcher, PollWorker, ReopenOneWorker, RepoStatsFetcher, Reporter, UpdateChecker, UsageFetcher, Watcher


# --------------------------------- main window --------------------------------
class Main(QtWidgets.QMainWindow):
    COLS = ["", "Issue", "Author", "Created", "Title"]
    SCREENSHOT_DIR = ""      # set by --screenshot: view-only run that saves one PNG per tab and exits

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
        self._ring_delegate = DwellRingDelegate(self.table)     # countdown ring on ⏳ DWELL rows
        self.table.setItemDelegateForColumn(3, self._ring_delegate)
        self._chip_delegate = TitleChipDelegate(self.table)     # [cyber]/[aup] pill chips in titles
        self.table.setItemDelegateForColumn(4, self._chip_delegate)
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
        self.f_kind.addItems(["All kinds", "cyber", "aup", "Fable 5"])   # harness is excluded
        self.f_dedup = QtWidgets.QComboBox()
        self.f_dedup.addItems(["Any", "Defended", "Not defended", "Swept", "Merged"])
        self.f_search = QtWidgets.QLineEdit()
        self.f_search.setPlaceholderText("Filter by title or #number…")
        self.f_search.setClearButtonEnabled(True)
        for w in (self.f_scope, self.f_state, self.f_kind, self.f_dedup):
            w.currentIndexChanged.connect(self._repopulate)
        self._search_debounce = QtCore.QTimer(self)          # don't re-lay 400+ rows per keystroke
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(250)
        self._search_debounce.timeout.connect(self._repopulate)
        self.f_search.textChanged.connect(lambda _t: self._search_debounce.start())
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
        self.spark = Sparkline()                 # 30-day reports trend, right in the header
        hl.addWidget(self.spark)
        hl.addSpacing(8)
        self.tok_label = QtWidgets.QLabel("")    # usage meter: live plan windows + ClAudit's own spend
        self._usage = claudit.plan_usage(fetch=False) or {}   # disk cache only; the fetcher refreshes it
        self._usage_dirty = True
        self.tok_label.setCursor(QtCore.Qt.CursorShape.WhatsThisCursor)
        hl.addWidget(self.tok_label)
        hl.addSpacing(8)
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

        # build the watcher BEFORE the tabs/tray — both read its live state for their initial toggles
        self.watcher = Watcher(self.state, repo, interval, auto, backfill, backfill_interval, backfill_max)
        _cfg = cs.load_config()
        self.watcher.dwell = bool(_cfg.get("dwell_autofile"))              # opt-in dwell auto-filer
        self.watcher.amplify = bool(_cfg.get("amplify"))                   # opt-in community 👍
        if "defend" in _cfg:
            self.watcher.defend = bool(_cfg["defend"])
        if "reopen" in _cfg:
            self.watcher.reopen = bool(_cfg["reopen"])
        if "closures" in _cfg:
            self.watcher.closures = bool(_cfg["closures"])

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(board, "Issues")
        self.tabs.addTab(self._build_stats_tab(), "Project")
        self.tabs.addTab(self._build_activity_tab(), "Activity")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.tabs.currentChanged.connect(
            lambda i: (self._fetch_stats(), self._fetch_poll()) if i == 1 else None)
        self.tabs.currentChanged.connect(self._fade_tab)

        root = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(root)
        lay.addWidget(header)
        lay.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

        self._build_tray(auto, backfill)
        self.watcher.acted.connect(self._on_acted)
        if not self.SCREENSHOT_DIR:
            self.watcher.start()          # screenshot mode never files, defends, or backfills
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
        if getattr(self, "_nw", None) and self._nw.isRunning():
            return                             # previous poll still in flight — don't stack threads
        self._nw = NotifyWatcher()
        self._nw.got.connect(self._on_notifs)
        self._nw.start()

    def _on_notifs(self, items):
        new = [n for n in items if n.get("id") not in self._seen_notifs]
        for n in items:
            self._seen_notifs.add(n.get("id"))
        if len(self._seen_notifs) > 5000:      # bound the id cache (it only needs the recent window)
            self._seen_notifs = set(list(self._seen_notifs)[-2500:])
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
        if getattr(self, "_uc", None) and self._uc.isRunning():
            return                             # a slow fetch is still going — skip this tick
        self._uc = UpdateChecker(self._head)
        self._uc.updated.connect(self._restart)
        self._uc.newer.connect(self._on_newer_release)
        self._uc.start()

    def _on_newer_release(self, latest):
        if getattr(self, "_release_told", "") == latest:
            return
        self._release_told = latest
        self.tray.showMessage("ClAudit", f"Version {latest} is out (you run {cs.__version__}). "
                              f"pip install --upgrade \"claudit-cc[gui] @ https://github.com/{cs.POLL_REPO}"
                              f"/archive/refs/tags/v{latest}.tar.gz\"",
                              QtWidgets.QSystemTrayIcon.MessageIcon.Information, 10000)

    def _restart(self):
        self.tray.showMessage("ClAudit", "Update detected — restarting with the new version…")
        if self.watcher:
            self.watcher.stop()
            self.watcher.wait(2000)
        cs._release_singleton()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    def _fade_tab(self, i):
        """Subtle fade-in when switching tabs (self-removing effect, fire-and-forget animation)."""
        w = self.tabs.widget(i)
        if w is None:
            return
        eff = QtWidgets.QGraphicsOpacityEffect(w)
        w.setGraphicsEffect(eff)
        anim = QtCore.QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(160)
        anim.setStartValue(0.45)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QtCore.QEasingCurve.Type.OutCubic)
        anim.finished.connect(lambda: w.setGraphicsEffect(None))
        anim.start(QtCore.QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)

    @staticmethod
    def _fmt_tok(n):
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n / 1_000:.1f}K"
        return str(int(n))

    def _refresh_usage(self):
        """Kick an off-thread fetch of the live plan usage (no-op while one is running)."""
        if getattr(self, "_uf", None) and self._uf.isRunning():
            return
        self._uf = UsageFetcher()
        self._uf.got.connect(self._on_usage)
        self._uf.start()

    USAGE_ALERTS = (80, 95)          # tray toast once per level per crossing (re-arms below 80)

    def _on_usage(self, u):
        if u:
            self._usage = u
            peak = max(u["five_hour"]["pct"], u["seven_day"]["pct"])
            level = max((lv for lv in self.USAGE_ALERTS if peak >= lv), default=0)
            seen = getattr(self, "_usage_alerted", 0)
            if level > seen:
                guarded, why = claudit.usage_guarded(u)
                self.tray.showMessage(
                    "ClAudit · plan usage", f"Your Claude plan is at {peak:.0f}% of a window. "
                    + (f"Usage guard active: {why}; claude calls are paused."
                       if guarded else f"claude calls pause at {claudit.USAGE_GUARD_PCT}%."),
                    QtWidgets.QSystemTrayIcon.MessageIcon.Warning, 8000)
            self._usage_alerted = level if level else 0
        self._usage_dirty = True

    def _update_tokens(self):
        # runs every second off bf_timer — only re-read/re-render when tokens.json changed, the
        # burn flag flipped, or a fresh usage snapshot landed (idle cost is one stat() call)
        try:
            mt = os.stat(claudit.TOKENS_FILE).st_mtime
        except OSError:
            mt = 0.0
        burn = bool(claudit.BURN_TOKENS)
        self._tok_tick = getattr(self, "_tok_tick", 0) + 1
        if self._tok_tick == 2 or self._tok_tick % claudit.USAGE_TTL == 0:
            self._refresh_usage()                 # at startup, then every USAGE_TTL seconds
        stale = self._tok_tick % 60 == 0          # reset countdowns tick down once a minute
        changed = (mt, burn) != getattr(self, "_tok_seen", None) or self._usage_dirty
        if not changed and not burn and not stale:
            return
        redraw = stale or changed
        self._tok_seen = (mt, burn)
        self._usage_dirty = False
        if not redraw:                            # burn mode: still pulse the colors each tick
            self._tok_pulse = not getattr(self, "_tok_pulse", False)
            col = "#ff3b30" if self._tok_pulse else "#ff9f0a"
            self.tok_label.setStyleSheet(
                f"color:#fff; background:{col}; border-radius:8px; padding:2px 10px; font-weight:800;")
            return
        t = claudit.load_tokens()
        u = self._usage or None
        if u:
            # the real thing: the same 5-hour / 7-day windows Claude Code's /usage shows, plus any
            # model-scoped weekly limit that is currently the binding one
            parts = [f"5h {u['five_hour']['pct']:.0f}%", f"7d {u['seven_day']['pct']:.0f}%"]
            parts += [f"{s['name']} {s['pct']:.0f}%" for s in u.get("scoped", []) if s.get("active")]
            age = time.time() - float(u.get("fetched", 0) or 0)
            self.tok_label.setText("🔥 " + " · ".join(parts) + (" · stale" if age > 3 * claudit.USAGE_TTL else ""))
            frac = min(claudit.usage_peak(u) / 100.0, 1.0)
        else:
            wk, plans = claudit.plan_estimates(t)
            pct = "  ".join(f"{n.replace('Max ', 'M')} {p:.0f}%" for n, p in plans.items())
            self.tok_label.setText(f"🔥 est. ${wk:.2f}/wk · {pct}")
            frac = min(plans.get("Pro", 0.0) / 100.0, 1.0)
        self.tok_label.setToolTip(
            claudit.usage_summary(u, t)
            + ("\n\n🔥 BURN-TOKENS MODE IS ON — the LLM writes every report." if burn
               else "\n\nBurn-tokens mode is off (the meter still tracks).")
            + ("" if u else "\n\nSign in to Claude Code (`claude`) to show your real plan windows here."))
        if burn:                                  # alarming red⇄orange pulse while burning
            self._tok_pulse = not getattr(self, "_tok_pulse", False)
            col = "#ff3b30" if self._tok_pulse else "#ff9f0a"
            self.tok_label.setStyleSheet(
                f"color:#fff; background:{col}; border-radius:8px; padding:2px 10px; font-weight:800;")
        else:
            # quiet mode: the pill FILLS left-to-right with the highest plan window in use —
            # green under 50%, amber under 80%, red beyond (severity at a glance, no reading needed)
            col = "#2f6b3f" if frac < 0.5 else ("#7a5c22" if frac < 0.8 else "#7a2e2e")
            f1, f2 = max(frac, 0.001), min(max(frac, 0.001) + 0.001, 1.0)
            self.tok_label.setStyleSheet(
                "color:#aeb6c2; border:1px solid #2a2e37; border-radius:8px; padding:2px 10px; "
                "font-weight:600; background:qlineargradient(x1:0,y1:0,x2:1,y2:0, "
                f"stop:0 {col}, stop:{f1:.3f} {col}, stop:{f2:.3f} #1b1e25, stop:1 #1b1e25);")

    def _update_bf(self):
        self._update_tokens()
        w = self.watcher
        snap = _snap(self.state)                 # copy: the watcher thread mutates state under us
        filed = sum(1 for s, r in snap.items()
                    if not s.startswith("__") and isinstance(r, dict) and r.get("issue"))
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
        self.act_defend.setChecked(bool(self.watcher and self.watcher.defend))
        self.act_defend.toggled.connect(self._toggle_defend)
        self.act_reopen = menu.addAction("Auto-reopen dup-bot closes")
        self.act_reopen.setCheckable(True)
        self.act_reopen.setChecked(bool(self.watcher and self.watcher.reopen))
        self.act_reopen.toggled.connect(self._toggle_reopen)
        self.act_closures = menu.addAction("Auto-defend sweeps && merges")
        self.act_closures.setCheckable(True)
        self.act_closures.setChecked(bool(self.watcher and self.watcher.closures))
        self.act_closures.toggled.connect(self._toggle_closures)
        self.act_llm = menu.addAction("Claude PII scrubbing")
        self.act_llm.setCheckable(True)
        self.act_llm.setChecked(claudit.LLM_SCRUB)
        self.act_llm.toggled.connect(self._toggle_llm)
        menu.addSeparator()
        menu.addAction("🧹 Defend closures now", self._defend_closures_now)
        menu.addAction("☂ Open umbrella issue",
                       lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(
                           f"https://github.com/{self.repo}/issues/{cs.umbrella_num()}")))
        menu.addSeparator()
        menu.addAction("🔒 Edit PII denylist…", self._edit_scrub)
        menu.addAction("🔇 Edit mute list…", self._edit_mute)
        menu.addAction("🩺 Run doctor…", self._run_doctor)
        menu.addAction("Show window", self._show_window)
        menu.addAction("Refresh", self.refresh)
        menu.addAction("Open repo issues",
                       lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(
                           f"https://github.com/{self.repo}/issues?q=is:issue+author:@me")))
        menu.addSeparator()
        menu.addAction("Quit", self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _show_window(self):
        # reliably surface the window even from tray/minimised/off-screen (Wayland-friendly)
        self.setVisible(True)
        self.setWindowState(self.windowState() & ~QtCore.Qt.WindowState.WindowMinimized
                            | QtCore.Qt.WindowState.WindowActive)
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _tray_activated(self, reason):
        if reason != QtWidgets.QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self._show_window()

    def _edit_scrub(self):
        ScrubListDialog(self).exec()

    def _edit_mute(self):
        MuteListDialog(self).exec()

    def _run_doctor(self):
        """The same environment check as `claudit_scan.py --doctor`, in a dialog (off-thread)."""
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle("ClAudit doctor")
        dlg.resize(720, 460)
        if os.path.exists(cs.ICON):
            dlg.setWindowIcon(QtGui.QIcon(cs.ICON))
        v = QtWidgets.QVBoxLayout(dlg)
        out = QtWidgets.QPlainTextEdit("Checking…")
        out.setReadOnly(True)
        out.setFont(QtGui.QFont("monospace"))
        v.addWidget(out, 1)
        row = QtWidgets.QHBoxLayout()
        copy = QtWidgets.QPushButton("Copy")
        copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText(out.toPlainText()))
        close = QtWidgets.QPushButton("Close")
        close.clicked.connect(dlg.accept)
        row.addStretch(1)
        row.addWidget(copy)
        row.addWidget(close)
        v.addLayout(row)

        class _Doc(QtCore.QThread):
            done = QtCore.pyqtSignal(str)

            def run(self):
                try:
                    self.done.emit(cs.doctor_text(cs.doctor_rows()))
                except Exception as e:
                    self.done.emit(f"doctor failed: {e}")
        t = _Doc(dlg)
        t.done.connect(out.setPlainText)
        t.start()
        dlg.exec()
        t.wait(2000)

    def _toggle_auto(self, on):
        if not self.watcher:
            return
        self.watcher.auto = on
        self._sync_setting_ui("auto", on)
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
        self._sync_setting_ui("dwell_autofile", on)
        mins = cs.DWELL_SECONDS // 60
        self.tray.showMessage("ClAudit", f"Dwell auto-file ENABLED — new blocks wait ~{mins} min, the "
                              "LLM judges + writes each, then files one linked bespoke issue per Request "
                              "ID. No manual push." if on else "Dwell auto-file disabled.")

    def _toggle_llm(self, on):
        claudit.LLM_SCRUB = on
        cfg = cs.load_config()
        cfg["llm_scrub"] = on
        cs.save_config(cfg)
        self._sync_setting_ui("llm_scrub", on)
        self.tray.showMessage("ClAudit", f"Claude PII scrubbing {'ON' if on else 'OFF'} (saved).")

    def _toggle_backfill(self, on):
        if not self.watcher:
            return
        self.watcher.backfill = on
        self._sync_setting_ui("backfill", on)
        self.tray.showMessage("ClAudit", f"Backfill ENABLED — drip-filing old backlog "
                              f"(1 / {self.watcher.backfill_interval:g} min)." if on
                              else "Backfill paused.")

    def _toggle_defend(self, on):
        if not self.watcher:
            return
        self.watcher.defend = on
        self._sync_setting_ui("defend", on)
        if on:
            self.watcher.last_defend = 0.0   # sweep on the next tick
        self.tray.showMessage("ClAudit", "Auto-defend ENABLED — every dup-bot flag gets 👎 + a "
                              "'not a duplicate' note automatically." if on
                              else "Auto-defend paused.")

    def _toggle_reopen(self, on):
        if not self.watcher:
            return
        self.watcher.reopen = on
        self._sync_setting_ui("reopen", on)
        if on:
            self.watcher.last_reopen = 0.0   # sweep on the next tick
        self.tray.showMessage("ClAudit", "Auto-reopen ENABLED — issues the dup-bot CLOSED as "
                              "duplicates get reopened (your own closes are left alone)." if on
                              else "Auto-reopen paused.")

    def _toggle_closures(self, on):
        if not self.watcher:
            return
        self.watcher.closures = on
        self._sync_setting_ui("closures", on)
        if on:
            self.watcher.last_closures = 0.0   # sweep on the next tick
        self.tray.showMessage("ClAudit", "Closure defender ENABLED — bot stale-sweeps get a "
                              "still-relevant note (tracked in the umbrella issue) and merged "
                              "dups' request IDs are folded onto their canonicals." if on
                              else "Closure defender paused.")

    def _defend_closures_now(self):
        self._log("🧹 closure defender: scanning your closed issues…")
        self._cwq = ClosureWorker(self.state, self.repo)
        self._cwq.progress.connect(lambda num: self._log(f"🧹 acted on #{num}"))
        self._cwq.finished_n.connect(lambda n: (
            self._log(f"🧹 closure defender done — {n} action(s)"), self.refresh()))
        self._cwq.start()

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
                    num = str(rec.get("issue") or "")
                    key = rec.get("chain") or ((rec.get("projects") or [None])[0])  # session-precise; project fallback
                    if num and key:
                        chain[num] = key
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

    # ---- settings tab (everything applies on the fly, no save button) ----
    def _build_settings_tab(self):
        area = QtWidgets.QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        inner = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(inner)
        head = QtWidgets.QLabel("Every setting applies the moment you change it, no save button. "
                                "Choices persist across restarts.")
        head.setWordWrap(True)
        v.addWidget(head)
        self._toggles = {}
        w = self.watcher

        def grp(title, rows):
            box = QtWidgets.QGroupBox(title)
            g = QtWidgets.QVBoxLayout(box)
            for key, label, desc, on in rows:
                frame = QtWidgets.QFrame()      # hoverable row (see QFrame#setrow in STYLE)
                frame.setObjectName("setrow")
                row = QtWidgets.QHBoxLayout(frame)
                row.setContentsMargins(8, 4, 8, 4)
                txt = QtWidgets.QVBoxLayout()
                txt.setSpacing(1)
                nm = QtWidgets.QLabel(label)
                nm.setStyleSheet("font-weight:600; background:transparent;")
                ds = QtWidgets.QLabel(desc)
                ds.setObjectName("subtle")
                ds.setWordWrap(True)
                ds.setStyleSheet("background:transparent;")
                txt.addWidget(nm)
                txt.addWidget(ds)
                row.addLayout(txt, 1)
                sw = ToggleSwitch()
                sw.set_silent(on)
                sw.toggled.connect(lambda val, k=key: self._apply_setting(k, val))
                self._toggles[key] = sw
                row.addWidget(sw, 0, QtCore.Qt.AlignmentFlag.AlignVCenter)
                g.addWidget(frame)
            v.addWidget(box)

        grp("Filing && detection", [
            ("auto", "Auto-post new blocks", "File each new block the instant it's seen.", bool(w and w.auto)),
            ("dwell_autofile", "Dwell auto-file", ("Hold new blocks for the dwell, LLM-judge + compose, then "
             "file one linked bespoke issue per Request ID. No manual push."), bool(w and w.dwell)),
            ("backfill", "Backfill old blocks", "Slowly drip-file the baselined backlog while watching.",
             bool(w and w.backfill)),
            ("report_harness", "File harness reports too", ("Also file auto-mode-classifier (harness) denials. "
             "Off by default — they're local decisions and often correct."), cs.REPORT_HARNESS)])
        grp("Defense", [
            ("defend", "Auto-defend dup-bot flags", ("👎 + a contextual 'not a duplicate' note on every "
             "flagged issue."), bool(w and w.defend)),
            ("reopen", "Auto-reopen dup-bot closes", ("Reopen issues the dup-bot closed as duplicates (your "
             "own closes are left alone)."), bool(w and w.reopen)),
            ("closures", "Closure defender (sweeps and merges)", ("Answer inactivity-bot stale-sweeps with a "
             "still-relevant note tracked in the umbrella issue, and fold merged dups' request IDs onto "
             "their canonicals."), bool(w and w.closures)),
            ("amplify", "Community 👍 amplification", ("👍 other ClAudit users' open false-positive issues to "
             "boost the shared signal."), bool(w and w.amplify))])
        grp("Reliability", [
            ("watchdog", "Watchdog (keep-alive)", ("Run a detached supervisor that relaunches ClAudit "
             "automatically if it ever crashes. A normal Quit still quits."),
             bool(cs.load_config().get("watchdog")))])
        grp("LLM && PII", [
            ("llm_scrub", "Claude PII scrubbing", ("Use the claude CLI to catch names/hosts the regex can't, "
             "before posting."), claudit.LLM_SCRUB),
            ("burn_tokens", "Burn-tokens bespoke reports", ("Have Claude write each report (best PII defense + "
             "specific titles). Needs the claude CLI."), claudit.BURN_TOKENS),
            ("gate", "Honesty gate", ("LLM pre-judges 'real false positive vs correct block' and skips the "
             "latter before filing."), cs.GATE)])

        # token-diet controls: which model + effort ClAudit's own LLM calls use (live, like the rest)
        mbox = QtWidgets.QGroupBox("LLM cost")
        mv = QtWidgets.QVBoxLayout(mbox)
        mform = QtWidgets.QFormLayout()
        self.cmb_model = QtWidgets.QComboBox()
        self._model_opts = [("Haiku 4.5 — fast + cheapest (recommended)", "claude-haiku-4-5-20251001"),
                            ("Sonnet 5 — quality/cost balance", "claude-sonnet-5"),
                            ("Opus 4.8 — high", "claude-opus-4-8"),
                            ("Fable 5 — top tier, priciest", "claude-fable-5"),
                            ("Session default", "")]
        for label, _v in self._model_opts:
            self.cmb_model.addItem(label)
        cur = claudit.LLM_MODEL
        self.cmb_model.setCurrentIndex(next((i for i, (_l, v) in enumerate(self._model_opts)
                                             if v == cur), 0))
        self.cmb_model.currentIndexChanged.connect(
            lambda i: self._apply_setting("llm_model", self._model_opts[i][1]))
        mform.addRow("Compose/scrub/gate model:", self.cmb_model)
        self.cmb_effort = QtWidgets.QComboBox()
        self.cmb_effort.addItems(["low", "medium", "high"])
        self.cmb_effort.setCurrentText(claudit.LLM_EFFORT or "low")
        self.cmb_effort.currentTextChanged.connect(
            lambda t: self._apply_setting("llm_effort", t))
        mform.addRow("Effort:", self.cmb_effort)
        self.cmb_engine = QtWidgets.QComboBox()
        self._engine_opts = [("Auto (tandem when both installed)", "auto"),
                             ("Tandem — agy + claude cross-check", "tandem"),
                             ("agy (Antigravity CLI)", "agy"),
                             ("claude (Claude CLI)", "claude")]
        for label, _v in self._engine_opts:
            self.cmb_engine.addItem(label)
        cur_eng = (claudit.LLM_ENGINE or "auto").lower()
        self.cmb_engine.setCurrentIndex(next((i for i, (_l, v) in enumerate(self._engine_opts)
                                              if v == cur_eng), 0))
        self.cmb_engine.currentIndexChanged.connect(
            lambda i: self._apply_setting("llm_engine", self._engine_opts[i][1]))
        mform.addRow("LLM Engine:", self.cmb_engine)
        self._slider_row(mform, "Pause claude calls above", 50, 100, int(claudit.USAGE_GUARD_PCT or 100),
                         "%", "usage_guard_pct", lambda s: s)
        guard_note = QtWidgets.QLabel("Usage guard: once your 5-hour or 7-day plan window reaches this, "
                                      "ClAudit stops making claude calls (agy calls continue) so it never "
                                      "spends the last of a plan you need yourself. 100 = never pause.")
        guard_note.setObjectName("subtle")
        guard_note.setWordWrap(True)
        mform.addRow("", guard_note)
        mv.addLayout(mform)
        # estimated weekly spend per model at YOUR current filing rate — selected model highlighted
        self.cost_bars = BreakdownBars()
        self.cost_bars.fmt = lambda x: f"${x:.2f}"
        self.cost_bars.setMinimumHeight(120)
        mv.addWidget(self.cost_bars)
        self.cost_note = QtWidgets.QLabel("")
        self.cost_note.setObjectName("subtle")
        self.cost_note.setWordWrap(True)
        mv.addWidget(self.cost_note)
        self._refresh_llm_cost()
        v.addWidget(mbox)

        tbox = QtWidgets.QGroupBox("Timing")
        form = QtWidgets.QFormLayout(tbox)
        self._slider_row(form, "Dwell time", 1, 60, cs.DWELL_SECONDS // 60, "min", "dwell_seconds",
                         lambda m: m * 60)
        self._slider_row(form, "Watch interval", 10, 300, int(w.interval if w else 30), "s", "interval",
                         lambda s: s)
        v.addWidget(tbox)

        prow = QtWidgets.QHBoxLayout()
        pii = QtWidgets.QPushButton("Edit PII denylist…")
        pii.clicked.connect(self._edit_scrub)
        mute = QtWidgets.QPushButton("Edit mute list…")
        mute.setToolTip("Terms that stop a finding from being filed or sent to any LLM at all")
        mute.clicked.connect(self._edit_mute)
        prow.addWidget(pii)
        prow.addWidget(mute)
        prow.addStretch(1)
        v.addLayout(prow)
        v.addStretch(1)
        area.setWidget(inner)
        return area

    # empirical per-call ballparks (USD) from the token meter — cache state makes exact figures
    # noisy, so these are labeled estimates in the UI, not quotes
    MODEL_CALL_USD = {"claude-haiku-4-5-20251001": 0.03, "claude-sonnet-5": 0.05,
                      "claude-opus-4-8": 0.09, "claude-fable-5": 0.14, "": 0.14}

    def _refresh_llm_cost(self):
        """Estimated $/week per model at the CURRENT filing rate (reports in the last 7 days from
        the local issues DB × ~2 LLM calls per report). Selected model highlighted."""
        if not hasattr(self, "cost_bars"):
            return
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S")
        n = sum(1 for rec in cs.load_issue_rows() if (rec.get("first_ts") or "") >= cutoff)
        calls = max(n, 1) * 2                       # gate + merged compose per report
        rows = []
        for label, mid in self._model_opts:
            per = self.MODEL_CALL_USD.get(mid, 0.14)
            name = label.split(" — ")[0]
            sel = (mid == claudit.LLM_MODEL)
            rows.append((name + ("  ◀" if sel else ""), round(calls * per, 2),
                         "#b794f6" if sel else "#3d4658"))
        self.cost_bars.set_data(rows)
        self.cost_note.setText(
            f"Estimated $/week at your current rate: {n} report(s) filed in the last 7 days × "
            f"~2 LLM calls each. Ballpark per-call costs from the live token meter; cache state "
            f"makes exact figures vary. The 🔥 header meter tracks what you actually spend.")

    def _slider_row(self, form, label, lo, hi, init, unit, key, mapper):
        sl = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(int(init))
        sl.setFixedWidth(230)
        val = QtWidgets.QLabel(f"{int(init)} {unit}")
        val.setFixedWidth(56)
        # label tracks the drag live; the actual apply (config write) debounces to the last value
        deb = QtCore.QTimer(sl)
        deb.setSingleShot(True)
        deb.setInterval(400)
        deb.timeout.connect(lambda: self._apply_setting(key, mapper(sl.value())))
        sl.valueChanged.connect(lambda x: (val.setText(f"{x} {unit}"), deb.start()))
        cont = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(cont)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(sl)
        row.addWidget(val)
        form.addRow(label + ":", cont)

    def _apply_setting(self, key, val):
        """Apply a setting LIVE (running watcher + globals), persist it, and log it — no save button."""
        w = self.watcher
        if key == "auto" and w:
            w.auto = val
        elif key == "dwell_autofile" and w:
            w.dwell = val
            if val:                                    # the dwell filer needs the LLM
                cs.GATE = claudit.BURN_TOKENS = claudit.LLM_SCRUB = True
        elif key == "backfill" and w:
            w.backfill = val
        elif key == "defend" and w:
            w.defend = val
            if val:
                w.last_defend = 0.0
        elif key == "reopen" and w:
            w.reopen = val
            if val:
                w.last_reopen = 0.0
        elif key == "closures" and w:
            w.closures = val
            if val:
                w.last_closures = 0.0
        elif key == "amplify" and w:
            w.amplify = val
        elif key == "llm_scrub":
            claudit.LLM_SCRUB = val
        elif key == "burn_tokens":
            claudit.BURN_TOKENS = val
            if val:
                claudit.LLM_SCRUB = True
        elif key == "gate":
            cs.GATE = val
        elif key == "report_harness":
            cs.REPORT_HARNESS = val
        elif key == "dwell_seconds":
            cs.DWELL_SECONDS = int(val)
        elif key == "llm_model":
            claudit.LLM_MODEL = str(val)
            self._refresh_llm_cost()
        elif key == "llm_effort":
            claudit.LLM_EFFORT = str(val)
        elif key == "llm_engine":
            claudit.LLM_ENGINE = str(val)
        elif key == "usage_guard_pct":
            claudit.USAGE_GUARD_PCT = int(val)
        elif key == "interval" and w:
            w.interval = float(val)
        cfg = cs.load_config()
        cfg[key] = val
        cs.save_config(cfg)
        if key == "watchdog" and val:              # persist first, THEN start (it reads the flag)
            spawn_watchdog()
        self._sync_setting_ui(key, val)
        shown = (f"{int(val) // 60} min" if key == "dwell_seconds" else val)
        self._log(f"⚙ {key.replace('_', ' ')} → {shown}")

    def _sync_setting_ui(self, key, val):
        """Keep the tray menu, the settings switches, and dependent toggles in sync after any change."""
        trays = {"auto": "act_auto", "backfill": "act_backfill", "defend": "act_defend",
                 "reopen": "act_reopen", "closures": "act_closures", "dwell_autofile": "act_dwell",
                 "llm_scrub": "act_llm"}
        a = getattr(self, trays.get(key, ""), None)
        if a is not None and a.isChecked() != bool(val):
            a.blockSignals(True)
            a.setChecked(bool(val))
            a.blockSignals(False)
        sw = getattr(self, "_toggles", {}).get(key)
        if isinstance(val, bool) and sw is not None and sw.isChecked() != val:
            sw.set_silent(val)
        if key in ("dwell_autofile", "burn_tokens") and val:     # both force PII scrubbing on
            if self._toggles.get("llm_scrub"):
                self._toggles["llm_scrub"].set_silent(True)
            if getattr(self, "act_llm", None):
                self.act_llm.blockSignals(True)
                self.act_llm.setChecked(True)
                self.act_llm.blockSignals(False)

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
        bmute = QtWidgets.QPushButton("🔇 Edit mute list…")
        bmute.setToolTip("Manage the local mute.txt — findings containing these terms are never filed or sent to an LLM")
        bmute.clicked.connect(self._edit_mute)
        brow.addWidget(b)
        brow.addWidget(bscrub)
        brow.addWidget(bmute)
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
                with open(hp) as fh:
                    hist = json.load(fh)
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
                svg = b""
                if os.path.exists(sp):
                    with open(sp, "rb") as fh:
                        svg = fh.read()
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
        deduped = _snap(self.state.get("__deduped__"))
        reopened_map = _snap(self.state.get("__reopened__"))
        closures = _snap(self.state.get("__closures__"))
        swept_def = _snap(self.state.get("__swept_defended__"))
        folded = _snap(self.state.get("__folded__"))
        canon_dups = {}                 # open canonical -> the dup numbers merged into it
        for n, i in closures.items():
            if i.get("kind") == "merged" and i.get("into"):
                canon_dups.setdefault(str(i["into"]), []).append(n)
        chain_of = self._chain_map()    # issue number -> work-session chain key (for the graph gutter)
        model_of = {str(rec.get("issue")): rec.get("model", "")
                    for rec in cs.load_issue_rows() if rec.get("issue")}   # which model flagged it

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
                rows.append(("9999", "queued", "QUEUED", "you", "—", title, "", "", None,
                             cs.flag_model(f.get("block_text", ""))))

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
                frac = min(max((now - t0) / max(cs.DWELL_SECONDS, 1), 0.0), 1.0)
                rows.append(("9998", "dwelling", "⏳ DWELL", "you",
                             f"files in ~{mins}m\x1f{frac:.3f}",   # \x1f: ring fraction for the delegate
                             f"{title}  ·  {chain}", "", chain, proj,
                             cs.flag_model(fnd.get("block_text", "")) if fnd else ""))

        for it in self.community:
            st = it.get("state", "").lower()
            auth_raw = it.get("author")
            author = (auth_raw.get("login", "—") if isinstance(auth_raw, dict)
                      else auth_raw if isinstance(auth_raw, str) else "—")
            title = it.get("title", "")
            if "[harness]" in title.lower():
                continue                     # harness = withdrawn auto-classifier noise; never list it
            if scope == "Mine only" and self.me and author != self.me:
                continue
            if statef == "Open only" and st != "open":
                continue
            if statef == "Closed only" and st != "closed":
                continue
            mdl = model_of.get(str(it.get("number")), "") or (
                "Fable 5" if "fable 5" in title.lower() else "")
            if kindf == "Fable 5":
                if "fable" not in mdl.lower():
                    continue
            elif kindf != "All kinds" and f"[{kindf}]" not in title.lower():
                continue
            is_defended = deduped.get(str(it["number"])) == "not-duplicate"
            if dedupf == "Defended" and not is_defended:
                continue
            if dedupf == "Not defended" and is_defended:
                continue
            cinfo = closures.get(str(it["number"]))
            is_canon = str(it["number"]) in canon_dups
            if dedupf == "Swept" and (not cinfo or cinfo.get("kind") != "swept"):
                continue
            if dedupf == "Merged" and not (is_canon or (cinfo and cinfo.get("kind") == "merged")):
                continue
            if needle and needle not in title.lower() and needle not in str(it.get("number", "")):
                continue
            created = fmt_ts(it.get("createdAt", ""))
            ded = deduped.get(str(it["number"]))
            reopened = reopened_map.get(str(it["number"]))
            label = f"#{it['number']}" + (" 👎✓" if ded == "not-duplicate" else "")
            if cinfo and cinfo.get("kind") == "swept":
                label += " 🧹"
            elif cinfo and cinfo.get("kind") == "merged":
                label += " ⇥"
            if is_canon:
                label += " 📎"
            why = ""
            if st == "closed":
                reason = (it.get("stateReason") or "").lower()
                why = {"not_planned": "closed: not planned (often = duplicate)",
                       "duplicate": "closed as DUPLICATE — not actually a dupe",
                       "completed": "closed: completed"}.get(reason, f"closed ({reason or '—'})")
                if cinfo and cinfo.get("kind") == "swept":
                    why = f"🧹 swept by the inactivity bot {(cinfo.get('at') or '')[:10]}"
                    dd = swept_def.get(str(it["number"]))
                    if dd == "reopened":
                        why += " · ♻ reopened by ClAudit"
                    elif dd:
                        why += f" · 🛡 still-relevant note posted; tracked in #{cs.umbrella_num()}"
                elif cinfo and cinfo.get("kind") == "merged":
                    tgt = cinfo.get("into")
                    why = (f"⇥ merged into #{tgt or '?'}"
                           + (f" by {cinfo.get('by')}" if cinfo.get("by") else ""))
                    if tgt and str(it["number"]) in {str(x) for x in folded.get(str(tgt), [])}:
                        why += " · 🛡 request IDs folded onto the canonical"
                if reopened and not str(reopened).startswith("review"):
                    why += " · ♻ reopened by ClAudit"
            if is_canon:
                sibs = sorted(canon_dups[str(it["number"])], key=int)
                extra = (f"📎 canonical — {len(sibs)} merged sibling(s): "
                         + ", ".join(f"#{s}" for s in sibs[:6])
                         + (" …" if len(sibs) > 6 else ""))
                why = f"{why} · {extra}" if why else extra
            rows.append((it.get("createdAt", ""), st, label, author, created, title,
                         it.get("url", ""), why, chain_of.get(str(it.get("number"))), mdl))

        rows.sort(key=lambda r: r[0], reverse=True)   # newest first
        graph, nlanes_ = self._build_chain_graph(rows, DOT)
        self._chain_delegate.set_data(graph, nlanes_)

        self.table.setRowCount(len(rows))
        for r, (_, _st, num, author, created, title, url, why, _chain, mdl) in enumerate(rows):
            is_claudit = "claudit" in title.lower() or title.lower().startswith(("[cyber]", "[aup]", "[bug]"))
            if author == "you" or (self.me and author == self.me):
                owner = "#b794f6"          # yours = purple
            elif is_claudit:
                owner = "#5eead4"          # another ClAudit user = teal
            else:
                owner = None
            tip = why or ("Click to open in browser" if url else "Not filed yet")
            for c, val in enumerate(["", num, author, created, title]):   # col 0 painted by the delegate
                frac = None
                if c == 3 and "\x1f" in val:              # dwell row: split off the countdown fraction
                    val, _, fs = val.partition("\x1f")
                    try:
                        frac = float(fs)
                    except ValueError:
                        frac = None
                item = QtWidgets.QTableWidgetItem(val)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, url)
                if frac is not None:
                    item.setData(DwellRingDelegate.FRAC_ROLE, frac)
                if c == 4 and mdl:
                    item.setData(TitleChipDelegate.MODEL_ROLE, mdl)
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
        if getattr(self, "total_open", -1) >= 0:
            nopen = self.total_open       # exact search count — the fetched list is capped at 1000,
                                          # which froze the tray pill at the cap (was stuck at 600)
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
        csum = cs.closure_summary(self.state)
        today = datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        nday = sum(1 for it in c if (it.get("createdAt", "") or "")[:10] == today)
        self.stats_bar.setText(
            f"<span style='color:#3fb950'>● {nopen} open</span> &nbsp; "
            f"<span style='color:#a371f7'>● {real_closed} closed by Anthropic</span> &nbsp;|&nbsp; "
            f"cyber {kinds['cyber']} · aup {kinds['aup']} &nbsp;|&nbsp; "
            f"<span style='color:#b58a8a'>⊘ {harness_withdrawn} harness withdrawn (false)</span> &nbsp;|&nbsp; "
            f"🛡 {defended} &nbsp; ♻ {reopened} &nbsp; "
            f"<span style='color:#d29922'>🧹 {csum['swept']} swept</span> &nbsp; "
            f"<span style='color:#a371f7'>⇥ {csum['merged']} merged</span> &nbsp; "
            f"<span style='color:#5eead4'>+{nday} today</span>")
        self.stats_bar.setToolTip(
            f"{real_closed} cyber/aup reports actually closed by Anthropic.\n"
            f"{harness_withdrawn} harness (auto-mode-classifier) reports were ClAudit's own false "
            "reports, withdrawn as log-only — they do NOT count as Anthropic closing a ticket.\n"
            f"🧹 {csum['swept']} swept by the inactivity bot ({csum['defended']} answered; tracked "
            f"in umbrella #{csum['umbrella']}).\n"
            f"⇥ {csum['merged']} merged into canonicals by a maintainer ({csum['folded']} request-ID "
            "set(s) folded).")
        if hasattr(self, "breakdown"):
            self.breakdown.set_data([
                ("open", nopen, "#3fb950"),
                ("closed (Anthropic)", real_closed, "#a371f7"),
                ("cyber", kinds["cyber"], "#4aa3ff"),
                ("aup", kinds["aup"], "#d29922"),
                ("harness withdrawn (false)", harness_withdrawn, "#b58a8a"),
                ("defended", defended, "#5eead4"),
            ])
        # header sparkline: cumulative reports/day over the last 30 days
        if hasattr(self, "spark"):
            per_day = {}
            for it in c:
                d = (it.get("createdAt", "") or "")[:10]
                if d:
                    per_day[d] = per_day.get(d, 0) + 1
            base = datetime.date.today() - datetime.timedelta(days=29)
            cum, series = sum(v for d, v in per_day.items() if d < base.isoformat()), []
            for i in range(30):
                cum += per_day.get((base + datetime.timedelta(days=i)).isoformat(), 0)
                series.append(cum)
            self.spark.set_series(series)
        self._badge_tray(nopen)

    def _badge_tray(self, count):
        """Overlay the live open-FP count on the tray icon (cached — repaints only when it changes)."""
        if count == getattr(self, "_badge_n", None) or not os.path.exists(cs.ICON):
            return
        self._badge_n = count
        pm = QtGui.QIcon(cs.ICON).pixmap(64, 64)
        if count > 0:
            p = QtGui.QPainter(pm)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            txt = str(count) if count < 1000 else "1k+"
            f = QtGui.QFont(self.font())
            f.setPixelSize(26)
            f.setBold(True)
            p.setFont(f)
            wtx = QtGui.QFontMetrics(f).horizontalAdvance(txt) + 14
            r = QtCore.QRectF(64 - wtx, 34, wtx, 28)
            p.setPen(QtCore.Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor("#d63031"))
            p.drawRoundedRect(r, 13, 13)
            p.setPen(QtGui.QColor("#ffffff"))
            p.drawText(r, QtCore.Qt.AlignmentFlag.AlignCenter, txt)
            p.end()
        self.tray.setIcon(QtGui.QIcon(pm))

    def _on_community(self, items, me, nopen_total=-1):
        self.community, self.me = items, me
        self.total_open = nopen_total
        self._repopulate()
        if self.SCREENSHOT_DIR and not getattr(self, "_shot", False):
            self._shot = True
            QtCore.QTimer.singleShot(4000, self._take_screenshots)   # let stats/poll fetchers land

    def _take_screenshots(self):
        """Save docs/screenshot*.png from the live window (README), one per tab, then quit."""
        names = ["screenshot.png", "screenshot-project.png", "screenshot-activity.png",
                 "screenshot-settings.png"]
        os.makedirs(self.SCREENSHOT_DIR, exist_ok=True)
        self.resize(1100, 680)
        self.show()
        for i, name in enumerate(names):
            self.tabs.setCurrentIndex(i)
            loop = QtCore.QEventLoop()
            QtCore.QTimer.singleShot(1200, loop.quit)     # animations settle, fades finish
            loop.exec()
            path = os.path.join(self.SCREENSHOT_DIR, name)
            ok = self.grab().save(path)
            print(("wrote " if ok else "FAILED ") + path)
        QtWidgets.QApplication.quit()

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
        cinfo = _snap(self.state.get("__closures__")).get(str(num))
        if cinfo and cinfo.get("kind") == "merged" and cinfo.get("into"):
            tgt = int(cinfo["into"])
            m.addAction(f"📎 Open canonical #{tgt}", lambda: self._open_detail_num(tgt))
        if cinfo and cinfo.get("kind") == "swept":
            m.addAction(f"☂ Open umbrella #{cs.umbrella_num()}",
                        lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(
                            f"https://github.com/{self.repo}/issues/{cs.umbrella_num()}")))
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
        if kind == "closures":   # closure defender: swept notes + merged req-ID folds
            self.tray.showMessage("ClAudit · 🧹 CLOSURES",
                                  f"Closure defender acted on {n} issue(s): still-relevant notes on "
                                  f"bot-swept reports (umbrella #{cs.umbrella_num()}) and request-ID "
                                  "folds onto merge canonicals.", icon)
            self._log(f"🧹 closure defender acted on {n} issue(s)")
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
        if kind == "amplify":    # 👍 on other users' false-positive issues
            self.tray.showMessage("ClAudit · 👍 AMPLIFIED",
                                  f"👍 {n} open false-positive issue(s) from other ClAudit users.", icon)
            self._log(f"👍 amplified {n} community false-positive issue(s)")
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
        try:                                       # tell the watchdog this exit is intentional
            open(QUIT_FLAG, "w").close()
        except OSError:
            pass
        if self.watcher:
            self.watcher.stop()
            self.watcher.wait(1500)
        QtWidgets.QApplication.quit()
