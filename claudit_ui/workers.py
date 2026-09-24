"""ClAudit GUI: workers (split out of claudit_gui.py; see that file for the entry point)."""
import json
import os
import subprocess
import sys
import time
from PyQt6 import QtCore
import claudit
import claudit_scan as cs
from .common import REPO_DIR, STATE_LOCK, _code_changed, git_commit, git_pull_if_behind


class UpdateChecker(QtCore.QThread):
    """Off-thread: pull new commits from GitHub (if clean+behind), then flag if CODE moved. A pull
    that only refreshes docs/counter/poll/trend updates the checkout but does not restart the app.
    A pip/wheel install has no .git to pull, so it checks the latest GitHub Release instead and
    reports a newer version (the tray shows it once)."""
    updated = QtCore.pyqtSignal()
    newer = QtCore.pyqtSignal(str)

    def __init__(self, launch_head):
        super().__init__()
        self.launch_head = launch_head

    def run(self):
        if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
            latest = cs.latest_release_version()
            if latest and cs.version_tuple(latest) > cs.version_tuple(cs.__version__):
                self.newer.emit(latest)
            return
        git_pull_if_behind()                 # auto-update from GitHub (stays current either way)
        cur = git_commit()
        if cur and self.launch_head and cur != self.launch_head and _code_changed(self.launch_head, cur):
            self.updated.emit()              # restart only when real code changed

# ----------------------------- background workers -----------------------------
class Watcher(QtCore.QThread):
    acted = QtCore.pyqtSignal(int, str)    # (count, kind: "auto"|"queued"|"backfill"|"defend")

    DEFEND_INTERVAL = 240                   # seconds between dedup-defender sweeps (fast: 1 search/pass)
    CLOSURE_INTERVAL = 900                  # seconds between closure-defender sweeps (sweeps + merges)

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
        self.closures = True               # closure defender: answer bot stale-sweeps (still-relevant
                                           # note -> umbrella) + fold merged dups onto canonicals
        self.amplify = False               # solidarity 👍 on other users' FP issues; opt-in (dormant solo)
        self._transient_mark = ""          # newest overloaded/rate-limit ts we've alerted on
        self.bf_done = 0
        self.last_live = 0.0
        self.last_bf = 0.0
        self.last_defend = 0.0
        self.last_reopen = 0.0
        self.last_closures = 0.0
        self.last_amplify = 0.0
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
                        d = cs.defend_all(self.repo, self.state, compose=claudit.BURN_TOKENS)
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
            # CLOSURES: answer stale-bot sweeps + fold maintainer merges (idempotent, paced inside).
            # Capped per pass: a cold state (hundreds of un-answered sweeps) must not hold
            # STATE_LOCK for the whole backlog — each tick takes a bounded slice instead.
            if self.closures and now - self.last_closures >= self.CLOSURE_INTERVAL:
                self.last_closures = now
                try:
                    with STATE_LOCK:
                        cc = cs.defend_closures(self.repo, self.state, limit=25,
                                                compose=claudit.BURN_TOKENS)
                        if cc:
                            cs.update_umbrella(self.repo, self.state)
                    if cc:
                        self.acted.emit(cc, "closures")
                        if cc >= 25:                   # backlog remains — take the next slice soon
                            self.last_closures = now - self.CLOSURE_INTERVAL + 120
                except Exception as e:
                    print("closure defender error:", e, file=sys.stderr)
            # AMPLIFY: opt-in solidarity 👍 on OTHER users' open false-positive issues (every 30 min).
            if self.amplify and now - self.last_amplify >= 1800:
                self.last_amplify = now
                try:
                    with STATE_LOCK:
                        a = cs.amplify_community(self.repo, self.state)
                    if a:
                        self.acted.emit(a, "amplify")
                except Exception as e:
                    print("amplify error:", e, file=sys.stderr)
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
    fetched = QtCore.pyqtSignal(list, str, int)

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
        by_num = {}
        # 1. Fetch user's own issues (all states, up to 2000)
        try:
            out_me = subprocess.run(
                ["gh", "issue", "list", "-R", self.repo, "--author", "@me", "--state", "all",
                 "--limit", "2000", "--json", "number,state,title,author,url,createdAt"],
                capture_output=True, text=True, check=True).stdout
            for it in json.loads(out_me or "[]"):
                by_num[it["number"]] = it
        except Exception as e:
            print("my issues fetch failed:", e, file=sys.stderr)
        # 2. Fetch community ClAudit issues (all authors, up to 2000)
        try:
            out_comm = subprocess.run(
                ["gh", "issue", "list", "-R", self.repo, "--state", "all", "--limit", "2000",
                 "--search", '"Filed automatically by ClAudit"',
                 "--json", "number,state,title,author,url,createdAt"],
                capture_output=True, text=True, check=True).stdout
            for it in json.loads(out_comm or "[]"):
                by_num[it["number"]] = it
        except Exception as e:
            print("community fetch failed:", e, file=sys.stderr)
        items = list(by_num.values())
        items.sort(key=lambda x: x.get("number", 0), reverse=True)
        # exact open count for tray pill
        nopen = -1
        try:
            def _count(q):
                return int(subprocess.run(
                    ["gh", "api", "-X", "GET", "search/issues", "-f", f"q={q}",
                     "-f", "per_page=1", "--jq", ".total_count"],
                    capture_output=True, text=True, check=True).stdout.strip())
            total = _count(f'repo:{self.repo} is:issue is:open "Filed automatically by ClAudit"')
            harness = _count(f'repo:{self.repo} is:issue is:open "[bug][harness]" in:title')
            nopen = max(0, total - harness)
        except Exception as e:
            print("open-count fetch failed:", e, file=sys.stderr)
        self.fetched.emit(items, me, nopen)

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
                 ("[.[] | {id:.id, reason:.reason, title:.subject.title, type:.subject.type, "
                 "url:.subject.url, repo:.repository.full_name}]")],
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

