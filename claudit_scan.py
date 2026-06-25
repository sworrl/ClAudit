#!/usr/bin/env python3
"""claudit_scan — watch all Claude Code sessions for server-side BLOCKS, dedup them,
and file them as GitHub issues (new issue) or comment when one recurs (update).

Files issues for:   cybersecurity safety-filter blocks, AUP/Usage-Policy blocks.
Logs but NEVER sends: overloaded/529, rate-limit, usage-limit, any other API error.

Findings dedup by the triggering prompt (retries collapse into one finding with all
Request IDs). State maps each finding -> its issue, so a recurrence with new Request
IDs becomes a comment ("update"), not a duplicate issue.

Usage:
  claudit_scan.py                  # dry-run: list new findings, file nothing
  claudit_scan.py --baseline       # mark ALL current findings as seen, file nothing
  claudit_scan.py --watch          # poll forever; file new blocks + comment recurrences
  claudit_scan.py --post           # one-shot: review backlog in $EDITOR, then file
  claudit_scan.py --post --no-review
Flags: --interval N (watch poll secs, default 30), --delay N (secs between posts,
       default 3), --limit N (0=all), -R owner/repo.
"""

import argparse
import atexit
import collections
import hashlib
import json
import os
import re
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claudit  # noqa: E402  (LLM_SCRUB flag + llm_redact)
from claudit import scrub  # noqa: E402  (reuse the PII scrubber)

# Launched from a desktop icon, PATH is often minimal and `gh`/`claude` aren't found.
# Make sure the interpreter's own bin dir (where gh usually lives) + common bins are on PATH.
for _p in (os.path.dirname(sys.executable), "/usr/local/bin", "/opt/homebrew/bin",
           os.path.expanduser("~/.local/bin")):
    if _p and _p not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")

PROJECTS = os.path.expanduser("~/.claude/projects")
STATE_DIR = os.path.expanduser("~/.claude/claudit")
STATE_FILE = os.path.join(STATE_DIR, "filed.json")
ERROR_LOG = os.path.join(STATE_DIR, "error-log.jsonl")
LOCK_FILE = os.path.join(STATE_DIR, "watcher.lock")
ISSUES_DB = os.path.join(STATE_DIR, "issues.jsonl")   # local record of every filed issue
__version__ = "2.0.4"
DEFAULT_REPO = "anthropics/claude-code"
PROJECT_URL = "https://github.com/sworrl/ClAudit"   # issues link back here for transparency
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claudit_icon.png")
DEFAULT_NOTE = ("False positive — in-scope, authorized security work; not out of scope. "
                "Filed automatically by claudit.")

REQ_ID = re.compile(r"req_[A-Za-z0-9]+")
TOKEN = re.compile(r"token=[A-Za-z0-9_\-]+")
FILE_KINDS = {
    "cyber": "Cybersecurity safety-filter false positive",
    "aup": "AUP / Usage-Policy block (false positive)",
    "harness": "Claude Code harness / auto-mode classifier denial",
}

WHY = {
    "cyber": ("Legitimate, in-scope security / defensive / administration work was flagged by the "
              "cybersecurity-topic safety classifier — pattern-matched on terminology, not on any "
              "harmful intent. Securing or administering one's own systems is the opposite of attacking them."),
    "aup": ("A benign, in-scope request was flagged as a Usage-Policy violation — a false positive on "
            "ordinary, authorized work."),
    "harness": ("The Claude Code auto-mode classifier denied an action during legitimate, authorized, "
                "in-scope work. See the block reason below."),
}


def classify(text):
    t = text.lower()
    if "safety measures that flagged this message for a cybersecurity topic" in t:
        return "cyber"
    if "violate our usage policy" in t or "unable to respond to this request" in t:
        return "aup"
    if "overloaded" in t or "temporarily limiting" in t or "529" in t:
        return "overloaded"
    if "hit your limit" in t or "rate limit" in t or "429" in t or "· resets" in t:
        return "limit"
    return "other"


def human_text(entry):
    if entry.get("type") != "user":
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text").strip()
        return text or None
    return None


