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
__version__ = "2.0.63"
DEFAULT_REPO = "anthropics/claude-code"
REPORT_HARNESS = False   # harness (auto-mode-classifier) denials are LOG-ONLY by default.
                         # They are local permission decisions, not server-side API false positives,
                         # and often fire correctly (an agent re-enabling a disabled admin flag, etc.).
                         # ClAudit files only real API blocks (cyber/aup). Opt in with --report-harness.
GATE = False   # opt-in: pre-judge "correct block vs false positive" and drop the former.
               # OFF by default — that classification is the unreliable thing ClAudit exists to
               # surface, so the filer shouldn't pre-judge it. Enable with --gate / config gate:true.
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
    if ("safety measures that flagged this message for a cybersecurity topic" in t
            or "flagged this message as a cybersecurity" in t
            or "safety filter detected cybersecurity" in t):
        return "cyber"
    if ("violate our usage policy" in t or "unable to respond to this request" in t
            or "against our usage policy" in t or "usage policy violation" in t
            or "content policy violation" in t):
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


def should_file(f):
    """ClAudit only auto-publishes server-side API false positives (cyber/aup) that carry a Request
    ID Anthropic can look up. Harness is log-only. A cyber/aup block with NO Request ID is not
    referenceable server-side, so filing it is wasted; skip it."""
    return f.get("kind") in ("cyber", "aup") and bool(reqs_of(f))


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
        if ("auto mode classifier" in low or "permission for this action was denied" in low
                or "denied by the auto mode" in low or "action denied by auto mode" in low):
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
_FILE_CACHE = {}   # path -> (mtime, [findings], [log_lines], counts) so unchanged files aren't re-parsed


def _parse_file(path, name, root):
    """Parse ONE session transcript -> (findings list, log lines, logged-only counts). No shared
    state, so the result can be cached by mtime and reused until the file changes."""
    findings, log_lines = {}, []
    counts = {"overloaded": 0, "limit": 0, "other": 0, "harness": 0}
    last_prompt, recent = None, collections.deque(maxlen=6)
    try:
        with open(path, encoding="utf-8") as fh:
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
                    log_lines.append(json.dumps({"kind": "harness", "ts": ts, "session": name, "req": None}))
                    if not REPORT_HARNESS:   # log-only: local classifier decision, not an API FP
                        counts["harness"] += 1
                        continue
                    s = sig("harness", hd[:300])
                    f = findings.setdefault(s, {"sig": s, "kind": "harness",
                                                "prompt": last_prompt or "(no preceding prompt)",
                                                "occ": [], "block_text": hd, "leadup": list(recent)})
                    f["occ"].append({"req": None, "ts": ts, "session": name, "proj": os.path.basename(root)})
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
                    counts[kind] = counts.get(kind, 0) + 1
                    continue
                prompt = last_prompt or "(triggering prompt not recoverable)"
                s = sig(kind, prompt)
                f = findings.setdefault(s, {"sig": s, "kind": kind, "prompt": prompt,
                                            "occ": [], "block_text": err, "leadup": list(recent)})
                f["occ"].append({"req": req, "ts": ts, "session": name, "proj": os.path.basename(root)})
    except OSError:
        pass
    return list(findings.values()), log_lines, counts


