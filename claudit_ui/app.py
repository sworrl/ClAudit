"""ClAudit GUI: app (split out of claudit_gui.py; see that file for the entry point)."""
import argparse
import os
import sys
from PyQt6 import QtGui, QtWidgets
import claudit
import claudit_scan as cs
from .common import STYLE, run_watchdog, spawn_watchdog
from .main_window import Main


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
    p.add_argument("--version", action="version", version=f"ClAudit {cs.__version__}")
    p.add_argument("--screenshot", metavar="DIR",
                   help="(docs) build the window read-only, save one PNG per tab into DIR, exit. "
                        "Files nothing; bypasses the single-instance lock. Use QT_QPA_PLATFORM=offscreen")
    p.add_argument("--watchdog", action="store_true",
                   help="(internal) run as the crash-recovery supervisor, not the GUI")
    args = p.parse_args()

    if args.watchdog:                              # detached supervisor mode — no Qt, just watch + relaunch
        run_watchdog()
        return

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
    # persisted Settings-tab choices override argparse defaults so they survive restart
    if "auto" in cfg:
        args.auto = bool(cfg["auto"])
    if "backfill" in cfg:
        args.backfill = bool(cfg["backfill"])
    if "interval" in cfg:
        args.interval = float(cfg["interval"])
    if "gate" in cfg:
        cs.GATE = bool(cfg["gate"])
    if "report_harness" in cfg:
        cs.REPORT_HARNESS = bool(cfg["report_harness"])
    if "llm_model" in cfg:
        claudit.LLM_MODEL = str(cfg["llm_model"])
    if "llm_effort" in cfg:
        claudit.LLM_EFFORT = str(cfg["llm_effort"])
    if "llm_engine" in cfg:
        claudit.LLM_ENGINE = str(cfg["llm_engine"])
    if "agy_project" in cfg:
        claudit.AGY_PROJECT = str(cfg["agy_project"] or "")
    if "agy_review_model" in cfg:
        claudit.AGY_REVIEW_MODEL = str(cfg["agy_review_model"] or "")
    if "usage_guard_pct" in cfg:
        claudit.USAGE_GUARD_PCT = int(cfg["usage_guard_pct"])
    if args.screenshot:
        Main.SCREENSHOT_DIR = args.screenshot
    elif not cs.acquire_singleton():
        QtWidgets.QMessageBox.warning(None, "ClAudit",
                                      "Another ClAudit watcher is already running.\nThis instance will exit.")
        return
    w = Main(args.repo, args.interval, args.auto, args.backfill, args.backfill_interval,
             args.backfill_max)
    if not args.hidden:
        w.show()
    if cfg.get("watchdog") and not args.screenshot:   # keep-alive supervisor across crashes
        spawn_watchdog()
    sys.exit(app.exec())