class ClosureWorker(QtCore.QThread):
    """One live closure-defender pass: classify fresh closes, answer swept ones (still-relevant
    note -> umbrella), fold merged dups' request IDs onto canonicals, refresh the umbrella."""
    progress = QtCore.pyqtSignal(int)         # issue/canonical number acted on
    finished_n = QtCore.pyqtSignal(int)       # total actions this pass

    def __init__(self, state, repo):
        super().__init__()
        self.state, self.repo = state, repo

    def run(self):
        n = 0
        try:
            with STATE_LOCK:
                n = cs.defend_closures(self.repo, self.state, compose=claudit.BURN_TOKENS,
                                       on_done=lambda num, info: self.progress.emit(num))
                if n:
                    cs.update_umbrella(self.repo, self.state)
        except Exception as e:
            print("closure worker error:", e, file=sys.stderr)
        self.finished_n.emit(n)

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
                                ("{stars:.stargazers_count, forks:.forks_count, watchers:.subscribers_count, "
                                "issues:.open_issues_count, updated:.pushed_at}")],
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
            m_merge = cs.MERGE_TARGET_RE.search(c.get("body", "") or "")
            if m_merge and "clos" in b:
                ev.append((c.get("createdAt", ""), "⇥", f"Merged into #{m_merge.group(1)} by {who}"))
            elif cs.STALE_CLOSE_RE.search(b):
                ev.append((c.get("createdAt", ""), "🧹", "Swept by the inactivity bot (stale close)"))
            elif cs._is_swept_defense(c.get("body", "")):
                ev.append((c.get("createdAt", ""), "🛡",
                           f"ClAudit answered the sweep — tracked in #{cs.umbrella_num()}"))
            elif cs._is_fold_note(c.get("body", "")):
                ev.append((c.get("createdAt", ""), "📎", "ClAudit folded merged siblings' Request IDs here"))
            elif "possible duplicate" in b or "closed as a duplicate" in b:
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

class UsageFetcher(QtCore.QThread):
    """Off-thread: refresh the live plan usage snapshot (Anthropic's OAuth usage endpoint, the same
    numbers Claude Code's /usage shows). Emits the parsed dict, or {} when there is no login."""
    got = QtCore.pyqtSignal(dict)

    def run(self):
        try:
            self.got.emit(claudit.plan_usage() or {})
        except Exception as e:
            print("usage fetch failed:", e, file=sys.stderr)
            self.got.emit({})