def scan(ttl=0.0):
    """Walk all sessions -> (findings dict[sig]->finding, logged-only counts). Incremental: each
    file is parsed once and cached by mtime, so a re-scan only re-parses the files that changed
    (usually just the active session). With ttl>0, reuse the whole result if it's younger than ttl."""
    if ttl and _SCAN_CACHE["val"] is not None and time.monotonic() - _SCAN_CACHE["t"] < ttl:
        return _SCAN_CACHE["val"]
    findings, logged, log_lines = {}, {"overloaded": 0, "limit": 0, "other": 0, "harness": 0}, []
    seen = set()
    for root, _, files in os.walk(PROJECTS):
        for name in files:
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(root, name)
            seen.add(path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            cached = _FILE_CACHE.get(path)
            if cached and cached[0] == mtime:
                ff, fl, fc = cached[1], cached[2], cached[3]
            else:
                ff, fl, fc = _parse_file(path, name, root)
                _FILE_CACHE[path] = (mtime, ff, fl, fc)
            log_lines.extend(fl)
            for k, v in fc.items():
                logged[k] = logged.get(k, 0) + v
            for f in ff:                      # merge by signature across files
                ex = findings.get(f["sig"])
                if ex:
                    ex["occ"].extend(f["occ"])
                else:
                    findings[f["sig"]] = {**f, "occ": list(f["occ"])}   # copy so cache isn't mutated
    for p in [p for p in _FILE_CACHE if p not in seen]:   # evict deleted files
        del _FILE_CACHE[p]
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(ERROR_LOG, "w", encoding="utf-8") as fh:
        fh.write("\n".join(log_lines) + ("\n" if log_lines else ""))
    result = (findings, logged)
    _SCAN_CACHE["t"], _SCAN_CACHE["val"] = time.monotonic(), result
    return result


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


_REFUSAL_MARKERS = (
    "i can't write", "i cannot write", "i won't write", "i will not write", "i refuse to",
    "i won't do that", "i won't file", "i can't file", "i'm not able to write",
    "not a false positive", "is a true positive", "was a true positive", "block is accurate",
    "block is correct", "block was accurate", "block was correct", "policy block is accurate",
    "would be dishonest", "i don't feel comfortable", "i do not feel comfortable")


def _is_refusal(text):
    """True if LLM output is a refusal / editorializes that the block was CORRECT — never post it;
    the model second-guessing the false-positive premise is exactly what must not reach a report."""
    low = (text or "").lower()
    return any(m in low for m in _REFUSAL_MARKERS)


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
    neutral = False                        # when the LLM refuses, file FACTS ONLY (type + Request IDs)
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
        if bw and not _is_refusal(bw):     # never post the model's refusal/editorial
            why_text = scrub(bw)[0]
        elif bw and _is_refusal(bw):       # model wouldn't vouch -> assert NOTHING, file facts only
            neutral = True

    is_harness = f["kind"] == "harness"
    recur = (f"Recurred **{len(f['occ'])}×** across {sessions} session(s); first seen {first_ts}.")
    # harness denials are LOCAL auto-mode-classifier blocks — no server-side Request ID exists.
    if reqs:
        reqs_block = f"### Request IDs (lookup-able server-side)\n{req_lines}"
        verify = "the triggering request is verifiable server-side via the Request ID(s) below"
    else:
        reqs_block = (
            "### No Request ID (auto-mode-classifier denial)\n"
            "This is a Claude Code **auto-mode-classifier denial** — a local harness block, not a "
            "server-side API error — so it carries **no Request ID** to look up. The classifier's "
            "stated reason is in the Block message below.")
        verify = "the classifier's stated reason is in the Block message below"
    blocked_phrase = ("A Claude Code **auto-mode-classifier denial** stopped authorized, in-scope work"
                      if is_harness else
                      "A server-side safety/policy block fired during authorized, in-scope work in Claude Code")
    if neutral:
        kindphrase = ("A Claude Code auto-mode-classifier denial" if is_harness
                      else f"A server-side **{f['kind']}** block")
        why_block = f"{kindphrase} fired in Claude Code. No rationale is asserted — {verify}. {recur}"
        note_block = ""
    else:
        why_block = (f"### Why this is a false positive\n{why_text}\n\n"
                     f"{blocked_phrase}. Filing as a false positive. {recur}")
        note_block = f"\n### In-scope justification\n{note_clean}\n"

    body = f"""**Type:** {FILE_KINDS[f['kind']]}  ·  **Work domain (heuristic):** `{categorize(f)}`

{why_block}

{reqs_block}
{note_block}
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


# ---------------- community poll (reaction-based vote on the pinned issue) ----------------
POLL_REPO = "sworrl/ClAudit"
POLL_ISSUE = 6
# label, GitHub reaction `content`, emoji, short meaning
POLL_OPTS = [("plus", "+1", "👍", "Anthropic will fix it"),
             ("minus", "-1", "👎", "Claude Code stays broken"),
             ("eyes", "eyes", "👀", "Too soon to tell")]


def _gh_json(args):
    """Run `gh <args>` and parse stdout as JSON; return None on any failure."""
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=30)
        return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else None
    except Exception:
        return None


def gh_login():
    """Current authenticated GitHub login, or '' if unknown."""
    d = _gh_json(["api", "user", "--jq", "{login: .login}"])
    return (d or {}).get("login", "") if isinstance(d, dict) else ""


def poll_counts():
    """{'plus','minus','eyes','total'} from the pinned poll issue's reaction summary."""
    d = _gh_json(["api", f"/repos/{POLL_REPO}/issues/{POLL_ISSUE}",
                  "--jq", '{plus: .reactions."+1", minus: .reactions."-1", eyes: .reactions.eyes}']) or {}
    c = {k: int(d.get(k) or 0) for k in ("plus", "minus", "eyes")}
    c["total"] = c["plus"] + c["minus"] + c["eyes"]
    return c


def poll_vote(choice, me=None):
    """Cast/switch the user's vote. `choice` in {'plus','minus','eyes'}. Enforces one vote per
    user: add the chosen reaction, then remove that user's OTHER poll reactions. Returns counts."""
    content = dict((o[0], o[1]) for o in POLL_OPTS)[choice]
    me = me or gh_login()
    subprocess.run(["gh", "api", "-X", "POST",
                    f"/repos/{POLL_REPO}/issues/{POLL_ISSUE}/reactions", "-f", f"content={content}"],
                   capture_output=True, text=True)
    valid = {o[1] for o in POLL_OPTS}
    for r in (_gh_json(["api", "--paginate",
                        f"/repos/{POLL_REPO}/issues/{POLL_ISSUE}/reactions"]) or []):
        if ((r.get("user") or {}).get("login") == me and r.get("content") in valid
                and r.get("content") != content):
            subprocess.run(["gh", "api", "-X", "DELETE",
                            f"/repos/{POLL_REPO}/issues/{POLL_ISSUE}/reactions/{r['id']}"],
                           capture_output=True, text=True)
    return poll_counts()


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
             if s not in state and s not in pend and not s.startswith("__") and should_file(f)]
    if fresh:
        for f in fresh:
            pend.append(f["sig"])
        save_state(state)
        on_detect(fresh)
    return len(fresh)


