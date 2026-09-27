#!/usr/bin/env python3
"""Render the install census: docs/nodes.json plus the NODES block in README.md.

Three sources, merged:
  passive   every ClAudit-filed issue on anthropics/claude-code ends with "ClAudit vX.Y.Z" and has
            an author: distinct reporters, the version each one last filed with, when.
  github    opt-in heartbeat comments on the census issue (sworrl/ClAudit#14): login, version, OS,
            install mode, node hash, updated_at. Quiet after GITHUB_QUIET_H without an edit.
  anon      the Cloudflare Worker's /stats: active / stopped / quiet nodes and version breakdowns
            from the anonymous 10-minute heartbeat.
Run by .github/workflows/poll.yml hourly; also runnable locally (reads only, writes the two files).
"""
import datetime
import json
import os
import re
import subprocess
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claudit_scan as cs  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
NODES_JSON = os.path.join(ROOT, "docs", "nodes.json")
START, END = "<!-- NODES:START -->", "<!-- NODES:END -->"
VERSION_RE = re.compile(r"ClAudit v(\d+\.\d+\.\d+)")
GITHUB_QUIET_H = 24


def _gh_json(args):
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=300).stdout
        return json.loads(out or "null")
    except Exception:
        return None


def _iso(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def passive_census(items, now):
    """items: [{author:{login}, body, createdAt}] -> reporters keyed by login with their latest
    version and last filing; plus per-version reporter counts and 7d/30d activity."""
    reps = {}
    for it in items or []:
        login = ((it.get("author") or {}).get("login") or "").strip()
        m = VERSION_RE.search(it.get("body") or "")
        if not login or not m:
            continue
        at = _iso(it.get("createdAt"))
        r = reps.setdefault(login, {"version": m.group(1), "last": at, "first": at, "issues": 0})
        r["issues"] += 1
        if at and (r["last"] is None or at > r["last"]):
            r["last"], r["version"] = at, m.group(1)
        if at and (r["first"] is None or at < r["first"]):
            r["first"] = at
    by_version = {}
    active_7d = active_30d = 0
    for r in reps.values():
        by_version[r["version"]] = by_version.get(r["version"], 0) + 1
        if r["last"]:
            age = (now - r["last"]).total_seconds()
            active_7d += age <= 7 * 86400
            active_30d += age <= 30 * 86400
    return {"reporters": len(reps), "active_7d": active_7d, "active_30d": active_30d,
            "by_latest_version": dict(sorted(by_version.items(), key=lambda kv: cs.version_tuple(kv[0]), reverse=True)),
            "issues": sum(r["issues"] for r in reps.values())}


def github_census(comments, now):
    """Heartbeat comments -> nodes with login, version, os, mode, hash, last, and active/quiet."""
    nodes = []
    for c in comments or []:
        body = c.get("body") or ""
        if cs.CENSUS_MARKER not in body:
            continue
        m = re.search(r"v(\d+\.\d+\.\d+)\s*·\s*(\w+)\s*·\s*(\w+)\s*·\s*node ([0-9a-f]{8})", body)
        last = _iso(c.get("updated_at") or c.get("updatedAt"))
        age_h = (now - last).total_seconds() / 3600 if last else 1e9
        nodes.append({"login": (c.get("user") or c.get("author") or {}).get("login", "?"),
                      "version": m.group(1) if m else "?", "os": m.group(2) if m else "?",
                      "mode": m.group(3) if m else "?", "node": m.group(4) if m else "?",
                      "last": last.isoformat() if last else "", "active": age_h <= GITHUB_QUIET_H})
    active = [n for n in nodes if n["active"]]
    by_v = {}
    for n in active:
        by_v[n["version"]] = by_v.get(n["version"], 0) + 1
    return {"nodes": len(nodes), "active": len(active), "quiet": len(nodes) - len(active),
            "by_version": dict(sorted(by_v.items(), key=lambda kv: cs.version_tuple(kv[0]), reverse=True)),
            "list": nodes}


def anon_census(url):
    try:
        req = urllib.request.Request(url.rstrip("/") + "/stats", headers={"User-Agent": "ClAudit-census"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r) or {}
    except Exception:
        return {}


def render_md(d):
    a, g, p = d.get("anon") or {}, d.get("github") or {}, d.get("passive") or {}
    L = [START, "### Installs", ""]
    if a.get("generated"):
        L.append(f"Running right now (anonymous heartbeat): **{a.get('active', 0)}** node(s), "
                 f"{a.get('quiet', 0)} gone quiet and {a.get('stopped', 0)} stopped cleanly in the last 24 h, "
                 f"{a.get('seen_7d', 0)} seen in 7 days.")
        vs = a.get("versions") or {}
        if vs:
            L.append("Versions running: " + ", ".join(f"{v} ({n})" for v, n in
                                                     sorted(vs.items(), key=lambda kv: cs.version_tuple(kv[0]), reverse=True)) + ".")
    else:
        L.append("Anonymous heartbeat: no data yet.")
    L.append(f"Opt-in GitHub heartbeats ([#{cs.CENSUS_ISSUE}](https://github.com/{cs.POLL_REPO}/issues/{cs.CENSUS_ISSUE})): "
             f"{g.get('active', 0)} active, {g.get('quiet', 0)} quiet.")
    L.append(f"Reporters seen in filed issues: {p.get('reporters', 0)} accounts, {p.get('active_30d', 0)} active in 30 days, "
             f"{p.get('active_7d', 0)} in 7. Latest version per reporter: "
             + (", ".join(f"{v} ({n})" for v, n in (p.get('by_latest_version') or {}).items()) or "none") + ".")
    L.append("")
    L.append(f"_Updated {d.get('updated', '')}. What each number means and what is sent: "
             f"[Census](#census)._")
    L.append(END)
    return "\n".join(L)


def main():
    now = datetime.datetime.now(datetime.timezone.utc)
    # GitHub search returns at most 1000 issues per query; the newest and the oldest 1000 together
    # cover the corpus until it passes 2000 filed reports.
    items, seen = [], set()
    for order in ("created-desc", "created-asc"):
        for it in _gh_json(["issue", "list", "-R", cs.DEFAULT_REPO, "--search",
                            f'"Filed automatically by ClAudit" sort:{order}', "--state", "all",
                            "--limit", "1000", "--json", "number,author,body,createdAt"]) or []:
            if it.get("number") not in seen:
                seen.add(it.get("number"))
                items.append(it)
    comments = _gh_json(["api", "--paginate", f"repos/{cs.POLL_REPO}/issues/{cs.CENSUS_ISSUE}/comments"]) or []
    if isinstance(comments, dict):
        comments = [comments]
    d = {"updated": now.strftime("%Y-%m-%d %H:%M UTC"), "passive": passive_census(items, now),
         "github": github_census(comments, now), "anon": anon_census(cs.CENSUS_URL)}
    os.makedirs(os.path.dirname(NODES_JSON), exist_ok=True)
    with open(NODES_JSON, "w", encoding="utf-8") as fh:
        json.dump(d, fh, indent=1)
    with open(README, encoding="utf-8") as fh:
        text = fh.read()
    if START in text and END in text:
        pre, rest = text.split(START, 1)
        _old, post = rest.split(END, 1)
        with open(README, "w", encoding="utf-8") as fh:
            fh.write(pre + render_md(d) + post)
    else:
        sys.stderr.write("WARN: NODES markers not found in README; skipping README update\n")
    a = d["anon"]
    print(f"census: anon active {a.get('active', 0)} quiet {a.get('quiet', 0)} stopped {a.get('stopped', 0)} | "
          f"github active {d['github']['active']} | reporters {d['passive']['reporters']}")


if __name__ == "__main__":
    main()
