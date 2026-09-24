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
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Re-exports: the code lives in claudit_ui/; tests and older scripts import names from here.
from claudit_ui.common import (
    GUI_SCRIPT,
    INTRO_SPAN,
    KIND_VIZ,
    LANE_ORDER,
    QSvgWidget,
    QUIT_FLAG,
    REPO_DIR,
    SPAWN_DUR,
    STATE_LOCK,
    STYLE,
    WATCHDOG_PID,
    _HAVE_SVG,
    _code_changed,
    _ease_out_back,
    _git,
    _iso_epoch,
    _launch,
    _pid_alive,
    _read_pid,
    _rp,
    _snap,
    chain_color,
    fmt_ts,
    git_commit,
    git_pull_if_behind,
    run_watchdog,
    spawn_watchdog,
    watchdog_running,
)
from claudit_ui.widgets import (
    AnimatedBanner,
    BreakdownBars,
    ChainGraphDelegate,
    ChronoLine,
    DwellRingDelegate,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLVertexArrayObject,
    QOpenGLWidget,
    ShaderBanner,
    Sparkline,
    TitleChipDelegate,
    ToggleSwitch,
    _HAVE_GL,
    make_banner,
)
from claudit_ui.workers import (
    ClosureWorker,
    CommunityFetcher,
    DedupWorker,
    DefendAllWorker,
    IssueDetailFetcher,
    NotifyWatcher,
    PollWorker,
    ReopenOneWorker,
    RepoStatsFetcher,
    Reporter,
    UpdateChecker,
    UsageFetcher,
    Watcher,
)
from claudit_ui.dialogs import (
    IssueDetailDialog,
    MuteListDialog,
    ScrubListDialog,
)
from claudit_ui.main_window import (
    Main,
)
from claudit_ui.app import (
    main,
)


if __name__ == "__main__":
    main()