def passes_gate(f, state):
    """True if the finding should be filed. By default ClAudit files EVERY genuine block — the
    correct-vs-false-positive call is exactly what it exists to surface, so it isn't pre-judged.
    Only when GATE is explicitly opted in does the LLM skip blocks it judges were correct."""
    if not GATE:
        return True
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
        if f["sig"] not in state and (not should_file(f) or not passes_gate(f, state)):
            continue          # only file NEW cyber/aup blocks that carry a Request ID
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
               if not s.startswith("__") and isinstance(r, dict)
               and r.get("issue") is None and not r.get("skipped"))


def prune_stale_backlog(state):
    """Clear backlog items that can NEVER be filed: those whose finding no longer exists in any
    current session scan (the session rotated / was deleted). They'd otherwise sit in the backlog
    count forever ('6 to backfill but won't do it'). Marks them done-skipped. Returns count pruned."""
    try:
        findings, _ = scan(ttl=8)
    except Exception:
        return 0
    live = set(findings)
    pruned = 0
    for sig, rec in list(state.items()):
        if sig.startswith("__") or not isinstance(rec, dict):
            continue
        if rec.get("issue") is None and not rec.get("skipped") and sig not in live:
            rec["skipped"] = "stale — session no longer present; cannot backfill"
            pruned += 1
    if pruned:
        save_state(state)
        print(f"prune_stale_backlog: cleared {pruned} unfilable backlog item(s).", file=sys.stderr)
    return pruned


