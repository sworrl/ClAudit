"""ClAudit GUI: dialogs (split out of claudit_gui.py; see that file for the entry point)."""
import os
from PyQt6 import QtCore, QtGui, QtWidgets
import claudit
import claudit_scan as cs
from .common import fmt_ts
from .workers import DedupWorker, IssueDetailFetcher


class ScrubListDialog(QtWidgets.QDialog):
    """View / add / remove terms in the local PII denylist (~/.claude/claudit/scrub.txt)."""
    PATH = os.path.expanduser("~/.claude/claudit/scrub.txt")
    TITLE = "PII denylist"
    INFO = ("Names, orgs, hostnames, codenames — anything the regex can't know — are scrubbed from "
            "<b>every</b> report before it's filed. Word-boundary, case-insensitive. This file is "
            "<b>local only</b> and never committed.")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.TITLE)
        self.resize(440, 480)
        if os.path.exists(cs.ICON):
            self.setWindowIcon(QtGui.QIcon(cs.ICON))
        v = QtWidgets.QVBoxLayout(self)
        info = QtWidgets.QLabel(self.INFO)
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
            with open(self.PATH, encoding="utf-8") as fh:
                for line in fh:
                    t = line.strip()
                    if t and not t.startswith("#"):
                        self.lst.addItem(t)
        self.lst.sortItems()
        self.count.setText(f"{self.lst.count()} term(s) · {self.PATH}")

    def _save(self):
        terms = self._terms()
        os.makedirs(os.path.dirname(self.PATH), exist_ok=True)
        with open(self.PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(terms) + ("\n" if terms else ""))
        if self.PATH == ScrubListDialog.PATH:
            claudit._EXTRA = None          # invalidate cache so the running watcher reloads it
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

class MuteListDialog(ScrubListDialog):
    """View / add / remove terms in the mute list (~/.claude/claudit/mute.txt). Scrub redacts and
    still posts; a mute stops the report outright and keeps it away from every LLM CLI."""
    PATH = cs.MUTE_FILE
    TITLE = "Mute list"
    INFO = ("Findings whose block text, prompt, conversation lead-up, or project path contain one of "
            "these terms are <b>never filed, never composed, and never sent to any LLM</b>. Use it for "
            "work that must not be described publicly even in redacted form (active litigation, "
            "clients under NDA). Substring match, case-insensitive; picked up live, no restart.")

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