def error_text(entry):
    if not entry.get("isApiErrorMessage"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def sig(kind, prompt):
    norm = re.sub(r"\s+", " ", prompt.lower()).strip()[:500]
    return hashlib.sha1((kind + "|" + norm).encode()).hexdigest()[:12]


def reqs_of(f):
    seen, out = set(), []
    for o in f["occ"]:
        if o["req"] and o["req"] not in seen:
            seen.add(o["req"])
            out.append(o)
    return out


def harness_denial(entry):
    """Text of a Claude Code auto-mode-classifier denial (a tool_result error), or None."""
    if entry.get("type") != "user":
        return None
    content = (entry.get("message") or {}).get("content")
    if not isinstance(content, list):
        return None
    for b in content:
        if not (isinstance(b, dict) and b.get("type") == "tool_result"):
            continue
        tc = b.get("content")
        if isinstance(tc, str):
            text = tc
        elif isinstance(tc, list):
            text = "\n".join(x.get("text", "") for x in tc
                             if isinstance(x, dict) and x.get("type") == "text")
        else:
            text = ""
        low = text.lower()
        if "auto mode classifier" in low or "permission for this action was denied" in low:
            return text.strip()
    return None


def assistant_text(entry):
    """Text of a normal (non-error) assistant turn, for capturing conversation leadup."""
    if entry.get("type") != "assistant" or entry.get("isApiErrorMessage"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        t = "\n".join(b.get("text", "") for b in content
                      if isinstance(b, dict) and b.get("type") == "text").strip()
        return t or None
    return None


_SCAN_CACHE = {"t": -1e9, "val": None}


def scan(ttl=0.0):
    """Walk all sessions -> (findings dict[sig]->finding, logged-only counts).
    With ttl>0, reuse the last result if it's younger than ttl seconds — backfill drips
    re-scan hundreds of files otherwise, which is what made it crawl."""
    if ttl and _SCAN_CACHE["val"] is not None and time.monotonic() - _SCAN_CACHE["t"] < ttl:
        return _SCAN_CACHE["val"]
    findings, logged, log_lines = {}, {"overloaded": 0, "limit": 0, "other": 0}, []
    for root, _, files in os.walk(PROJECTS):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            last_prompt = None
            recent = collections.deque(maxlen=6)   # rolling conversation leadup
            try:
                with open(os.path.join(root, name), encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            entry = json.loads(line)
                        except ValueError:
                            continue
                        hp = human_text(entry)
                        if hp:
                            last_prompt = hp
                            recent.append(("user", hp[:300]))
                            continue
                        hd = harness_denial(entry)
                        if hd:
                            ts = entry.get("timestamp", "")
                            log_lines.append(json.dumps({"kind": "harness", "ts": ts,
                                                         "session": name, "req": None}))
                            s = sig("harness", hd[:300])
                            f = findings.setdefault(s, {"sig": s, "kind": "harness",
                                                        "prompt": last_prompt or "(no preceding prompt)",
                                                        "occ": [], "block_text": hd, "leadup": list(recent)})
                            f["occ"].append({"req": None, "ts": ts, "session": name,
                                             "proj": os.path.basename(root)})
                            continue
                        err = error_text(entry)
                        if err is None:
                            at = assistant_text(entry)
                            if at:
                                recent.append(("assistant", at[:300]))
                            continue
                        kind = classify(err)
                        ts = entry.get("timestamp", "")
                        m = REQ_ID.search(err)
                        req = m.group(0) if m else None
                        log_lines.append(json.dumps({"kind": kind, "ts": ts, "session": name, "req": req}))
                        if kind not in FILE_KINDS:
                            logged[kind] += 1
                            continue
                        prompt = last_prompt or "(triggering prompt not recoverable)"
                        s = sig(kind, prompt)
                        f = findings.setdefault(s, {"sig": s, "kind": kind, "prompt": prompt,
                                                    "occ": [], "block_text": err, "leadup": list(recent)})
                        f["occ"].append({"req": req, "ts": ts, "session": name,
                                         "proj": os.path.basename(root)})
            except OSError:
                continue
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(ERROR_LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(log_lines) + ("\n" if log_lines else ""))
    return findings, logged


def _pid_alive(pid):
    """Best-effort cross-platform liveness check."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _release_singleton():
    try:
        if os.path.exists(LOCK_FILE) and open(LOCK_FILE).read().strip() == str(os.getpid()):
            os.remove(LOCK_FILE)
    except OSError:
        pass


def acquire_singleton():
    """Self-awareness: ensure only ONE watcher process runs at a time. Returns True if we
    hold the lock, False if a live watcher already does (so the caller should exit)."""
    os.makedirs(STATE_DIR, exist_ok=True)
    if os.path.exists(LOCK_FILE):
        try:
            other = int((open(LOCK_FILE).read().strip() or "0"))
        except (OSError, ValueError):
            other = 0
        if other and other != os.getpid() and _pid_alive(other):
            return False
    with open(LOCK_FILE, "w") as fh:
        fh.write(str(os.getpid()))
    atexit.register(_release_singleton)
    return True


CONFIG_FILE = os.path.join(STATE_DIR, "config.json")


def load_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=1)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if isinstance(data, list):  # migrate old set-of-sigs format
        return {s: {"issue": None, "url": None, "reqs": []} for s in data}
    return data


def save_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)


DOMAIN_PATTERNS = [
    ("cloud-iam", re.compile(r"\b(entra|azure ad|tenant|conditional access|app role|app registration|service principal|okta|oauth|iam)\b", re.I)),
    ("defensive-hardening", re.compile(r"\b(harden|hardening|mfa|firewall|edr|blue team|patch|cis benchmark|least privilege|lockdown)\b", re.I)),
    ("reverse-engineering", re.compile(r"\b(disassemble|decompile|ghidra|ida pro|opcode|unpack|reverse engineer)\b", re.I)),
    ("infra-devops", re.compile(r"\b(kubernetes|docker|terraform|nginx|ansible|systemd|hypervisor|reverse proxy)\b", re.I)),
    ("web-security", re.compile(r"\b(xss|sql injection|sqli|csrf|ssrf|burp|owasp)\b", re.I)),
    ("offensive-pentest", re.compile(r"\b(exploit|payload|msfvenom|metasploit|shellcode|reverse shell|\bc2\b|privilege escalation|lateral movement)\b", re.I)),
    ("crypto-secrets", re.compile(r"\b(encrypt|decrypt|private key|certificate|tls|keystore)\b", re.I)),
    ("malware-forensics", re.compile(r"\b(malware|forensic|memory dump|yara|incident response|ioc)\b", re.I)),
]


def categorize(f):
    """Heuristic work-domain tag for the report (helps triage which classifier over-fires)."""
    text = (f.get("prompt", "") + " " + f.get("block_text", "") + " "
            + " ".join(t for _, t in (f.get("leadup") or []))).lower()
    for cat, pat in DOMAIN_PATTERNS:
        if pat.search(text):
            return cat
    return "general"


def project_label(encoded):
    """Turn a ~/.claude/projects dir name into a readable path with the username scrubbed."""
    return scrub("/" + encoded.strip("-").replace("-", "/"))[0]


def build_issue(f, note):
    reqs = reqs_of(f)
    first_ts = min((o["ts"] for o in f["occ"] if o["ts"]), default="")
    sessions = len({o["session"] for o in f["occ"]})
    projects = sorted({project_label(o["proj"]) for o in f["occ"] if o.get("proj")})
    proj = projects[0].rstrip("/").split("/")[-1] if projects else "unknown"
    # [Bug] first (issue template), then kind; ClAudit-tagged + a distinct discriminator.
    if f["kind"] == "harness":
        m = re.search(r"Reason:\s*(.+?)(?:\.\s|\n|$)", f["block_text"])
        reason = (m.group(1).strip()[:70] if m else f"#{f['sig']}")
        title = f"[Bug][harness] ClAudit: auto-mode classifier denied — {reason}"
    else:
        lead = reqs[0]["req"] if reqs else f"#{f['sig']}"
        title = f"[Bug][{f['kind']}] ClAudit false-positive in {proj} — {lead}"
    req_lines = "\n".join(f"- `{o['req']}`  ({o['ts']})" for o in reqs) or "- (no Request ID captured)"
    note_clean, _ = scrub(note or DEFAULT_NOTE)
    # Full PII scrub on the block message (was token-only — leaked IPs/hosts/paths).
    block_clean = scrub(TOKEN.sub("token=[SCRUBBED]", f["block_text"]))[0].strip()[:500]
    if f["kind"] == "harness":
        # Show ONLY the classifier's stated reason — never the quoted command (infra/sketch risk).
        m = re.search(r"Reason:\s*(.+?)(?:\.\s|\n|If you have|$)", f["block_text"])
        block_clean = (scrub(m.group(1).strip())[0][:300] if m
                       else "(auto-mode classifier denial — see Request IDs)")
    leadup = f.get("leadup") or []
    leadup_md = "\n".join(f"**{role}:** {scrub(re.sub(chr(10), ' ', txt))[0]}"
                          for role, txt in leadup) or "_(not captured)_"

    why_text = WHY.get(f["kind"], WHY["cyber"])
    if claudit.BURN_TOKENS:   # spend tokens to craft a bespoke, specific title + explanation
        ctx = f"work domain: {categorize(f)}\nblock message: {block_clean}\nconversation leadup:\n{leadup_md}"
        bt = claudit.llm_compose(
            f"Write ONE specific GitHub issue title (max ~95 chars) that starts EXACTLY with "
            f"'[Bug][{f['kind']}]' and describes the concrete legitimate work this Claude Code safety "
            f"block wrongly stopped. Output ONLY that title line — no preamble, no 'here is', no quotes, "
            f"no explanation.", ctx)
        if bt:
            # the model sometimes adds a chatty preamble line — take the line that's actually a title
            lines = [ln.strip().strip('"').strip("`") for ln in bt.splitlines() if ln.strip()]
            cand = next((ln for ln in lines if ln.lower().startswith("[bug]")), "")
            if cand:                       # only trust a real title; else keep the deterministic one
                cand = scrub(cand)[0][:110]
                lead_req = reqs[0]["req"] if reqs else ""
                title = cand if (not lead_req or lead_req in cand) else f"{cand} ({lead_req})"
        bw = claudit.llm_compose(
            "Write a tight, factual 2-3 sentence explanation of why this Claude Code safety block is a "
            "false positive on legitimate, in-scope work — suitable for a bug report to Anthropic.", ctx)
        if bw:
            why_text = scrub(bw)[0]

    body = f"""**Type:** {FILE_KINDS[f['kind']]}  ·  **Work domain (heuristic):** `{categorize(f)}`

### Why this is a false positive
{why_text}

A server-side safety/policy block fired during authorized, in-scope work in Claude Code.
Filing as a false positive. Recurred **{len(f['occ'])}×** across {sessions}
session(s); first seen {first_ts}.

### Request IDs (lookup-able server-side)
{req_lines}

### In-scope justification
{note_clean}

### Block message
> {block_clean}

**Environment:** Claude Code, Linux. · **Work domain:** `{categorize(f)}`

---
<sub>🔎 Filed automatically by [ClAudit v{__version__}]({PROJECT_URL}) — a FOSS tool for reporting false-positive Claude Code blocks.</sub>"""
    # Title is built from already-scrubbed parts + the Request ID — regex/denylist scrub only
    # (never the LLM, so the Request ID survives). Body gets the opt-in LLM pass.
    return scrub(title)[0], claudit.llm_redact(body)


def log_issue(f, repo, url):
    """Append a filed issue to the local issues DB (with PII-scrubbed leadup)."""
    rec = {
        "sig": f["sig"], "kind": f["kind"], "repo": repo, "url": url,
        "version": __version__,
        "issue": url.rsplit("/", 1)[-1],
        "reqs": [o["req"] for o in reqs_of(f)],
        "first_ts": min((o["ts"] for o in f["occ"] if o["ts"]), default=""),
        "projects": sorted({project_label(o["proj"]) for o in f["occ"] if o.get("proj")}),
        "leadup": [[role, scrub(txt)[0]] for role, txt in (f.get("leadup") or [])],
    }
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(ISSUES_DB, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def gh_create(repo, title, body):
    out = subprocess.run(["gh", "issue", "create", "-R", repo, "--title", title, "--body", body],
                         capture_output=True, text=True, check=True).stdout.strip()
    return out  # issue URL


def gh_comment(repo, issue, body):
    subprocess.run(["gh", "issue", "comment", issue, "-R", repo, "--body", body],
                   capture_output=True, text=True, check=True)


def file_one(f, note, repo, state):
    """Create an issue, or comment if it already exists with fresh Request IDs.
    Returns (action, title_or_ref, url) with action in {'new','updated',None}."""
    rec = state.get(f["sig"])
    cur_reqs = [o["req"] for o in reqs_of(f)]
    if rec is None:
        # Reserve the signature and persist BEFORE the network call so a concurrent pass
        # (or a crash-restart) can never double-file the same finding.
        state[f["sig"]] = {"issue": None, "url": None, "kind": f["kind"], "reqs": cur_reqs}
        save_state(state)
        title, body = build_issue(f, note)
        try:
            url = gh_create(repo, title, body)
        except Exception:
            state.pop(f["sig"], None)    # release on failure so it can retry later
            save_state(state)
            raise
        state[f["sig"]].update(issue=url.rsplit("/", 1)[-1], url=url)
        log_issue(f, repo, url)
        return ("new", title, url)
    fresh = [r for r in cur_reqs if r not in rec.get("reqs", [])]
    if fresh and rec.get("issue"):
        gh_comment(repo, rec["issue"], "Recurred again. Additional Request IDs:\n" +
                   "\n".join(f"- `{r}`" for r in fresh))
        rec["reqs"] = rec.get("reqs", []) + fresh
        return ("updated", f"#{rec['issue']}", rec.get("url"))
    return (None, None, rec.get("url"))


def baseline(state):
    findings, _ = scan()
    for s, f in findings.items():
        state.setdefault(s, {"issue": None, "url": None, "kind": f["kind"],
                             "reqs": [o["req"] for o in reqs_of(f)]})
    state["__baselined__"] = True
    save_state(state)
    return len(findings)


def ensure_baseline(state, announce=None):
    """Never file the backlog: if we've never baselined, mark all current findings
    seen (file nothing) before any watch/cycle can run."""
    if state.get("__baselined__"):
        return 0
    n = baseline(state)
    if announce:
        announce("baselined", f"{n} existing blocks marked seen (not filed)", "")
    return n


def _toast(title, body):
    """Best-effort desktop notification across Linux / macOS / Windows."""
    try:
        if shutil.which("notify-send"):
            subprocess.run(["notify-send", "-a", "claudit", "-i", ICON, title, body], check=False)
        elif platform.system() == "Darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification {json.dumps(body)} with title {json.dumps(title)}'],
                           check=False)
        elif platform.system() == "Windows":
            ps = (f"$ws=New-Object -ComObject WScript.Shell;"
                  f"[void]$ws.Popup({json.dumps(body)},5,{json.dumps(title)},64)")
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
    except Exception:
        pass


def notify(action, ref, url):
    """Plain desktop toast (cross-platform)."""
    _toast(f"claudit: {action}", f"{ref}\n{url or ''}".strip())
    print(f"[{action}] {ref} {url or ''}", file=sys.stderr)


def notify_action(title, body, on_report):
    """Toast with a clickable 'Report it' button (Linux/notify-send only); runs on_report()
    if clicked. notify-send -A blocks until the user acts, so call this in a thread. On other
    platforms it degrades to a plain notification (use the GUI's menu to file)."""
    if not shutil.which("notify-send"):
        _toast(title, body)
        return
    res = subprocess.run(
        ["notify-send", "-a", "claudit", "-i", ICON, "-u", "critical",
         "-A", "report=Report it", "-A", "dismiss=Dismiss", title, body],
        capture_output=True, text=True)
    if res.stdout.strip() == "report":
        on_report()


def announce_pending(state, repo, delay):
    """Pop an actionable toast for whatever is currently queued (in a background thread)."""
    n = len(pending_sigs(state))
    if not n:
        return
    threading.Thread(target=notify_action, daemon=True, args=(
        f"ClAudit: {n} false-positive block(s) queued",
        "Click ‘Report it’ to file these to GitHub now.",
        lambda: notify("filed", f"{file_pending(state, repo, False, delay, notify)} reported", repo),
    )).start()


def pending_sigs(state):
    return list(state.get("__pending__", []))


def monitor_cycle(state, on_detect):
    """Notify-only pass: detect NEW findings (not seen, not already pending), queue them,
    and toast. Files NOTHING. Returns the count of newly-detected findings."""
    findings, _ = scan()
    pend = state.setdefault("__pending__", [])
    fresh = [f for s, f in findings.items()
             if s not in state and s not in pend and not s.startswith("__")]
    if fresh:
        for f in fresh:
            pend.append(f["sig"])
        save_state(state)
        on_detect(fresh)
    return len(fresh)


def passes_gate(f, state):
    """True if the finding should be filed. When the LLM is on, blocks it judges were CORRECT
    (not false positives) are skipped and recorded, so correct safety blocks never get filed."""
    ctx = " ".join(t for _, t in (f.get("leadup") or []))
    ok, reason = claudit.llm_is_false_positive(f["kind"], f.get("block_text", ""), ctx)
    if not ok:
        state[f["sig"]] = {"issue": None, "skipped": (reason or "judged a correct block")[:140],
                           "kind": f["kind"], "reqs": [o["req"] for o in reqs_of(f)]}
        save_state(state)
        print(f"  skip {f['sig']} — not a false positive: {reason[:80]}", file=sys.stderr)
    return ok


def auto_cycle(state, repo, delay, on_event):
    """Auto-post pass: file every NEW finding, comment recurrences. Files nothing for
    baselined/already-filed findings. Returns count of actions taken."""
    findings, _ = scan(ttl=8)
    acted = 0
    for f in findings.values():
        if f["sig"] not in state and not passes_gate(f, state):
            continue
        try:
            action, ref, url = file_one(f, "", repo, state)
        except Exception as e:
            print(f"  ! {f['sig']}: {e}", file=sys.stderr)
            continue
        if action:
            save_state(state)
            on_event(action, ref, url)
            acted += 1
            time.sleep(delay)
    return acted


def backfill_one(f, repo, state):
    """File ONE baselined-but-unfiled finding (a backlog item). Returns event tuple or None."""
    rec = state.get(f["sig"])
    if rec is None or rec.get("issue"):
        return None                       # not a backlog item (new, or already filed)
    title, body = build_issue(f, "")
    url = gh_create(repo, title, body)
    rec.update(issue=url.rsplit("/", 1)[-1], url=url)
    save_state(state)
    log_issue(f, repo, url)
    return ("backfilled", title, url)


def backlog_size(state):
    return sum(1 for s, r in state.items()
               if not s.startswith("__") and r.get("issue") is None)


def _latest_ts(f):
    return max((o["ts"] for o in f["occ"] if o["ts"]), default="")


RATE_LIMIT_HINTS = ("rate limit", "secondary", "abuse", "retry-after",
                    "too quickly", "try again later", "have exceeded")


def backfill_step(state, repo, n, on_event):
    """File up to n backlog items this call, MOST-RECENT blocks first. Returns
    (filed_count, rate_limited) so the caller can back off when GitHub pushes back."""
    findings, _ = scan(ttl=8)
    backlog = [f for f in findings.values()
               if (state.get(f["sig"]) or {}).get("issue", "x") is None
               and not (state.get(f["sig"]) or {}).get("skipped")]
    backlog.sort(key=_latest_ts, reverse=True)
    done = 0
    for f in backlog:
        if done >= n:
            break
        if not passes_gate(f, state):    # skip correct/borderline blocks the LLM won't vouch for
            continue
        try:
            ev = backfill_one(f, repo, state)
        except Exception as e:
            msg = (getattr(e, "stderr", "") or str(e)).lower()
            if any(k in msg for k in RATE_LIMIT_HINTS):
                print("  backfill rate-limited — backing off", file=sys.stderr)
                return done, True
            print(f"  ! backfill {f['sig']}: {e}", file=sys.stderr)
            continue
        if ev:
            on_event(*ev)
            done += 1
    return done, False


def file_pending(state, repo, review_flag, delay, on_event):
    """File everything queued by the watcher (user-initiated). Clears the queue."""
    pend = set(state.get("__pending__", []))
    if not pend:
        return 0
    findings, _ = scan()
    todo = [findings[s] for s in pend if s in findings]
    if review_flag:
        todo = [(f, n) for f, n in review(todo)]
    else:
        todo = [(f, "") for f in todo]
    filed = 0
    for f, note in todo:
        try:
            action, ref, url = file_one(f, note, repo, state)
            pend = state.get("__pending__", [])
            if f["sig"] in pend:
                pend.remove(f["sig"])
            save_state(state)
            on_event(action or "skipped", ref or f["sig"], url)
            filed += 1
        except Exception as e:
            print(f"  ! failed {f['sig']}: {e}", file=sys.stderr)
        time.sleep(delay)
    return filed


# --- one-pass review (for --post backlog filing) ---
def review(findings):
    blocks = [f"### KEEP {f['sig']} | [{f['kind']}] {len(reqs_of(f))} req-id(s), {len(f['occ'])} hits\n"
              f"note: \nhint: {re.sub(chr(10), ' ', f['prompt'])[:160]}\n" for f in findings]
    header = ("# claudit review — DELETE any block you DON'T want filed, then save & close.\n"
              "# Put your in-scope justification after `note:` (PII-scrubbed into the issue).\n\n")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(header + "\n".join(blocks))
        path = tf.name
    subprocess.run([os.environ.get("EDITOR", "nano"), path], check=True)
    with open(path, encoding="utf-8") as fh:
        edited = fh.read()
    os.unlink(path)
    notes = {}
    for chunk in re.split(r"^### KEEP ", edited, flags=re.M)[1:]:
        s = chunk[:12]
        mm = re.search(r"^note:\s*(.*)$", chunk, flags=re.M)
        notes[s] = (mm.group(1).strip() if mm else "")
    by_sig = {f["sig"]: f for f in findings}
    return [(by_sig[s], notes[s]) for s in notes if s in by_sig]


def _llm_dupe_verdict(title, body, flagtext):
    """Ask the `claude` CLI to honestly judge whether an issue is a genuine duplicate."""
    if not shutil.which("claude"):
        return {"duplicate": True, "reason": "no claude CLI available — defaulting to leave it alone"}
    prompt = (
        "You are triaging GitHub bug reports for a maintainer. Decide whether THIS issue is genuinely a "
        "DUPLICATE of the flagged issue(s) — i.e. the SAME underlying bug / root cause — or genuinely "
        "DISTINCT (a clearly different operation or root cause). Be honest and conservative: if they share "
        "the same root cause, it IS a duplicate even if the specific commands differ. "
        "Respond with ONLY a JSON object: "
        '{"duplicate": true/false, "of": "#N or empty", "reason": "one factual sentence"}.\n\n'
        f"THIS ISSUE:\n{title}\n{body[:1500]}\n\nDUP-BOT FLAG (lists the claimed duplicates):\n{flagtext[:1500]}")
    try:
        out = subprocess.run(["claude", "-p", prompt], capture_output=True, text=True, timeout=90).stdout
        m = re.search(r"\{.*\}", out, re.DOTALL)
        return json.loads(m.group(0)) if m else {"duplicate": True, "reason": "no parseable verdict"}
    except Exception as e:
        return {"duplicate": True, "reason": f"verdict error: {e}"}


def dedup_guard(state, repo, limit, apply, on_done=None):
    """For each of YOUR open issues the dup-bot flagged, have the LLM judge it on facts.
    With apply=True it 👎s the dup-bot + posts a PII-scrubbed 'not a duplicate' comment ONLY on
    issues the LLM judges genuinely distinct. Records per-issue status in state['__deduped__']
    so the GUI can show it. Calls on_done(num, status) per handled issue."""
    deduped = state.setdefault("__deduped__", {})
    try:
        issues = json.loads(subprocess.run(
            ["gh", "issue", "list", "-R", repo, "--author", "@me", "--state", "open",
             "--limit", str(limit or 100), "--json", "number,title,body"],
            capture_output=True, text=True, check=True).stdout or "[]")
    except Exception as e:
        print(f"dedup_guard: cannot list issues: {e}", file=sys.stderr)
        return 0
    distinct = dupes = 0
    for it in issues:
        num = it["number"]
        cj = subprocess.run(["gh", "issue", "view", str(num), "-R", repo, "--json", "comments"],
                            capture_output=True, text=True).stdout
        comments = (json.loads(cj or "{}").get("comments") or [])
        flag = next((c for c in comments if "possible duplicate" in c.get("body", "").lower()
                     or "closed as a duplicate" in c.get("body", "").lower()), None)
        if not flag:
            continue
        v = _llm_dupe_verdict(it["title"], it.get("body", ""), flag["body"])
        is_dup = bool(v.get("duplicate"))
        print(f"  #{num}: {'DUPLICATE of ' + v.get('of', '?') if is_dup else 'DISTINCT'} — {v.get('reason', '')[:140]}")
        if is_dup:
            dupes += 1
            deduped[str(num)] = "duplicate"
            if on_done:
                on_done(num, "duplicate")
            continue
        distinct += 1
        if apply:
            _push_not_dup(repo, num, flag.get("id"), v.get("of", ""), v.get("reason", ""), it.get("body", ""))
            deduped[str(num)] = "not-duplicate"
            if on_done:
                on_done(num, "not-duplicate")
            time.sleep(3)
    save_state(state)
    print(f"\n{distinct} judged DISTINCT, {dupes} judged duplicate.", file=sys.stderr)
    return distinct + dupes


def _push_not_dup(repo, num, comment_id, of, reason, issue_body):
    """👎 the dup-bot comment and post a factual 'not a duplicate' note on one issue."""
    if comment_id:
        subprocess.run(["gh", "api", "graphql", "-f",
                        f'query=mutation{{addReaction(input:{{subjectId:"{comment_id}",'
                        f'content:THUMBS_DOWN}}){{reaction{{content}}}}}}'],
                       capture_output=True, text=True)
    body = scrub(f"Not a duplicate {('of ' + of) if of else ''} — {reason} Distinct operation; see "
                 f"the Request IDs above. (Assessed by ClAudit; PII-scrubbed.)")[0]
    bc = claudit.llm_compose(
        f"Write a brief, professional, factual GitHub comment (2-3 sentences) explaining why this issue "
        f"is NOT a duplicate of {of or 'the flagged issue'}. Reason: {reason}. Note it's a distinct "
        f"block with its own Request ID.", issue_body[:1500])
    if bc:
        body = scrub(bc)[0]
    subprocess.run(["gh", "issue", "comment", str(num), "-R", repo, "--body", claudit.llm_redact(body)],
                   capture_output=True, text=True)


def mark_not_duplicate(state, repo, num):
    """MANUAL per-issue dedup (the GUI calls this when YOU click 👎 on a specific issue you judge is
    not a duplicate). 👎s the dup-bot comment + posts a factual note, live. Records status."""
    cj = subprocess.run(["gh", "issue", "view", str(num), "-R", repo, "--json", "body,comments"],
                        capture_output=True, text=True).stdout
    data = json.loads(cj or "{}")
    flag = next((c for c in (data.get("comments") or [])
                 if "possible duplicate" in c.get("body", "").lower()
                 or "closed as a duplicate" in c.get("body", "").lower()), None)
    of = ""
    if flag:
        m = re.search(r"#(\d+)", flag["body"])
        of = f"#{m.group(1)}" if m else ""
    _push_not_dup(repo, num, (flag or {}).get("id"), of, "you reviewed it and it is a distinct block",
                  data.get("body", ""))
    state.setdefault("__deduped__", {})[str(num)] = "not-duplicate"
    save_state(state)
    return True


def main():
    p = argparse.ArgumentParser(description="Watch Claude Code sessions for safety/AUP blocks.")
    p.add_argument("--version", action="version", version=f"ClAudit {__version__}")
    p.add_argument("--baseline", action="store_true", help="mark all current findings seen, file nothing")
    p.add_argument("--watch", action="store_true", help="poll forever (notify-only): detect + queue new blocks")
    p.add_argument("--auto", action="store_true", help="with --watch: auto-file new blocks instead of queuing")
    p.add_argument("--backfill", action="store_true",
                   help="with --watch: slowly drip-file the baselined backlog while monitoring")
    p.add_argument("--backfill-interval", dest="backfill_interval", type=float, default=10,
                   help="starting seconds between backfilled issues; auto-adapts to GitHub limits (default 10)")
    p.add_argument("--backfill-max", dest="backfill_max", type=int, default=0,
                   help="stop backfilling after N issues this run (0 = no cap)")
    p.add_argument("--pending", action="store_true", help="list blocks queued by the watcher")
    p.add_argument("--file-pending", dest="file_pending", action="store_true",
                   help="file everything the watcher queued (user-initiated)")
    p.add_argument("--post", action="store_true", help="one-shot: review backlog and file")
    p.add_argument("--no-review", action="store_true", help="with --post, skip the $EDITOR review")
    p.add_argument("--interval", type=float, default=30, help="watch poll interval secs (default 30)")
    p.add_argument("--delay", type=float, default=3, help="secs between posts (default 3)")
    p.add_argument("--limit", type=int, default=0, help="max findings this run (0 = all)")
    p.add_argument("-R", "--repo", default=DEFAULT_REPO, help=f"target repo (default {DEFAULT_REPO})")
    p.add_argument("--llm-scrub", dest="llm_scrub", action="store_true",
                   help="also use the `claude` CLI to scrub PII the regex can't (slower, uses tokens)")
    p.add_argument("--burn-tokens", dest="burn_tokens", action="store_true",
                   help="use the `claude` CLI to write bespoke titles/bodies — the strongest PII defense")
    p.add_argument("--dedup-guard", dest="dedup_guard", action="store_true",
                   help="LLM-judge your dup-bot-flagged issues (dry-run; add --apply to comment on distinct ones)")
    p.add_argument("--apply", action="store_true", help="with --dedup-guard: actually post the comments")
    args = p.parse_args()
    cfg = load_config()
    if args.llm_scrub or cfg.get("llm_scrub"):
        claudit.LLM_SCRUB = True
    if args.burn_tokens or cfg.get("burn_tokens"):
        claudit.BURN_TOKENS = claudit.LLM_SCRUB = True   # burn-tokens needs the LLM
    state = load_state()

    if args.dedup_guard:
        dedup_guard(state, args.repo, args.limit, args.apply)
        return

    if args.baseline:
        n = baseline(state)
        print(f"Baselined {n} findings as seen. Only NEW blocks will be filed from now.", file=sys.stderr)
        return

    if args.pending:
        pend = pending_sigs(state)
        print(f"{len(pend)} block(s) queued to file:", file=sys.stderr)
        for s in pend:
            print(f"  {s}")
        return

    if args.file_pending:
        n = file_pending(state, args.repo, not args.no_review, args.delay, notify)
        print(f"Filed {n} queued block(s).", file=sys.stderr)
        return

    if args.watch:
        if not acquire_singleton():
            sys.exit("claudit: another watcher is already running — refusing to start a second.")
        ensure_baseline(state, lambda a, r, u: print(f"  {r}", file=sys.stderr))

        mode = "AUTO-FILING new" if args.auto else "notify-only"
        bf = f" + adaptive backfill ({backlog_size(state)} queued)" if args.backfill else ""
        print(f"Watching {PROJECTS} ({mode}{bf}). Ctrl-C to stop.", file=sys.stderr)

        def on_detect(fresh):
            announce_pending(state, args.repo, args.delay)
        if not args.auto:
            announce_pending(state, args.repo, args.delay)   # surface anything already queued
        last_live, last_bf, bf_done = 0.0, 0.0, 0
        bf_delay = max(4.0, float(args.backfill_interval))
        try:
            while True:
                now = time.monotonic()
                if now - last_live >= args.interval:   # LIVE: new blocks fire as seen
                    last_live = now
                    if args.auto:
                        n = auto_cycle(state, args.repo, 0, notify)
                        if n:
                            print(f"  (+{n} filed)", file=sys.stderr)
                    else:
                        n = monitor_cycle(state, on_detect)
                        if n:
                            print(f"  (+{n} queued; run --file-pending to report)", file=sys.stderr)
                capped = args.backfill_max and bf_done >= args.backfill_max
                if args.backfill and not capped and now - last_bf >= bf_delay:
                    last_bf = now
                    b, limited = backfill_step(state, args.repo, 1, notify)
                    if limited:
                        bf_delay = min(bf_delay * 2, 300)
                        print(f"  backfill backing off -> {bf_delay:.0f}s", file=sys.stderr)
                    elif b:
                        bf_done += 1
                        bf_delay = max(bf_delay * 0.8, 4.0)
                        print(f"  (backfilled {bf_done}; {backlog_size(state)} left; ~{bf_delay:.0f}s/ea)",
                              file=sys.stderr)
                        if args.backfill_max and bf_done >= args.backfill_max:
                            print(f"  backfill cap ({args.backfill_max}) reached.", file=sys.stderr)
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)
        return

    # default: dry-run / --post backlog
    findings, logged = scan()
    new = sorted((f for s, f in findings.items() if s not in state),
                 key=lambda f: len(f["occ"]), reverse=True)
    if args.limit:
        new = new[:args.limit]
    print(f"\nLogged-only (never sent): {logged['overloaded']} overloaded, {logged['limit']} limit, "
          f"{logged['other']} other -> {ERROR_LOG}", file=sys.stderr)
    print(f"Cyber/AUP: {len(findings)} distinct, {len(new)} new.\n", file=sys.stderr)
    if not new:
        print("Nothing new.", file=sys.stderr)
        return
    for i, f in enumerate(new, 1):
        print(f"{i:>3}. [{f['kind']}] {len(reqs_of(f))} req-id(s), {len(f['occ'])}x")
    if not args.post:
        print("\nDry run. --baseline to seen them, --watch to monitor, --post to file backlog.", file=sys.stderr)
        return
    todo = [(f, "") for f in new] if args.no_review else review(new)
    if not todo:
        sys.exit("Nothing kept.")
    print(f"\nFiling {len(todo)} issue(s), {args.delay}s apart...", file=sys.stderr)
    for f, note in todo:
        try:
            action, ref, url = file_one(f, note, args.repo, state)
            save_state(state)
            notify(action or "skipped", ref or f["sig"], url)
        except Exception as e:
            print(f"  ! failed {f['sig']}: {e}", file=sys.stderr)
        time.sleep(args.delay)


if __name__ == "__main__":
    main()