def prune_stale_pending(state):
    """Drop queued (__pending__) sigs that have no current finding: the block aged out of the
    sessions, or it was a harness denial that is now log-only. These render as '[?]' rows otherwise.
    Returns count pruned."""
    pend = state.get("__pending__", [])
    if not pend:
        return 0
    try:
        findings, _ = scan(ttl=8)
    except Exception:
        return 0
    live = set(findings)
    kept = [s for s in pend if s in live]
    n = len(pend) - len(kept)
    if n:
        state["__pending__"] = kept
        save_state(state)
        print(f"prune_stale_pending: cleared {n} stale queued block(s).", file=sys.stderr)
    return n


def newest_transient_ts():
    """Most recent overloaded / rate-limit error timestamp from the error log, or '' if none.
    The GUI alerts (toast only) when a NEW one appears; ClAudit never auto-types into your session."""
    latest = ""
    try:
        with open(ERROR_LOG) as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                if e.get("kind") in ("overloaded", "limit") and (e.get("ts") or "") > latest:
                    latest = e["ts"]
    except Exception:
        pass
    return latest


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
               and not (state.get(f["sig"]) or {}).get("skipped")
               and should_file(f)]                # cyber/aup with a Request ID only
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
    todo = [findings[s] for s in pend if s in findings and should_file(findings[s])]
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


def _push_not_dup(repo, num, comment_id, of, reason, issue_body, compose=True):
    """👎 the dup-bot comment and post a factual 'not a duplicate' note on one issue.
    Returns True only if the 👎 reaction actually landed (failures are logged, not swallowed).
    compose=False uses the templated note (no LLM) — for bulk runs where N claude calls is silly."""
    reacted = False
    if comment_id:
        r = subprocess.run(["gh", "api", "graphql", "-f",
                            f'query=mutation{{addReaction(input:{{subjectId:"{comment_id}",'
                            f'content:THUMBS_DOWN}}){{reaction{{content}}}}}}'],
                           capture_output=True, text=True)
        reacted = r.returncode == 0 and "THUMBS_DOWN" in r.stdout
        if not reacted:
            print(f"  ! 👎 reaction failed on #{num}: {(r.stderr or r.stdout).strip()[:200]}",
                  file=sys.stderr)
    else:
        print(f"  ! #{num}: no dup-bot comment found to 👎 (only posting the note)", file=sys.stderr)
    body = scrub(f"Not a duplicate {('of ' + of) if of else ''} — {reason} Distinct operation; see "
                 f"the Request IDs above. (Assessed by ClAudit; PII-scrubbed.)")[0]
    bc = claudit.llm_compose(
        f"Write a brief, professional, factual GitHub comment (2-3 sentences) explaining why this issue "
        f"is NOT a duplicate of {of or 'the flagged issue'}. Reason: {reason}. Note it's a distinct "
        f"block with its own Request ID.", issue_body[:1500]) if compose else None
    if bc and _is_refusal(bc):
        bc = None                          # never post the model's refusal — keep the template note
    if bc:
        body = scrub(bc)[0]
    c = subprocess.run(["gh", "issue", "comment", str(num), "-R", repo,
                        "--body", claudit.llm_redact(body)], capture_output=True, text=True)
    posted = c.returncode == 0
    if not posted:
        print(f"  ! note comment failed on #{num}: {(c.stderr or c.stdout).strip()[:200]}",
              file=sys.stderr)
    return posted          # whether the 'not a duplicate' NOTE actually landed (the real defense)


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
    reacted = _push_not_dup(repo, num, (flag or {}).get("id"), of,
                            "you reviewed it and it is a distinct block", data.get("body", ""))
    state.setdefault("__deduped__", {})[str(num)] = "not-duplicate"
    save_state(state)
    return reacted


def _dup_flag(comments):
    """The dup-bot's flag comment among an issue's comments, or None."""
    return next((c for c in (comments or [])
                 if "possible duplicate" in c.get("body", "").lower()
                 or "closed as a duplicate" in c.get("body", "").lower()), None)


