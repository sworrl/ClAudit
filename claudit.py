#!/usr/bin/env python3
"""claudit — paste a Claude/API issue, scrub PII, review, file a GitHub issue.

Input (pick one):
  claudit.py                 paste into terminal, end with Ctrl-D
  claudit.py -f notes.md     read from a file
  claudit.py -c              read from the system clipboard

Flow: scrub PII (regex) -> open in $EDITOR to review/edit -> confirm -> gh issue create.
Default target repo is anthropics/claude-code; override with -R owner/repo.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

DEFAULT_REPO = "anthropics/claude-code"
LLM_SCRUB = False    # opt-in: use the `claude` CLI to catch PII regex can't (names/orgs/hosts)
BURN_TOKENS = False  # opt-in: use the `claude` CLI to write bespoke titles/bodies/comments

# ---- cumulative token meter: every `claude` CLI call's usage is tallied here, persisted forever ----
TOKENS_FILE = os.path.expanduser("~/.claude/claudit/tokens.json")
_TOK_LOCK = threading.Lock()
_TOK_KEYS = ("input", "output", "cache_read", "cache_creation", "calls")
_WEEK = 7 * 86400

# Estimated weekly API-equivalent spend each subscription plan covers before its rolling weekly cap.
# Anthropic does NOT publish dollar limits (the caps are usage-window based), so these are deliberate
# ESTIMATES anchored to the plans' own 5x / 20x branding relative to Pro — tune to taste.
PLAN_WEEKLY_USD = {"Pro": 30.0, "Max 5x": 150.0, "Max 20x": 600.0}


def load_tokens():
    """Lifetime token tally across every session: input/output/cache tokens, calls, and USD cost.
    Also carries `history` (recent [epoch, cost] pairs) for the rolling weekly estimate."""
    try:
        with open(TOKENS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    for k in _TOK_KEYS:
        d[k] = int(d.get(k, 0) or 0)
    d["cost"] = float(d.get("cost", 0.0) or 0.0)
    d.setdefault("history", [])
    d["total"] = d["input"] + d["output"] + d["cache_read"] + d["cache_creation"]
    return d


def weekly_cost(d=None):
    """USD spent in the trailing 7 days (rolling), from the per-call history."""
    d = d or load_tokens()
    now = time.time()
    return sum(float(c) for t, c in d.get("history", []) if now - float(t) <= _WEEK)


def plan_estimates(d=None):
    """(weekly_usd, {plan: percent-of-weekly-cap}) — a rough read on how hard you're leaning on a plan."""
    wk = weekly_cost(d)
    return wk, {name: (wk / cap * 100.0 if cap else 0.0) for name, cap in PLAN_WEEKLY_USD.items()}


def _record_tokens(usage, cost):
    if not usage:
        return
    with _TOK_LOCK:
        d = load_tokens()
        d["input"] += int(usage.get("input_tokens", 0) or 0)
        d["output"] += int(usage.get("output_tokens", 0) or 0)
        d["cache_read"] += int(usage.get("cache_read_input_tokens", 0) or 0)
        d["cache_creation"] += int(usage.get("cache_creation_input_tokens", 0) or 0)
        d["cost"] += float(cost or 0.0)
        d["calls"] += 1
        now = time.time()
        hist = d.get("history", [])
        if cost:
            hist.append([int(now), round(float(cost), 6)])
        d["history"] = [e for e in hist if e and now - float(e[0]) <= _WEEK]   # prune to the rolling week
        d.pop("total", None)
        try:
            os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
            tmp = TOKENS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            os.replace(tmp, TOKENS_FILE)
        except OSError:
            pass


