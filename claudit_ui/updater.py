"""Update monitor: what is running, what GitHub has, and the in-app update itself.

Two install modes. A git clone compares HEAD with origin/<branch> (commits behind/ahead, dirty
tree, the version on the remote branch) and updates with a fast-forward pull. A pip/wheel install
compares __version__ with the latest GitHub Release and updates by pip-installing that tag.
Pure stdlib plus claudit_scan, so it imports (and tests) without PyQt6.
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import claudit_scan as cs

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTO_UPDATE = True          # config auto_update: git clones pull and restart on their own when behind
RELEASE_TTL = 600           # seconds between GitHub Release lookups
_RELEASE_CACHE = {"t": 0.0, "val": None}


def _run(args, timeout=60):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _git(*args, timeout=30):
    return _run(["git", "-C", REPO_DIR, *args], timeout=timeout)


def install_mode():
    return "git" if os.path.isdir(os.path.join(REPO_DIR, ".git")) else "pip"


def latest_release(timeout=10, ttl=RELEASE_TTL):
    """{'version','url','notes','published'} for the newest GitHub Release, cached for `ttl`;
    {} when offline or rate-limited."""
    now = time.time()
    if _RELEASE_CACHE["val"] is not None and now - _RELEASE_CACHE["t"] < ttl:
        return _RELEASE_CACHE["val"]
    req = urllib.request.Request(cs.RELEASES_API, headers={"Accept": "application/vnd.github+json",
                                                           "User-Agent": "ClAudit"})
    out = {}
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r) or {}
        out = {"version": str(d.get("tag_name", "")).lstrip("v"), "url": d.get("html_url", ""),
               "notes": (d.get("body") or "").strip(), "published": str(d.get("published_at", ""))[:10]}
    except Exception:
        out = {}
    _RELEASE_CACHE["t"], _RELEASE_CACHE["val"] = now, out
    return out


def _remote_version(branch):
    r = _git("show", f"origin/{branch}:claudit_scan.py", timeout=10)
    if r.returncode != 0:
        return ""
    for line in r.stdout.splitlines():
        if line.startswith("__version__"):
            return line.split('"')[1] if '"' in line else ""
    return ""


def check(fetch=True):
    """Snapshot of the update state. Keys: mode, current, head, branch, latest, remote_version,
    remote_head, behind, ahead, dirty, code_changed, available, blocked, notes, release_url,
    published, checked. `blocked` is the reason an update cannot be applied ('' when it can)."""
    info = {"mode": install_mode(), "current": cs.__version__, "head": "", "branch": "",
            "latest": "", "remote_version": "", "remote_head": "", "behind": 0, "ahead": 0,
            "dirty": False, "code_changed": False, "available": False, "blocked": "",
            "notes": "", "release_url": "", "published": "", "checked": time.time(), "error": ""}
    rel = latest_release()
    info.update(latest=rel.get("version", ""), release_url=rel.get("url", ""),
                published=rel.get("published", ""))
    if info["mode"] == "git":
        try:
            info["head"] = _git("rev-parse", "--short", "HEAD", timeout=5).stdout.strip()
            branch = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=5).stdout.strip() or "main"
            info["branch"] = branch
            if fetch:
                f = _git("fetch", "--quiet", "origin", branch)
                if f.returncode != 0:
                    info["error"] = (f.stderr or "fetch failed").strip()[:200]
            info["remote_head"] = _git("rev-parse", "--short", f"origin/{branch}", timeout=5).stdout.strip()
            lr = _git("rev-list", "--left-right", "--count", f"HEAD...origin/{branch}", timeout=10).stdout.split()
            if len(lr) == 2:
                info["ahead"], info["behind"] = int(lr[0]), int(lr[1])
            info["dirty"] = bool(_git("status", "--porcelain", timeout=10).stdout.strip())
            info["remote_version"] = _remote_version(branch)
            if info["behind"]:
                info["code_changed"] = _code_changed_between("HEAD", f"origin/{branch}")
                info["available"] = True
                if info["ahead"]:
                    info["blocked"] = (f"this checkout has {info['ahead']} local commit(s) the remote "
                                       "does not: rebase or push them first")
                elif info["dirty"]:
                    info["blocked"] = "the working tree has uncommitted changes: commit or stash them first"
            if info["available"] and not info["notes"]:
                log = _git("log", "--no-merges", "--format=- %s", f"HEAD..origin/{branch}", timeout=10).stdout
                lines = [ln for ln in log.splitlines() if "chore: refresh poll" not in ln]
                info["notes"] = "\n".join(lines[:40])
            if rel.get("version") and rel["version"] == info["remote_version"]:
                info["notes"] = rel.get("notes") or info["notes"]
        except Exception as e:
            info["error"] = f"{type(e).__name__}: {e}"[:200]
    else:
        info["available"] = bool(info["latest"]) and cs.version_tuple(info["latest"]) > cs.version_tuple(cs.__version__)
        info["notes"] = rel.get("notes", "")
        if not shutil_pip_ok():
            info["blocked"] = "pip is not importable in this interpreter"
    return info


def _code_changed_between(a, b):
    try:
        out = _git("diff", "--name-only", a, b, timeout=10).stdout
    except Exception:
        return True
    return any(f.strip().endswith(".py") or f.strip() in ("requirements.txt", "pyproject.toml")
               for f in out.splitlines())


def shutil_pip_ok():
    try:
        import pip  # noqa: F401
        return True
    except ImportError:
        return False


def pip_command(version):
    """The pip upgrade for a wheel install: the tagged tarball from GitHub (works before and after
    the PyPI publisher exists)."""
    return [sys.executable, "-m", "pip", "install", "--upgrade",
            f"claudit-cc[gui] @ https://github.com/{cs.POLL_REPO}/archive/refs/tags/v{version}.tar.gz"]


def summary(info):
    """One line for the header pill / tray: '' when nothing to show."""
    if not info:
        return ""
    if info.get("blocked"):
        target = info.get("remote_version") or info.get("latest") or "?"
        return f"Update {target} waiting: {info['blocked'].split(':')[0]}"
    if info.get("available"):
        target = info.get("remote_version") or info.get("latest") or "?"
        n = info.get("behind")
        return f"Update available: {target}" + (f" ({n} commit{'s' if n != 1 else ''})" if n else "")
    return ""


def apply_update(info, on_line=None):
    """Do the update. git: fast-forward pull. pip: install the latest tag. Returns (ok, log).
    Never touches a dirty or diverged checkout (check() marks those `blocked`)."""
    log = []

    def say(s):
        log.append(s)
        if on_line:
            on_line(s)
    if info.get("blocked"):
        say(f"cannot update: {info['blocked']}")
        return False, "\n".join(log)
    try:
        if info.get("mode") == "git":
            branch = info.get("branch") or "main"
            say(f"$ git pull --ff-only origin {branch}")
            r = _git("pull", "--ff-only", "origin", branch, timeout=120)
            say((r.stdout + r.stderr).strip())
            ok = r.returncode == 0
        else:
            cmd = pip_command(info.get("latest") or "")
            say("$ " + " ".join(cmd))
            r = _run(cmd, timeout=600)
            say((r.stdout + r.stderr).strip()[-4000:])
            ok = r.returncode == 0
    except Exception as e:
        say(f"failed: {type(e).__name__}: {e}")
        ok = False
    say("done: restart to run the new version" if ok else "update failed; nothing was changed")
    return ok, "\n".join(log)