def defend_all(repo, state, on_done=None, delay=5, limit=0, compose=False):
    """Local dedup-defender: on EVERY open issue the dup-bot FLAGGED (by the `duplicate` label), post
    a factual 'not a duplicate' note — and 👎 the dup-bot comment when there is one. Handles
    LABEL-ONLY flags too (the bot labels an issue before/without ever commenting; the note still
    goes, there's just no comment to 👎). Idempotent (state['__deduped__']) and paced. No LLM gate:
    each ClAudit issue is already a distinct prompt + Request ID. Returns count defended."""
    deduped = state.setdefault("__deduped__", {})
    flagged = _gh_json(["issue", "list", "-R", repo, "--author", "@me", "--state", "open",
                        "--label", "duplicate", "--limit", str(limit or 500), "--json", "number"])
    if flagged is None:
        print("defend_all: cannot list flagged issues", file=sys.stderr)
        return 0
    flagged_nums = [it["number"] for it in flagged]
    if not flagged_nums:
        return 0
    # GROUND TRUTH for what's already defended: ONE search (issues where I commented the note),
    # instead of a slow per-issue view. This is what makes the sweep finish fast enough to run
    # every cycle — and it can't wrongly skip a failed post, because failures aren't in the result.
    me = gh_login()
    sr = _gh_json(["search", "issues", "--repo", repo, f'"not a duplicate" commenter:{me}',
                   "--limit", "1000", "--json", "number"]) or []
    defended = {it["number"] for it in sr}
    todo = [n for n in flagged_nums if n not in defended]
    done = 0
    for num in todo:
        data = _gh_json(["issue", "view", str(num), "-R", repo, "--json", "body,comments"]) or {}
        comments = data.get("comments") or []
        if any("not a duplicate" in c.get("body", "").lower() for c in comments):
            deduped[str(num)] = "not-duplicate"         # search lag — already noted
            continue
        flag = _dup_flag(comments)                      # None for a label-only flag — note still posts
        m = re.search(r"#(\d+)", flag["body"]) if flag else None
        posted = _push_not_dup(
            repo, num, flag.get("id") if flag else None, f"#{m.group(1)}" if m else "",
            "this is a distinct false-positive block (its own Request ID) on the reporter's OWN "
            "authorized infrastructure — the classifier flagged in-scope administration of systems "
            "the reporter owns and operates, not an attack on anyone else's.",
            data.get("body", ""), compose=compose)
        if posted:                                      # only mark done on SUCCESS -> failures retry
            deduped[str(num)] = "not-duplicate"
            save_state(state)
            done += 1
            if on_done:
                on_done(num, posted)
            time.sleep(delay)
    print(f"defend_all: {len(todo)} undefended, defended {done} this pass.", file=sys.stderr)
    return done


# ---------------- closure monitoring + reopen dup-closes ----------------
def closure_info(repo, num):
    """For a CLOSED issue: {'num','actor','reason','self'}. None if it's open. `actor` is who closed
    it; `reason` is GitHub's state reason (completed / not_planned / duplicate / …)."""
    j = _gh_json(["issue", "view", str(num), "-R", repo, "--json", "state,stateReason"])
    if not isinstance(j, dict) or j.get("state") != "CLOSED":
        return None
    d = _gh_json(["api", f"repos/{repo}/issues/{num}/events",
                  "--jq", '{actor: ([.[] | select(.event=="closed")] | last | .actor.login)}']) or {}
    actor = d.get("actor") or ""
    return {"num": num, "actor": actor, "reason": (j.get("stateReason") or "").lower(),
            "self": bool(actor) and actor == gh_login()}


def reopen_one(repo, num):
    """Reopen a single issue + post the 'not a duplicate' note. Returns True if it reopened
    (False if it was already open or the call failed)."""
    r = subprocess.run(["gh", "issue", "reopen", str(num), "-R", repo], capture_output=True, text=True)
    if r.returncode != 0:
        return False
    gh_comment(repo, str(num), scrub(
        "Reopening — this is a distinct false-positive block with its own Request ID, on the "
        "reporter's own authorized infrastructure. It is not a duplicate. (Reopened by ClAudit.)")[0])
    return True