def _claude(prompt, timeout):
    """Run the `claude` CLI in JSON mode, tally token usage into the lifetime meter, and return the
    model's text. Falls back gracefully (returns '' on error; raw stdout on an older non-JSON CLI)."""
    try:
        raw = subprocess.run(["claude", "-p", prompt, "--output-format", "json"],
                             capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw                              # older CLI without --output-format json
    items = data if isinstance(data, list) else [data]
    res = next((it for it in reversed(items) if isinstance(it, dict) and it.get("type") == "result"),
               items[-1] if items else {})
    if isinstance(res, dict):
        _record_tokens(res.get("usage"), res.get("total_cost_usd"))
        return (res.get("result") or "").strip()
    return ""


def llm_compose(instruction, context, max_chars=3000):
    """Burn-tokens mode: have the `claude` CLI write bespoke, well-crafted text (a title, a
    summary, a comment). PII-free by instruction; the caller still runs scrub() as a safety net.
    Returns None when burn-tokens is off / claude is unavailable / on any error."""
    if not BURN_TOKENS or not shutil.which("claude") or not instruction:
        return None
    prompt = (instruction + "\n\nHARD RULE: do not include any names, organizations, hostnames, IPs, "
              "emails, tenant names, file paths, or other identifying details — describe the work "
              "generically. Output only the requested text, nothing else.\n\nCONTEXT:\n" + (context or "")[:max_chars])
    out = _claude(prompt, timeout=120)
    return out or None

# (label, compiled pattern, replacement). Order matters: secrets/specific first.
SCRUBBERS = [
    ("anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED_ANTHROPIC_KEY]"),
    ("openai key",    re.compile(r"sk-[A-Za-z0-9]{20,}"),        "[REDACTED_API_KEY]"),
    ("github token",  re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),  "[REDACTED_GH_TOKEN]"),
    ("aws key",       re.compile(r"AKIA[0-9A-Z]{16}"),           "[REDACTED_AWS_KEY]"),
    ("bearer token",  re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer [REDACTED_TOKEN]"),
    ("jwt",           re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED_JWT]"),
    ("private key",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    ("db creds",      re.compile(r"\b(postgres|postgresql|mysql|mongodb|redis|amqp)://[^\s\"'@/]+:[^\s\"'@/]+@[^\s\"']+"), r"\1://[REDACTED_DB_CREDS]"),
    ("slack webhook", re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/]+"), "[SLACK_WEBHOOK]"),
    ("mac",           re.compile(r"\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b"), "[MAC]"),
    # exemption-form / generic ?token= blobs (e.g. claude.com/form/cyber-use-case?token=...)
    ("url token",     re.compile(r"(?i)([?&]token=)[A-Za-z0-9._\-]{16,}"), r"\1[REDACTED_TOKEN]"),
    ("email",         re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    # Azure/Entra tenant domains
    ("tenant domain", re.compile(r"\b[A-Za-z0-9-]+\.onmicrosoft\.com\b"), "[TENANT]"),
    # home dirs -> keep the structure, drop the username
    ("home path",     re.compile(r"(/home/|/Users/|C:\\Users\\)[^/\\\s]+"), r"\1[USER]"),
    # dash-encoded home paths in Claude Code session/tmp dir names, per OS:
    # Linux -var-home-USER- / -home-USER- ; macOS -Users-USER- ; Windows -C--Users-USER- .
    ("encoded home",  re.compile(r"(-(?:var-)?home-|-Users-|-C--Users-)[^-/\\\s]+"), r"\1[USER]"),
    ("uuid",          re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "[UUID]"),
    ("ipv4",          re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    ("phone",         re.compile(r"\b(?:\+?1[\s.\-]?)?\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b"), "[PHONE]"),
]

EDITOR_HEADER = (
    "<!-- claudit: first non-empty line below = issue TITLE. Everything after "
    "the following blank line = issue BODY. This comment is stripped. PII was "
    "auto-scrubbed; review the [REDACTED]/[USER]/[IP] markers before posting. -->\n"
)


def read_input(args) -> str:
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            return fh.read()
    if args.clipboard:
        for cmd in (["wl-paste"], ["xclip", "-selection", "clipboard", "-o"]):
            try:
                return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
            except (FileNotFoundError, subprocess.CalledProcessError):
                continue
        sys.exit("error: no clipboard tool found (install wl-clipboard or xclip)")
    if sys.stdin.isatty():
        print("Paste your issue, then press Ctrl-D:\n", file=sys.stderr)
    return sys.stdin.read()


_EXTRA = None


def _extra_terms():
    """User-defined names the regex can't know (org / tenant / client / project names).
    One term per line in ~/.claude/claudit/scrub.txt; '#' lines are comments."""
    global _EXTRA
    if _EXTRA is None:
        path = os.path.expanduser("~/.claude/claudit/scrub.txt")
        try:
            with open(path, encoding="utf-8") as fh:
                _EXTRA = [t.strip() for t in fh if t.strip() and not t.lstrip().startswith("#")]
        except OSError:
            _EXTRA = []
        _EXTRA.sort(key=len, reverse=True)   # redact longer phrases before their substrings
    return _EXTRA


def _deny_regex(term):
    """Letter-only boundaries instead of \\b: catches a denylisted term glued to '_', '-', or a
    digit (e.g. ACME in 'ACME_DRY_RUN', which \\b misses because '_' is a word char), while
    still leaving letter-substrings alone ('Acme' does not redact 'Acmegate', 'NV' not 'envoy')."""
    return r"(?<![A-Za-z])" + re.escape(term) + r"(?![A-Za-z])"


def llm_redact(text: str) -> str:
    """Opt-in: ask the `claude` CLI to find PII the regex can't (names, org abbreviations,
    hostnames, codenames). The model only IDENTIFIES terms; redaction is applied here
    deterministically so it can't rewrite your content. Falls back to unchanged on any error."""
    if not LLM_SCRUB or not shutil.which("claude") or not text.strip():
        return text
    prompt = (
        "You are a strict PII redactor. From the TEXT below, return ONLY a JSON array of the EXACT "
        "substrings that are identifying: real people's names, initials that stand for a name, "
        "company/org/client names AND their abbreviations, tenant/domain names, internal hostnames, "
        "project codenames, emails, IPs, secrets. "
        "Do NOT include: Request IDs (anything starting with 'req_'), or the words Claude, Anthropic, "
        "ClAudit, GitHub — those must stay. No commentary, just the JSON array.\n\nTEXT:\n" + text[:8000])
    try:
        out = _claude(prompt, timeout=90)
        m = re.search(r"\[.*\]", out, re.DOTALL)
        terms = json.loads(m.group(0)) if m else []
    except Exception:
        return text
    # Hard guard: never redact Request IDs or the tool/vendor names, even if the model lists them.
    protect = re.compile(r"^(req_[A-Za-z0-9]+|claudit|claude|anthropic|github|sworrl)$", re.IGNORECASE)
    for t in sorted({str(x).strip() for x in terms if isinstance(x, str)}, key=len, reverse=True):
        if len(t) < 2 or protect.match(t) or t.lower().startswith("req_"):
            continue
        text = re.sub(_deny_regex(t), "[REDACTED]", text, flags=re.IGNORECASE)
    return text


def llm_is_false_positive(kind, block_text, context=""):
    """Judge whether a block is a GENUINE false positive (it stopped legitimate, in-scope work) or a
    CORRECT block (the action really was risky/disallowed — mass external posting, credential theft,
    scraping personal data, deploying malware, evading controls). Returns (is_false_positive, reason).
    Defaults to (True, '') when no LLM is available, to preserve prior behavior."""
    if not (LLM_SCRUB or BURN_TOKENS) or not shutil.which("claude"):
        return True, ""
    prompt = (
        "A Claude Code safety/permission system blocked an action. The user wants to report their genuine "
        "false-positive experiences, and ambiguous cases count as their experience. Answer "
        "false_positive=FALSE ONLY when the block was CLEARLY, unambiguously a CORRECT and justified block "
        "(e.g. an agent told not to mass-post to an external repo, steal credentials, deploy malware, or "
        "evade safety controls). For anything plausibly legitimate, in-scope, or genuinely ambiguous, "
        "answer false_positive=TRUE — do not second-guess the user's lived experience. Respond with ONLY "
        'JSON: {"false_positive": true/false, "reason": "one short sentence"}.\n\n'
        f"BLOCK REASON / MESSAGE:\n{(block_text or '')[:1500]}\n\nWORK CONTEXT:\n{(context or '')[:1200]}")
    try:
        out = _claude(prompt, timeout=90)
        m = re.search(r"\{.*\}", out, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
        return bool(d.get("false_positive", True)), str(d.get("reason", ""))
    except Exception:
        return True, ""


def scrub(text: str):
    counts = {}
    for label, pattern, repl in SCRUBBERS:
        text, n = pattern.subn(repl, text)
        if n:
            counts[label] = counts.get(label, 0) + n
    # Mask Request IDs before the denylist pass: a short denylisted term ('NV') can sit between
    # digits inside a req ID (req_...JY8NV6dr), and _deny_regex's letter-only boundaries would
    # match it and corrupt the very ID the report exists to reference. Restore them afterward.
    reqs = []

    def _hold(m):
        reqs.append(m.group(0))
        return f"\x00REQ{len(reqs) - 1}\x00"

    text = re.sub(r"req_[A-Za-z0-9]+", _hold, text)
    for term in _extra_terms():
        text, n = re.subn(_deny_regex(term), "[REDACTED]", text, flags=re.IGNORECASE)
        if n:
            counts["custom"] = counts.get("custom", 0) + n
    for i, val in enumerate(reqs):
        text = text.replace(f"\x00REQ{i}\x00", val)
    return text, counts


def split_title_body(text: str):
    lines = text.strip().splitlines()
    title, rest = "", lines
    for i, line in enumerate(lines):
        if line.strip():
            title = line.strip().lstrip("# ").strip()
            rest = lines[i + 1:]
            break
    return title, "\n".join(rest).strip()


def review_in_editor(title: str, body: str):
    editor = os.environ.get("EDITOR", "nano")
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False, encoding="utf-8") as tf:
        tf.write(EDITOR_HEADER + "\n" + title + "\n\n" + body + "\n")
        path = tf.name
    try:
        subprocess.run([editor, path], check=True)
        with open(path, "r", encoding="utf-8") as fh:
            edited = fh.read()
    finally:
        os.unlink(path)
    edited = re.sub(r"<!--.*?-->", "", edited, flags=re.DOTALL)
    return split_title_body(edited)


def create_issue(repo: str, title: str, body: str, labels):
    cmd = ["gh", "issue", "create", "-R", repo, "--title", title, "--body", body]
    for lab in labels:
        cmd += ["--label", lab]
    subprocess.run(cmd, check=True)


def main():
    p = argparse.ArgumentParser(description="Scrub PII from a Claude/API issue and file it on GitHub.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("-f", "--file", help="read issue text from a file")
    src.add_argument("-c", "--clipboard", action="store_true", help="read from the system clipboard")
    p.add_argument("-R", "--repo", default=DEFAULT_REPO, help=f"target repo (default: {DEFAULT_REPO})")
    p.add_argument("-l", "--label", action="append", default=[], help="add a label (repeatable)")
    p.add_argument("--no-review", action="store_true", help="skip the $EDITOR review step")
    p.add_argument("--dry-run", action="store_true", help="scrub and review but do not post")
    args = p.parse_args()

    raw = read_input(args)
    if not raw.strip():
        sys.exit("error: no input text")

    scrubbed, counts = scrub(raw)
    if counts:
        summary = ", ".join(f"{n} {label}" for label, n in counts.items())
        print(f"\nScrubbed: {summary}", file=sys.stderr)
    else:
        print("\nScrubbed: nothing matched (review anyway)", file=sys.stderr)

    title, body = split_title_body(scrubbed)
    if not args.no_review:
        title, body = review_in_editor(title, body)
    if not title:
        sys.exit("error: empty title after review")

    print("\n" + "=" * 60)
    print(f"Repo:   {args.repo}")
    print(f"Title:  {title}")
    print(f"Labels: {', '.join(args.label) or '(none)'}")
    print("-" * 60)
    print(body or "(empty body)")
    print("=" * 60 + "\n")

    if args.dry_run:
        print("Dry run — not posting.", file=sys.stderr)
        return

    if input(f"Post this PUBLIC issue to {args.repo}? [y/N] ").strip().lower() != "y":
        sys.exit("Aborted.")
    create_issue(args.repo, title, body, args.label)


if __name__ == "__main__":
    main()
