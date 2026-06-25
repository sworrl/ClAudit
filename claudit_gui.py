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
import json
import os
import subprocess
import sys
import threading

from PyQt6 import QtCore, QtGui, QtWidgets

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claudit_scan as cs  # noqa: E402

STATE_LOCK = threading.Lock()

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

    def run(self):
        import time as _t
        with STATE_LOCK:
            cs.ensure_baseline(self.state)
        last_bf = 0.0
        while self._run:
            try:
                with STATE_LOCK:
                    if self.auto:
                        n = cs.auto_cycle(self.state, self.repo, 1, lambda *a: None)
                    else:
                        n = cs.monitor_cycle(self.state, lambda fresh: None)
                if n:
                    self.acted.emit(n, "auto" if self.auto else "queued")
                capped = self.backfill_max and self.bf_done >= self.backfill_max
                if self.backfill and not capped and _t.monotonic() - last_bf >= self.backfill_interval * 60:
                    last_bf = _t.monotonic()
                    with STATE_LOCK:
                        b = cs.backfill_step(self.state, self.repo, 1, lambda *a: None)
                    if b:
                        self.bf_done += b
                        self.acted.emit(b, "backfill")
            except Exception as e:
                print("watcher error:", e, file=sys.stderr)
            for _ in range(int(self.interval)):
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


class StatusFetcher(QtCore.QThread):
    """Fetch GitHub open/closed state for the authored issues, in one gh call."""
    fetched = QtCore.pyqtSignal(dict)

    def __init__(self, repo):
        super().__init__()
        self.repo = repo

    def run(self):
        states = {}
        try:
            out = subprocess.run(
                ["gh", "issue", "list", "-R", self.repo, "--author", "@me",
                 "--state", "all", "--limit", "500", "--json", "number,state,title"],
                capture_output=True, text=True, check=True).stdout
            for it in json.loads(out):
                states[str(it["number"])] = (it["state"], it["title"])
        except Exception as e:
            print("status fetch failed:", e, file=sys.stderr)
        self.fetched.emit(states)