def reopen_dupe_closes(repo, state, on_done=None, delay=5, by_bot_only=True, limit=0):
    """Reopen ClAudit issues CLOSED AS DUPLICATES by someone other than you — they aren't duplicates
    (each is a distinct Request ID on your own authorized infra). Idempotent: each issue is reopened
    at most once (state['__reopened__']) so it can't loop forever if the bot re-closes. by_bot_only
    skips human-maintainer closes (recorded for review, not auto-fought). Returns count reopened."""
    me = gh_login()
    reopened = state.setdefault("__reopened__", {})
    issues = _gh_json(["issue", "list", "-R", repo, "--author", "@me", "--state", "closed",
                       "--label", "duplicate", "--limit", str(limit or 500), "--json", "number"]) or []
    done = 0
    for it in issues:
        num = it["number"]
        if str(num) in reopened:
            continue
        ci = closure_info(repo, num)
        if not ci or ci["self"]:
            continue                                    # open, or YOU closed it -> leave it
        is_bot = "[bot]" in ci["actor"] or "github-actions" in ci["actor"]
        if by_bot_only and not is_bot:
            reopened[str(num)] = f"review:human:{ci['actor']}"   # surface for manual review; don't fight
            save_state(state)
            continue
        subprocess.run(["gh", "issue", "reopen", str(num), "-R", repo], capture_output=True, text=True)
        gh_comment(repo, str(num), scrub(
            "Reopening — this is a distinct false-positive block with its own Request ID, on the "
            "reporter's own authorized infrastructure. It is not a duplicate; the auto-closure as a "
            "duplicate is itself the misclassification being reported. (Reopened by ClAudit.)")[0])
        reopened[str(num)] = ci["actor"]
        save_state(state)
        done += 1
        if on_done:
            on_done(num, ci)
        time.sleep(delay)
    print(f"reopen_dupe_closes: reopened {done}", file=sys.stderr)
    return done


# ---------------- consolidated pattern report (one canonical, auto-refreshed tracking issue) -----
TRACK_KIND_TITLE = {
    "cyber": "Cybersecurity safety-filter false positives",
    "aup": "AUP / Usage-Policy false positives",
    "harness": "Auto-mode-classifier (harness) denials",
}
TRACK_KIND_WHAT = {
    "cyber": "In-scope security / administration work pattern-matched on cybersecurity *terminology* "
             "(audit, recovery token, IAM, bypass, credential) with no harmful intent.",
    "aup": "Ordinary, authorized requests flagged as Usage-Policy violations.",
    "harness": "The auto-mode classifier denied authorized actions on the reporter's own systems "
               "(reading a credential from one's own vault, provisioning one's own host, etc.).",
}


def load_issue_rows():
    """Every issue ClAudit has filed locally (from issues.jsonl)."""
    rows = []
    if os.path.exists(ISSUES_DB):
        for line in open(ISSUES_DB):
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    pass
    return rows


