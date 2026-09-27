"""Update monitor: pure logic, no Qt, no network (git and urllib are faked)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claudit_scan as cs  # noqa: E402
from claudit_ui import updater  # noqa: E402


class R:
    def __init__(self, out="", code=0, err=""):
        self.stdout, self.returncode, self.stderr = out, code, err


def _fake_git(answers):
    """answers: {first-arg-tuple-prefix: R}. Records every call."""
    calls = []

    def run(args, capture_output=True, text=True, timeout=0):
        calls.append(args)
        if args[0] != "git":
            return R("pip ok")
        sub = tuple(args[3:])
        for key, val in answers.items():
            if sub[:len(key)] == key:
                return val
        return R("")
    return run, calls


def test_check_git_mode_behind_clean(monkeypatch):
    monkeypatch.setattr(updater, "install_mode", lambda: "git")
    monkeypatch.setattr(updater, "latest_release", lambda **k: {
        "version": "2.9.0", "url": "https://github.com/sworrl/ClAudit/releases/tag/v2.9.0",
        "notes": "**Release notes**", "published": "2026-09-27"})
    run, calls = _fake_git({
        ("rev-parse", "--short", "HEAD"): R("abc1234\n"),
        ("rev-parse", "--abbrev-ref", "HEAD"): R("main\n"),
        ("fetch",): R(""),
        ("rev-parse", "--short", "origin/main"): R("def5678\n"),
        ("rev-list", "--left-right", "--count"): R("0\t3\n"),
        ("status", "--porcelain"): R(""),
        ("show", "origin/main:claudit_scan.py"): R('x = 1\n__version__ = "2.9.0"\n'),
        ("diff", "--name-only"): R("claudit_scan.py\nREADME.md\n"),
        ("log",): R("- chore: refresh poll + report counter\n- 2.9.0: thing\n"),
    })
    monkeypatch.setattr(updater.subprocess, "run", run)
    info = updater.check()
    assert info["mode"] == "git" and info["head"] == "abc1234" and info["remote_head"] == "def5678"
    assert (info["behind"], info["ahead"], info["dirty"]) == (3, 0, False)
    assert info["available"] is True and info["blocked"] == "" and info["code_changed"] is True
    assert info["remote_version"] == "2.9.0" and info["notes"] == "**Release notes**"   # release matches remote
    assert updater.summary(info) == "Update available: 2.9.0 (3 commits)"
    assert any(a[3] == "fetch" for a in calls)
    assert not any(a[3] == "fetch" for a in _fake_git({})[1])   # sanity: fresh recorder is empty
    # apply: a fast-forward pull, then "restart" advice
    run2, calls2 = _fake_git({("pull",): R("Updating abc1234..def5678\nFast-forward\n")})
    monkeypatch.setattr(updater.subprocess, "run", run2)
    ok, log = updater.apply_update(info)
    assert ok and "Fast-forward" in log and calls2[0][3:] == ["pull", "--ff-only", "origin", "main"]


def test_check_git_mode_blocked_dirty_or_diverged(monkeypatch):
    monkeypatch.setattr(updater, "install_mode", lambda: "git")
    monkeypatch.setattr(updater, "latest_release", lambda **k: {})
    base = {("rev-parse", "--short", "HEAD"): R("a\n"), ("rev-parse", "--abbrev-ref", "HEAD"): R("main\n"),
            ("rev-parse", "--short", "origin/main"): R("b\n"),
            ("show", "origin/main:claudit_scan.py"): R('__version__ = "2.9.1"\n'),
            ("diff", "--name-only"): R("docs/trend.svg\n"), ("log",): R("- 2.9.1: docs\n")}
    run, _ = _fake_git({**base, ("rev-list", "--left-right", "--count"): R("0\t1\n"),
                        ("status", "--porcelain"): R(" M claudit.py\n")})
    monkeypatch.setattr(updater.subprocess, "run", run)
    info = updater.check(fetch=False)
    assert info["available"] and info["dirty"] and info["blocked"].startswith("the working tree has uncommitted")
    assert info["code_changed"] is False                     # docs-only diff: no restart needed
    assert info["notes"] == "- 2.9.1: docs"                  # commit subjects, poll-bot lines dropped
    assert updater.summary(info).startswith("Update 2.9.1 waiting: the working tree")
    ok, log = updater.apply_update(info)
    assert ok is False and "cannot update" in log
    run, _ = _fake_git({**base, ("rev-list", "--left-right", "--count"): R("2\t1\n"),
                        ("status", "--porcelain"): R("")})
    monkeypatch.setattr(updater.subprocess, "run", run)
    info = updater.check(fetch=False)
    assert "2 local commit(s)" in info["blocked"]
    # up to date: nothing to say
    run, _ = _fake_git({**base, ("rev-list", "--left-right", "--count"): R("0\t0\n"),
                        ("status", "--porcelain"): R("")})
    monkeypatch.setattr(updater.subprocess, "run", run)
    info = updater.check(fetch=False)
    assert not info["available"] and updater.summary(info) == ""


def test_check_pip_mode_and_command(monkeypatch):
    monkeypatch.setattr(updater, "install_mode", lambda: "pip")
    monkeypatch.setattr(updater, "shutil_pip_ok", lambda: True)
    monkeypatch.setattr(cs, "__version__", "2.8.0")
    monkeypatch.setattr(updater, "latest_release", lambda **k: {"version": "2.9.0", "url": "u",
                                                                "notes": "notes", "published": "2026-09-27"})
    info = updater.check()
    assert info["mode"] == "pip" and info["available"] and info["latest"] == "2.9.0" and info["notes"] == "notes"
    assert updater.summary(info) == "Update available: 2.9.0"
    cmd = updater.pip_command("2.9.0")
    assert cmd[:4] == [sys.executable, "-m", "pip", "install"] and cmd[-1].endswith("/v2.9.0.tar.gz")
    ran = []
    monkeypatch.setattr(updater.subprocess, "run",
                        lambda args, capture_output, text, timeout: ran.append(args) or R("Successfully installed"))
    ok, log = updater.apply_update(info)
    assert ok and ran[0] == cmd and "Successfully installed" in log
    monkeypatch.setattr(updater, "latest_release", lambda **k: {"version": "2.8.0", "notes": ""})
    assert updater.check()["available"] is False


def test_latest_release_cache_and_offline(monkeypatch):
    import urllib.request
    calls = []

    class Resp:
        def read(self):
            return b'{"tag_name": "v3.0.0", "html_url": "h", "body": "b", "published_at": "2026-10-01T00:00:00Z"}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: calls.append(1) or Resp())
    updater._RELEASE_CACHE.update(t=0.0, val=None)
    a = updater.latest_release()
    b = updater.latest_release()
    assert a == b == {"version": "3.0.0", "url": "h", "notes": "b", "published": "2026-10-01"} and len(calls) == 1
    def boom(req, timeout=0):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert updater.latest_release(ttl=0) == {}
    updater._RELEASE_CACHE.update(t=0.0, val=None)