# --------------------------------- main window --------------------------------
class Main(QtWidgets.QMainWindow):
    COLS = ["", "Type", "Issue / Queue", "GitHub", "Req IDs", "Hits", "Detail"]

    def __init__(self, repo, interval, auto, backfill, backfill_interval, backfill_max):
        super().__init__()
        self.repo, self.state = repo, cs.load_state()
        self.findings, self.gh_states = {}, {}
        self.setWindowTitle(f"ClAudit v{cs.__version__} — false-positive blocks")
        self.resize(880, 460)
        if os.path.exists(cs.ICON):
            self.setWindowIcon(QtGui.QIcon(cs.ICON))

        self.table = QtWidgets.QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.doubleClicked.connect(self._show_detail)
        self.empty = QtWidgets.QLabel("🔎  Watching for false-positive blocks…\nNothing filed yet.",
                                      self.table.viewport())
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("color:#6b7280; font-size:15px; background:transparent;")

        self.status = QtWidgets.QLabel("Loading…")
        btn_refresh = QtWidgets.QPushButton("Refresh")
        btn_refresh.clicked.connect(self.refresh)
        self.btn_report = QtWidgets.QPushButton("Report pending")
        self.btn_report.setObjectName("primary")
        self.btn_report.clicked.connect(self.report_pending)

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(self.status, 1)
        bar.addWidget(self.btn_report)
        bar.addWidget(btn_refresh)
        root = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(root)
        lay.addWidget(self.table, 1)
        lay.addLayout(bar)
        self.setCentralWidget(root)

        self._build_tray(auto, backfill)
        self.watcher = Watcher(self.state, repo, interval, auto, backfill, backfill_interval, backfill_max)
        self.watcher.acted.connect(self._on_acted)
        self.watcher.start()
        self.refresh()

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
        self.watcher.auto = on
        self.tray.showMessage("ClAudit", "Auto-post ENABLED — new blocks file automatically."
                              if on else "Auto-post disabled — blocks queue for review.")

    def _toggle_backfill(self, on):
        self.watcher.backfill = on
        self.tray.showMessage("ClAudit", f"Backfill ENABLED — drip-filing old backlog "
                              f"(1 / {self.watcher.backfill_interval:g} min)." if on
                              else "Backfill paused.")

    # ---- data ----
    def refresh(self):
        threading.Thread(target=self._scan_then, daemon=True).start()
        f = StatusFetcher(self.repo)
        f.fetched.connect(self._on_states)
        f.start()
        self._fetcher = f  # keep ref

    def _scan_then(self):
        with STATE_LOCK:
            self.findings = cs.scan()[0]
        QtCore.QMetaObject.invokeMethod(self, "_repopulate", QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.pyqtSlot()
    def _repopulate(self):
        rows = []
        pend = set(cs.pending_sigs(self.state))
        for sig in pend:                                   # queued, not yet filed
            f = self.findings.get(sig, {})
            rows.append(("queued", f.get("kind", "?"), "QUEUED", "—",
                         len(cs.reqs_of(f)) if f else 0, len(f.get("occ", [])) if f else 0, sig))
        for sig, rec in self.state.items():                # filed
            if sig.startswith("__") or not rec.get("issue"):
                continue
            num = rec["issue"]
            gh_state, _ = self.gh_states.get(num, ("?", ""))
            rows.append((gh_state.lower(), rec.get("kind", "?"), f"#{num}", gh_state,
                         len(rec.get("reqs", [])), len(self.findings.get(sig, {}).get("occ", [])), sig))

        self.table.setRowCount(len(rows))
        for r, (st, kind, ref, ghs, nreq, hits, sig) in enumerate(rows):
            dot = {"open": "🟢", "closed": "🟣", "queued": "🟡"}.get(st, "⚪")
            for c, val in enumerate([dot, kind, ref, ghs, str(nreq), str(hits), "▸"]):
                item = QtWidgets.QTableWidgetItem(val)
                item.setData(QtCore.Qt.ItemDataRole.UserRole, sig)
                self.table.setItem(r, c, item)
        self.table.resizeColumnsToContents()
        self.empty.setGeometry(self.table.viewport().rect())
        self.empty.setVisible(len(rows) == 0)
        npend = len(pend)
        nfiled = sum(1 for s, rc in self.state.items() if not s.startswith("__") and rc.get("issue"))
        self.status.setText(f"{npend} queued · {nfiled} reported · repo {self.repo}")
        self.btn_report.setEnabled(npend > 0)
        self.act_pending.setText(f"Report {npend} pending")
        self.act_pending.setEnabled(npend > 0)

    def _on_states(self, states):
        self.gh_states = states
        self._repopulate()

    def _on_acted(self, n, kind):
        icon = (QtGui.QIcon(cs.ICON) if os.path.exists(cs.ICON)
                else QtWidgets.QSystemTrayIcon.MessageIcon.Information)
        msg = {"auto": f"Auto-filed {n} new false-positive block(s) to {self.repo}.",
               "queued": f"{n} new false-positive block(s) queued — use ‘Report pending’.",
               "backfill": f"Backfilled {n} old block(s) to {self.repo}."}.get(kind, f"{n} filed.")
        self.tray.showMessage("ClAudit", msg, icon)
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

    def _show_detail(self, idx):
        sig = self.table.item(idx.row(), 0).data(QtCore.Qt.ItemDataRole.UserRole)
        f = self.findings.get(sig, {})
        rec = self.state.get(sig, {})
        reqs = "\n".join(f"  • {o['req']}  ({o['ts']})" for o in cs.reqs_of(f)) or "  (none captured)"
        block = cs.TOKEN.sub("token=[SCRUBBED]", f.get("block_text", "(n/a)"))[:600]
        hint = (cs.scrub(f.get("prompt", ""))[0])[:400]
        url = rec.get("url") or "(not filed yet)"
        text = (f"Type: {f.get('kind', rec.get('kind', '?'))}\n"
                f"Issue: {('#' + rec['issue']) if rec.get('issue') else 'QUEUED (not filed)'}\n"
                f"URL: {url}\n\nRequest IDs:\n{reqs}\n\nBlock message:\n  {block}\n\n"
                f"Prompt hint (unreliable):\n  {hint}")
        dlg = QtWidgets.QMessageBox(self)
        dlg.setWindowTitle("Block detail")
        dlg.setText(text)
        if rec.get("url"):
            ob = dlg.addButton("Open on GitHub", QtWidgets.QMessageBox.ButtonRole.ActionRole)
            ob.clicked.connect(lambda: QtGui.QDesktopServices.openUrl(QtCore.QUrl(rec["url"])))
        dlg.addButton(QtWidgets.QMessageBox.StandardButton.Close)
        dlg.exec()

    def closeEvent(self, e):     # close to tray, keep watching
        e.ignore()
        self.hide()
        self.tray.showMessage("ClAudit", "Still watching in the tray.")

    def _quit(self):
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
                   help="minutes between backfilled issues (default 10)")
    p.add_argument("--backfill-max", dest="backfill_max", type=int, default=0,
                   help="stop backfilling after N issues (0 = no cap)")
    p.add_argument("--hidden", action="store_true", help="start minimized to tray")
    args = p.parse_args()

    app = QtWidgets.QApplication(sys.argv)
    app.setStyleSheet(STYLE)
    app.setQuitOnLastWindowClosed(False)
    if not cs.acquire_singleton():
        QtWidgets.QMessageBox.warning(None, "ClAudit",
                                      "Another ClAudit watcher is already running.\nThis instance will exit.")
        return
    w = Main(args.repo, args.interval, args.auto, args.backfill, args.backfill_interval, args.backfill_max)
    if not args.hidden:
        w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