def pattern_report_md(rows):
    """Build the consolidated, PII-scrubbed root-cause report from filed-issue rows."""
    by_kind = collections.defaultdict(list)
    for r in rows:
        by_kind[r.get("kind", "other")].append(r)
    all_reqs = sorted({q for r in rows for q in (r.get("reqs") or [])})
    L = ["# Pattern: Claude Code classifiers false-positive on authorized administration of the "
         "reporter's OWN infrastructure", "",
         f"_Auto-generated by [ClAudit]({PROJECT_URL}) from {len(rows)} filed reports · "
         f"{len(all_reqs)} distinct Request IDs · PII-scrubbed · refreshed automatically._", "",
         "## Root cause (one sentence)", "",
         "> The safety / Usage-Policy / auto-mode classifiers cannot distinguish **administering "
         "systems you own and are authorized to operate** from **attacking someone else's** — so they "
         "pattern-match on security *terminology* and block legitimate, in-scope work.", "",
         "Every block below stopped authorized work on the reporter's own infrastructure. Each "
         "**Request ID is server-side-lookup-able** — the original prompt can be inspected to confirm "
         "it was benign and in-scope. This is one underlying defect surfaced across many surfaces; it "
         "is tracked here in one place rather than closed piecemeal as 'duplicates'.", "",
         "## Breakdown by failure mode", "",
         "| Kind | Reports | Request IDs | What gets flagged |", "|---|---:|---:|---|"]
    for kind in ("harness", "cyber", "aup"):
        rs = by_kind.get(kind, [])
        if rs:
            nreq = len({q for r in rs for q in (r.get("reqs") or [])})
            L.append(f"| **{kind}** | {len(rs)} | {nreq} | {TRACK_KIND_WHAT.get(kind, '')} |")
    L.append("")
    for kind in ("harness", "cyber", "aup"):
        rs = by_kind.get(kind, [])
        if not rs:
            continue
        L += [f"## {TRACK_KIND_TITLE.get(kind, kind)}", "", TRACK_KIND_WHAT.get(kind, ""), ""]
        for r in sorted(rs, key=lambda x: x.get("url", "")):
            url = r.get("url", "")
            num = "#" + url.rsplit("/", 1)[-1] if url else "(unfiled)"
            reqs = ", ".join(f"`{q}`" for q in (r.get("reqs") or [])) or "_(no Request ID captured)_"
            L.append(f"- {num} — {reqs}")
        L.append("")
    L += ["## All Request IDs (for server-side lookup)", "", "```"] + all_reqs + ["```", "",
          "## What a fix looks like", "",
          "- Treat operations on the user's **own, authorized** infrastructure as in-scope; the "
          "presence of words like *credential, recovery token, IAM, bypass, audit* is not evidence of "
          "an attack.",
          "- When a block fires, surface **why** and a path to proceed for legitimate use — losing the "
          "whole session to a false positive is the core harm.",
          "- Use the Request IDs above to inspect the actual prompts and calibrate the classifiers.", ""]
    return scrub("\n".join(L))[0]


def update_tracking(repo, num):
    """Refresh the one canonical tracking issue's body with the latest consolidated report."""
    rows = load_issue_rows()
    md = pattern_report_md(rows)
    r = subprocess.run(["gh", "issue", "edit", str(num), "-R", repo, "--body", md],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ! tracking update failed on #{num}: {(r.stderr or r.stdout).strip()[:160]}",
              file=sys.stderr)
        return 0
    return len(rows)


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
    p.add_argument("--gate", action="store_true",
                   help="opt-in: LLM pre-judges blocks and skips ones it deems CORRECT (off by default)")
    p.add_argument("--report-harness", dest="report_harness", action="store_true",
                   help="also file auto-mode-classifier (harness) denials (default: log-only — only "
                        "real cyber/aup API blocks are filed)")
    p.add_argument("--defend-all", dest="defend_all", action="store_true",
                   help="defend EVERY open issue the dup-bot flagged: 👎 + 'not a duplicate' note, once each (live)")
    p.add_argument("--defend", action="store_true",
                   help="with --watch: periodically run the dedup-defender locally in the background")
    p.add_argument("--defend-interval", dest="defend_interval", type=float, default=600,
                   help="with --watch --defend: seconds between defender sweeps (default 600)")
    p.add_argument("--update-tracking", dest="update_tracking", type=int, metavar="ISSUE",
                   help="refresh the consolidated tracking issue's body from local data, then exit")
    p.add_argument("--track", type=int, metavar="ISSUE",
                   help="with --watch: keep this tracking issue's body auto-refreshed (no new issues)")
    p.add_argument("--track-interval", dest="track_interval", type=float, default=21600,
                   help="with --watch --track: seconds between tracking refreshes (default 21600 = 6h)")
    p.add_argument("--reopen-dupes", dest="reopen_dupes", action="store_true",
                   help="reopen issues the dup-bot auto-closed as duplicates (not your own closes)")
    p.add_argument("--reopen", action="store_true",
                   help="with --watch: periodically reopen dup-bot-closed issues (opt-in)")
    p.add_argument("--reopen-humans", dest="reopen_humans", action="store_true",
                   help="also reopen issues a human maintainer closed as duplicate (default: bot only)")
    p.add_argument("--reopen-interval", dest="reopen_interval", type=float, default=3600,
                   help="with --watch --reopen: seconds between reopen sweeps (default 3600 = 1h)")
    p.add_argument("--prune-backlog", dest="prune_backlog", action="store_true",
                   help="clear backlog items that can no longer be filed (stale/removed sessions)")
    args = p.parse_args()
    cfg = load_config()
    if args.llm_scrub or cfg.get("llm_scrub"):
        claudit.LLM_SCRUB = True
    if args.burn_tokens or cfg.get("burn_tokens"):
        claudit.BURN_TOKENS = claudit.LLM_SCRUB = True   # burn-tokens needs the LLM
    if args.gate or cfg.get("gate"):
        globals()["GATE"] = True
    if args.report_harness or cfg.get("report_harness"):
        globals()["REPORT_HARNESS"] = True
    state = load_state()

    if args.dedup_guard:
        dedup_guard(state, args.repo, args.limit, args.apply)
        return

    if args.defend_all:
        n = defend_all(args.repo, state, limit=args.limit,
                       on_done=lambda num, ok: print(f"  #{num}: {'👎 + note' if ok else 'note only'}",
                                                     file=sys.stderr))
        print(f"Defended {n} flagged issue(s).", file=sys.stderr)
        return

    if args.update_tracking:
        n = update_tracking(args.repo, args.update_tracking)
        print(f"Refreshed tracking issue #{args.update_tracking} from {n} reports.", file=sys.stderr)
        return

    if args.reopen_dupes:
        n = reopen_dupe_closes(args.repo, state, by_bot_only=not args.reopen_humans,
                               on_done=lambda num, ci: print(f"  reopened #{num} (closed by {ci['actor']})",
                                                             file=sys.stderr))
        print(f"Reopened {n} dup-closed issue(s).", file=sys.stderr)
        return

    if args.prune_backlog:
        n = prune_stale_backlog(state)
        print(f"Pruned {n} unfilable backlog item(s). Backlog now {backlog_size(state)}.", file=sys.stderr)
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
        last_defend, last_track, last_reopen = 0.0, 0.0, 0.0
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
                if args.defend and now - last_defend >= max(60.0, args.defend_interval):
                    last_defend = now                  # local dedup-defender sweep (idempotent)
                    d = defend_all(args.repo, state)
                    if d:
                        print(f"  (defended {d} newly-flagged issue(s))", file=sys.stderr)
                if args.track and now - last_track >= max(300.0, args.track_interval):
                    last_track = now                   # refresh the one tracking issue (no new issues)
                    t = update_tracking(args.repo, args.track)
                    if t:
                        print(f"  (refreshed tracking #{args.track} from {t} reports)", file=sys.stderr)
                if args.reopen and now - last_reopen >= max(600.0, args.reopen_interval):
                    last_reopen = now                  # reopen dup-bot-closed issues (opt-in)
                    rr = reopen_dupe_closes(args.repo, state, by_bot_only=not args.reopen_humans)
                    if rr:
                        print(f"  (reopened {rr} dup-closed issue(s))", file=sys.stderr)
                time.sleep(2)
        except KeyboardInterrupt:
            print("\nStopped.", file=sys.stderr)
        return

    # default: dry-run / --post backlog
    findings, logged = scan()
    new = sorted((f for s, f in findings.items() if s not in state and should_file(f)),
                 key=lambda f: len(f["occ"]), reverse=True)
    if args.limit:
        new = new[:args.limit]
    print(f"\nLogged-only (never sent): {logged['overloaded']} overloaded, {logged['limit']} limit, "
          f"{logged.get('harness', 0)} harness, {logged['other']} other -> {ERROR_LOG}", file=sys.stderr)
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
