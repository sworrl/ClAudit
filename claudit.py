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
import urllib.error
import urllib.request

DEFAULT_REPO = "anthropics/claude-code"
LLM_SCRUB = False    # opt-in: use the LLM CLI to catch PII regex can't (names/orgs/hosts)
BURN_TOKENS = False  # opt-in: use the LLM CLI to write bespoke titles/bodies/comments
LLM_ENGINE = "auto"  # LLM engine choice: "auto", "claude", "agy", or "tandem" (both, cross-checking)
# Defaults for ClAudit's own LLM calls (compose/scrub/gate/verdict are simple, well-scoped tasks).
# Haiku 4.5 at medium effort handles them fine, FAST, at a fraction of Sonnet's cost — verified in
# real use 2026-07. Override via config llm_model / llm_effort; set LLM_MODEL = "" to inherit the
# CLI session default.
LLM_MODEL = "claude-haiku-4-5-20251001"
LLM_EFFORT = "medium"
# Every non-interactive agy call is filed under this Antigravity project, so ClAudit's hundreds of
# compose/scrub/gate runs stay OUT of the default chat history and manual conversations remain easy
# to find and resume. Override with config `agy_project`; set to "" to use the default project.
AGY_PROJECT = "ClAudit"
# In agy-ONLY mode (llm_engine "agy"), the tandem cross-checks don't have a second CLI — instead
# the second voice is agy itself on this model (a different family than the session default), so
# review/veto diversity survives without spending any claude-CLI quota. "" disables the second
# voice. Override with config `agy_review_model`; `agy models` lists the choices.
AGY_REVIEW_MODEL = "gemini-3.1-pro-low"

# ---- cumulative token meter: every LLM CLI call's usage is tallied here, persisted forever ----
TOKENS_FILE = os.path.expanduser("~/.claude/claudit/tokens.json")
_TOK_LOCK = threading.Lock()
_TOK_KEYS = ("input", "output", "cache_read", "cache_creation", "calls")
_WEEK = 7 * 86400

# Estimated weekly API-equivalent spend each subscription plan covers before its rolling weekly cap.
# Anthropic does NOT publish dollar limits (the caps are usage-window based), so these are deliberate
# ESTIMATES anchored to the plans' own 5x / 20x branding relative to Pro — tune to taste.
PLAN_WEEKLY_USD = {"Pro": 30.0, "Max 5x": 150.0, "Max 20x": 600.0}


def available_engines():
    """Ordered list of engine CLIs this run will actually use. 'tandem' and 'auto' use every
    installed engine (agy first, so agy leads composition and claude reviews); a named engine
    is used alone; [] when nothing usable is installed."""
    engine = (LLM_ENGINE or "auto").lower()
    have = [e for e in ("agy", "claude") if shutil.which(e)]
    if engine in ("auto", "tandem"):
        return have
    return [engine] if engine in have else []


def get_available_llm_engine():
    """Resolve LLM_ENGINE against what's installed: 'agy', 'claude', 'tandem' (both), or None.
    'auto' runs tandem when both CLIs are present, else whichever one exists."""
    engines = available_engines()
    if not engines:
        return None
    return engines[0] if len(engines) == 1 else "tandem"


ENGINE_NAMES = ("claude", "agy")


def load_tokens():
    """Lifetime token tally across every session: input/output/cache tokens, calls, and USD cost,
    plus the same per engine under `engines` (claude has a USD cost; agy reports tokens only).
    Also carries `history`: recent [epoch, cost, engine, tokens] entries for the rolling week
    (older files hold [epoch, cost] pairs; both shapes are read)."""
    try:
        with open(TOKENS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        d = {}
    for k in _TOK_KEYS:
        d[k] = int(d.get(k, 0) or 0)
    d["cost"] = float(d.get("cost", 0.0) or 0.0)
    d.setdefault("history", [])
    engines = d.get("engines") if isinstance(d.get("engines"), dict) else {}
    for name in ENGINE_NAMES:
        e = engines.get(name) if isinstance(engines.get(name), dict) else {}
        engines[name] = {k: int(e.get(k, 0) or 0) for k in _TOK_KEYS}
        engines[name]["cost"] = float(e.get("cost", 0.0) or 0.0)
        engines[name]["total"] = sum(engines[name][k] for k in _TOK_KEYS if k != "calls")
    d["engines"] = engines
    d["total"] = d["input"] + d["output"] + d["cache_read"] + d["cache_creation"]
    return d


def _hist_entry(e):
    """(epoch, cost, engine, tokens) from either history shape."""
    try:
        t, c = float(e[0]), float(e[1] or 0.0)
    except (TypeError, ValueError, IndexError):
        return None
    engine = e[2] if len(e) > 2 and e[2] else "claude"
    tokens = int(e[3] or 0) if len(e) > 3 else 0
    return t, c, engine, tokens


def weekly_cost(d=None):
    """USD spent in the trailing 7 days (rolling), from the per-call history. Only claude calls
    carry a dollar figure; agy calls count in weekly_usage() by tokens."""
    d = d or load_tokens()
    now = time.time()
    total = 0.0
    for e in d.get("history", []):
        h = _hist_entry(e)
        if h and now - h[0] <= _WEEK:
            total += h[1]
    return total


def weekly_usage(d=None):
    """Per-engine calls / tokens / USD in the trailing 7 days: {engine: {calls, tokens, cost}}."""
    d = d or load_tokens()
    now = time.time()
    out = {name: {"calls": 0, "tokens": 0, "cost": 0.0} for name in ENGINE_NAMES}
    for e in d.get("history", []):
        h = _hist_entry(e)
        if not h or now - h[0] > _WEEK:
            continue
        slot = out.setdefault(h[2], {"calls": 0, "tokens": 0, "cost": 0.0})
        slot["calls"] += 1
        slot["tokens"] += h[3]
        slot["cost"] += h[1]
    return out


def plan_estimates(d=None):
    """(weekly_usd, {plan: percent-of-weekly-cap}) — the FALLBACK read when the live plan usage
    (plan_usage) is unavailable, e.g. API-key users with no Claude Code login."""
    wk = weekly_cost(d)
    return wk, {name: (wk / cap * 100.0 if cap else 0.0) for name, cap in PLAN_WEEKLY_USD.items()}


def _record_tokens(usage, cost, engine="claude"):
    if not usage:
        return
    engine = engine if engine in ENGINE_NAMES else "claude"
    with _TOK_LOCK:
        d = load_tokens()
        in_tok = usage.get("input_tokens", 0) or usage.get("input", 0) or 0
        out_tok = usage.get("output_tokens", 0) or usage.get("output", 0) or 0
        cache_r = (usage.get("cache_read_tokens", 0) or usage.get("cache_read_input_tokens", 0)
                   or usage.get("cache_read", 0) or 0)
        cache_c = (usage.get("cache_creation_tokens", 0) or usage.get("cache_creation_input_tokens", 0)
                   or usage.get("cache_creation", 0) or 0)
        add = {"input": int(in_tok), "output": int(out_tok), "cache_read": int(cache_r),
               "cache_creation": int(cache_c), "calls": 1}
        for k, v in add.items():
            d[k] += v
            d["engines"][engine][k] += v
        d["cost"] += float(cost or 0.0)
        d["engines"][engine]["cost"] += float(cost or 0.0)
        now = time.time()
        hist = d.get("history", [])
        tokens = add["input"] + add["output"] + add["cache_read"] + add["cache_creation"]
        hist.append([int(now), round(float(cost or 0.0), 6), engine, tokens])
        d["history"] = [e for e in hist if _hist_entry(e) and now - _hist_entry(e)[0] <= _WEEK]
        d.pop("total", None)
        for e in d["engines"].values():
            e.pop("total", None)
        try:
            os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
            tmp = TOKENS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            os.replace(tmp, TOKENS_FILE)
        except OSError:
            pass


# ---- live plan usage: the real 5-hour / 7-day utilization Claude Code itself shows in /usage ----
# Read from Anthropic's OAuth usage endpoint with the Claude Code login already on this machine.
# The token never leaves the machine except to api.anthropic.com, exactly as Claude Code uses it,
# and it is never logged or written anywhere by ClAudit. API-key-only setups get None (fallback
# to the dollar estimate above).
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_CACHE = os.path.expanduser("~/.claude/claudit/usage.json")
USAGE_TTL = 300                     # seconds between live fetches (the GUI polls the cache)
CREDENTIALS_FILE = os.path.expanduser("~/.claude/.credentials.json")


def _oauth_credentials():
    """Claude Code's OAuth session: $CLAUDE_CODE_OAUTH_TOKEN, else ~/.claude/.credentials.json
    (Linux / Windows), else the macOS keychain item Claude Code writes. {} when signed out."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    if tok:
        return {"accessToken": tok, "subscriptionType": ""}
    try:
        with open(CREDENTIALS_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("claudeAiOauth"), dict):
            return d["claudeAiOauth"]
    except (OSError, ValueError):
        pass
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials",
                                "-w"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                d = json.loads(r.stdout)
                if isinstance(d, dict) and isinstance(d.get("claudeAiOauth"), dict):
                    return d["claudeAiOauth"]
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return {}


def _parse_usage(data, plan=""):
    """Normalize the endpoint payload to what the meter shows: the 5-hour and 7-day windows plus
    any model-scoped weekly limits (e.g. a per-model cap that is the active constraint)."""
    def window(key):
        w = data.get(key) if isinstance(data.get(key), dict) else {}
        return {"pct": float(w.get("utilization") or 0.0), "resets_at": w.get("resets_at") or ""}
    out = {"plan": plan or "", "five_hour": window("five_hour"), "seven_day": window("seven_day"),
           "scoped": []}
    for lim in data.get("limits") or []:
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        model = ((lim.get("scope") or {}).get("model") or {})
        out["scoped"].append({"name": model.get("display_name") or model.get("id") or "model",
                              "pct": float(lim.get("percent") or 0.0),
                              "resets_at": lim.get("resets_at") or "",
                              "active": bool(lim.get("is_active"))})
    extra = data.get("extra_usage") if isinstance(data.get("extra_usage"), dict) else {}
    out["extra_usage"] = bool(extra.get("is_enabled"))
    return out


def usage_peak(u):
    """The highest utilization across every window (what the fill/color should react to)."""
    if not u:
        return 0.0
    return max([u.get("five_hour", {}).get("pct", 0.0), u.get("seven_day", {}).get("pct", 0.0)]
               + [s.get("pct", 0.0) for s in u.get("scoped", [])])


def plan_usage(max_age=USAGE_TTL, fetch=True):
    """The real plan utilization, cached on disk for `max_age` seconds. Returns the parsed dict
    (with `fetched` epoch) or None when there is no Claude Code login. A failed fetch keeps the
    previous snapshot (its `fetched` age tells the UI it is stale) rather than blanking the meter.
    fetch=False only reads the cache (safe on a UI thread)."""
    now = time.time()
    cached = None
    try:
        with open(USAGE_CACHE, encoding="utf-8") as fh:
            cached = json.load(fh)
        if not isinstance(cached, dict) or "seven_day" not in cached:
            cached = None
    except (OSError, ValueError):
        cached = None
    if cached and now - float(cached.get("fetched", 0) or 0) < max_age:
        return cached
    if not fetch:
        return cached
    cred = _oauth_credentials()
    tok = cred.get("accessToken")
    if not tok:
        return cached
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "User-Agent": "ClAudit"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"claudit: plan usage fetch failed: {e}", file=sys.stderr)
        return cached
    if not isinstance(data, dict):
        return cached
    out = _parse_usage(data, cred.get("subscriptionType") or "")
    out["fetched"] = int(now)
    try:
        os.makedirs(os.path.dirname(USAGE_CACHE), exist_ok=True)
        tmp = USAGE_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(out, fh)
        os.replace(tmp, USAGE_CACHE)
    except OSError:
        pass
    return out


def fmt_reset(iso, now=None):
    """'in 2h 05m' / 'in 3d 4h' from an ISO reset timestamp; '' when unknown."""
    if not iso:
        return ""
    try:
        t = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
        import calendar
        secs = calendar.timegm(t) - (now or time.time())
    except ValueError:
        return ""
    if secs <= 0:
        return "resetting"
    d, rem = divmod(int(secs), 86400)
    h, m = divmod(rem, 3600)
    m //= 60
    return f"in {d}d {h}h" if d else f"in {h}h {m:02d}m"


def usage_summary(u=None, t=None):
    """Plain-text usage report for the CLI (--usage) and the GUI tooltip."""
    u = plan_usage(fetch=False) if u is None else u
    t = t or load_tokens()
    wk = weekly_usage(t)
    L = []
    if u:
        plan = u.get("plan") or "Claude"
        L.append(f"Claude plan usage ({plan}), live from Anthropic:")
        L.append(f"  5-hour window   {u['five_hour']['pct']:5.1f}%   resets {fmt_reset(u['five_hour']['resets_at'])}")
        L.append(f"  7-day window    {u['seven_day']['pct']:5.1f}%   resets {fmt_reset(u['seven_day']['resets_at'])}")
        for s in u.get("scoped", []):
            L.append(f"  7-day {s['name']:<9} {s['pct']:5.1f}%   resets {fmt_reset(s['resets_at'])}"
                     + ("   (active limit)" if s.get("active") else ""))
        age = time.time() - float(u.get("fetched", 0) or 0)
        L.append(f"  fetched {int(age // 60)} min ago" + ("  (stale)" if age > 3 * USAGE_TTL else ""))
    else:
        wkc, plans = plan_estimates(t)
        L.append("Live plan usage unavailable (no Claude Code login found; run `claude` and sign in).")
        L.append(f"Estimated from ClAudit's own claude spend, ${wkc:.2f} this week:")
        for n, pct in plans.items():
            L.append(f"  {n:<7} {pct:5.1f}%   (est. cap ${PLAN_WEEKLY_USD[n]:.0f}/wk)")
    L.append("")
    L.append("ClAudit's own calls, trailing 7 days:")
    L.append(f"  claude  {wk['claude']['calls']:>5} calls  {_fmt_tokens(wk['claude']['tokens']):>8} tokens  ${wk['claude']['cost']:.2f}")
    L.append(f"  agy     {wk['agy']['calls']:>5} calls  {_fmt_tokens(wk['agy']['tokens']):>8} tokens")
    L.append("Lifetime across every session:")
    for name in ENGINE_NAMES:
        e = t["engines"][name]
        L.append(f"  {name:<7} {e['calls']:>5} calls  {_fmt_tokens(e['total']):>8} tokens"
                 + (f"  ${e['cost']:.2f}" if name == "claude" else ""))
    legacy = t["total"] - sum(t["engines"][n]["total"] for n in ENGINE_NAMES)
    if legacy > 0:
        L.append(f"  (+{_fmt_tokens(legacy)} tokens recorded before per-engine tallies existed)")
    return "\n".join(L)


def _fmt_tokens(n):
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(int(n))


def _claude(prompt, timeout, model=None):
    """Run the `claude` CLI in JSON mode, tally token usage into the lifetime meter, and return the
    model's text. Falls back gracefully (returns '' on error; raw stdout on an older non-JSON CLI)."""
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if model or LLM_MODEL:
        cmd += ["--model", model or LLM_MODEL]
    if LLM_EFFORT:
        cmd += ["--effort", LLM_EFFORT]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as e:                      # degrade gracefully, but never silently
        print(f"claudit: claude CLI call failed: {type(e).__name__}: {e}", file=sys.stderr)
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
        _record_tokens(res.get("usage"), res.get("total_cost_usd"), engine="claude")
        return (res.get("result") or "").strip()
    return ""


def _agy(prompt, timeout, model=None):
    """Run the `agy` CLI in non-interactive print mode with JSON output, tally token usage into the
    lifetime meter, and return the model's text. Falls back gracefully (returns '' on error)."""
    cmd = ["agy", "-p", prompt, "--output-format", "json"]
    if AGY_PROJECT:
        cmd += ["--project", AGY_PROJECT]
    if model:
        cmd += ["--model", model]
    elif LLM_MODEL and not LLM_MODEL.startswith("claude-"):
        cmd += ["--model", LLM_MODEL]
    if LLM_EFFORT:
        cmd += ["--effort", LLM_EFFORT]
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception as e:
        print(f"claudit: agy CLI call failed: {type(e).__name__}: {e}", file=sys.stderr)
        return ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict):
        if data.get("usage"):
            _record_tokens(data.get("usage"), cost=None, engine="agy")
        return (data.get("response") or "").strip()
    return ""


def engine_slots():
    """The distinct LLM 'voices' this run uses, as [(engine, model_override), ...]. Tandem gives
    agy + claude. In agy-ONLY mode with AGY_REVIEW_MODEL set, the second voice is agy on that
    different model — cross-checking survives without spending any claude quota."""
    engines = available_engines()
    slots = [(e, None) for e in engines]
    if engines == ["agy"] and AGY_REVIEW_MODEL:
        slots.append(("agy", AGY_REVIEW_MODEL))
    return slots


def _run_llm(prompt, timeout=120, engine=None, model=None):
    """Run one LLM prompt through `engine` ('agy' or 'claude'), or the primary available one,
    optionally forcing a specific model."""
    engines = available_engines()
    engine = engine or (engines[0] if engines else None)
    if engine == "agy":
        return _agy(prompt, timeout, model) if model else _agy(prompt, timeout)
    if engine == "claude":
        return _claude(prompt, timeout, model) if model else _claude(prompt, timeout)
    return ""


# Condensed no-ai-slop rules (the full skill lives in ~/.claude/skills/no-ai-slop; both engines
# know it by name). Included in every compose prompt so defense comments and report prose read
# as written, not generated.
STYLE_RULES = (
    "STYLE (no-ai-slop): no em-dashes. No intensifiers (significantly, dramatically, extremely). "
    "No filler openers (it's important to note, in today's world). No hollow claims; end each "
    "sentence on a checkable fact. Plain register, not adversarial. US English. No greeting, "
    "no sign-off, no marketing tone.")


def _redact_terms(text, terms):
    """Deterministically redact `terms` out of `text`, protecting Request IDs and tool/vendor
    names the reports depend on. Shared by llm_redact and the tandem compose review."""
    protect = re.compile(r"^(req_[A-Za-z0-9]+|claudit|claude|anthropic|github|sworrl)$", re.IGNORECASE)
    for t in sorted({str(x).strip() for x in terms if isinstance(x, str)}, key=len, reverse=True):
        if len(t) < 2 or protect.match(t) or t.lower().startswith("req_"):
            continue
        text = re.sub(_deny_regex(t), "[REDACTED]", text, flags=re.IGNORECASE)
    return text


def _tandem_review(draft, timeout=90):
    """Tandem mode: the SECOND engine reviews the first engine's draft before it can be posted.
    Returns (ok, pii_terms): pii_terms are exact substrings to redact; ok=False means the draft
    reads as AI slop and the caller should fall back to its deterministic template. Single-voice
    runs and reviewer failures return (True, []) so tandem never blocks what one voice allowed."""
    slots = engine_slots()
    if len(slots) < 2 or not (draft or "").strip():
        return True, []
    prompt = (
        "Review the DRAFT below, written for a public GitHub issue. Respond with ONLY JSON: "
        '{"pii": [...], "slop": true/false}. '
        "\"pii\": the EXACT substrings that identify someone or something private (person names, "
        "company/org/client names and abbreviations, tenant/domain names, internal hostnames, "
        "project codenames, emails, IPs, secrets). Request IDs (req_...) and the words Claude, "
        "Anthropic, ClAudit, GitHub are NOT PII. "
        "\"slop\": true only if the draft reads as AI-generated per the no-ai-slop rules "
        "(em-dashes, intensifiers, filler openers, hollow claims, brochure tone).\n\n"
        "DRAFT:\n" + draft[:6000])
    try:
        out = _run_llm(prompt, timeout=timeout, engine=slots[1][0], model=slots[1][1])
        m = re.search(r"\{.*\}", out, re.DOTALL)
        d = json.loads(m.group(0)) if m else {}
    except Exception:
        return True, []
    if not isinstance(d, dict):
        return True, []
    pii = [t for t in (d.get("pii") or []) if isinstance(t, str)]
    return not bool(d.get("slop")), pii


def llm_compose(instruction, context, max_chars=3000):
    """Burn-tokens mode: have the primary LLM CLI (agy or claude) write bespoke, well-crafted text
    (a title, a summary, a comment). In tandem mode the second engine then reviews the draft: PII
    it finds is redacted deterministically, and a draft it judges AI-slop is rejected (returns
    None, so callers fall back to their deterministic templates). PII-free by instruction; the
    caller still runs scrub() as a safety net. Returns None when burn-tokens is off / no LLM is
    available / on any error."""
    if not BURN_TOKENS or not available_engines() or not instruction:
        return None
    prompt = (instruction + "\n\n" + STYLE_RULES +
              "\n\nHARD RULE: do not include any names, organizations, hostnames, IPs, "
              "emails, tenant names, file paths, or other identifying details — describe the work "
              "generically. Output only the requested text, nothing else.\n\nCONTEXT:\n" + (context or "")[:max_chars])
    out = _run_llm(prompt, timeout=120)
    if not out:
        return None
    ok, pii = _tandem_review(out)
    if pii:
        out = _redact_terms(out, pii)
    return out if ok else None

# (label, compiled pattern, replacement). Order matters: secrets/specific first.
SCRUBBERS = [
    ("anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED_ANTHROPIC_KEY]"),
    ("openai key",    re.compile(r"sk-[A-Za-z0-9]{20,}"),        "[REDACTED_API_KEY]"),
    ("github token",  re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),  "[REDACTED_GH_TOKEN]"),
    ("aws key",       re.compile(r"AKIA[0-9A-Z]{16}"),           "[REDACTED_AWS_KEY]"),
    ("gitlab token",  re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),  "[REDACTED_GL_TOKEN]"),
    ("slack token",   re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    ("npm token",     re.compile(r"npm_[A-Za-z0-9]{36}"),        "[REDACTED_NPM_TOKEN]"),
    ("google key",    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),     "[REDACTED_GOOGLE_KEY]"),
    ("stripe key",    re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}"), "[REDACTED_STRIPE_KEY]"),
    ("ssh pubkey",    re.compile(r"ssh-(?:rsa|ed25519|dss) [A-Za-z0-9+/=]{40,}(?: \S+)?"), "[REDACTED_SSH_PUBKEY]"),
    ("bearer token",  re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer [REDACTED_TOKEN]"),
    ("jwt",           re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED_JWT]"),
    ("private key",   re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
    ("db creds",      re.compile(r"\b(postgres|postgresql|mysql|mongodb|redis|amqp)://[^\s\"'@/]+:[^\s\"'@/]+@[^\s\"']+"), r"\1://[REDACTED_DB_CREDS]"),
    # user:pass@ in any other URL (http basic auth, git remotes)
    ("url creds",     re.compile(r"\b(https?|git|ftp)://[^\s\"'@/:]+:[^\s\"'@/]+@"), r"\1://[REDACTED_CREDS]@"),
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
    # IPv6: full 8-group form, or compressed (::) with a 2+ char lead group and a non-empty tail —
    # the lead/tail requirements keep hh:mm:ss timestamps and C++ `A::foo` scope refs unmatched.
    ("ipv6",          re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}\b"
                                 r"|\b(?:[0-9a-fA-F]{2,4}:){1,6}:(?:[0-9a-fA-F]{1,4}(?::[0-9a-fA-F]{1,4}){0,5})\b"), "[IPV6]"),
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


def _identify_pii_terms(text, engine, model=None):
    """One voice's PII pass: returns the list of exact substrings it flags. Raises on failure so
    llm_redact can tell 'voice found nothing' from 'voice broke'."""
    prompt = (
        "You are a strict PII redactor. From the TEXT below, return ONLY a JSON array of the EXACT "
        "substrings that are identifying: real people's names, initials that stand for a name, "
        "company/org/client names AND their abbreviations, tenant/domain names, internal hostnames, "
        "project codenames, emails, IPs, secrets. "
        "Do NOT include: Request IDs (anything starting with 'req_'), or the words Claude, Anthropic, "
        "ClAudit, GitHub — those must stay. No commentary, just the JSON array.\n\nTEXT:\n" + text[:8000])
    out = _run_llm(prompt, timeout=90, engine=engine, model=model)
    m = re.search(r"\[.*\]", out, re.DOTALL)
    return json.loads(m.group(0)) if m else []


def llm_redact(text: str) -> str:
    """Opt-in: ask the LLM CLIs to find PII the regex can't (names, org abbreviations, hostnames,
    codenames). In tandem mode EVERY installed engine runs the pass and their findings are
    UNIONED — either engine flagging a term redacts it, so a name one model misses still gets
    caught by the other. The models only IDENTIFY terms; redaction is applied here
    deterministically so they can't rewrite your content. Falls back to unchanged only when
    every engine fails."""
    if not LLM_SCRUB or not available_engines() or not text.strip():
        return text
    terms, any_ok = set(), False
    for engine, model in engine_slots():
        try:
            found = _identify_pii_terms(text, engine, model)
        except Exception:
            continue
        any_ok = True
        terms.update(str(x).strip() for x in found if isinstance(x, str))
    if not any_ok:
        return text
    # Hard guard in _redact_terms: never redact Request IDs or the tool/vendor names.
    return _redact_terms(text, terms)


def llm_is_false_positive(kind, block_text, context=""):
    """Judge whether a block is a GENUINE false positive (it stopped legitimate, in-scope work) or a
    CORRECT block (the action really was risky/disallowed — mass external posting, credential theft,
    scraping personal data, deploying malware, evading controls). Returns (is_false_positive, reason).
    Defaults to (True, '') when no LLM is available, to preserve prior behavior. In tandem mode
    every installed engine is asked and ANY engine's clear 'correct block' verdict vetoes the
    filing — two independent models agreeing a report is legitimate beats one."""
    if not (LLM_SCRUB or BURN_TOKENS) or not available_engines():
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
    reason = ""
    for engine, model in engine_slots():
        try:
            out = _run_llm(prompt, timeout=90, engine=engine, model=model)
            m = re.search(r"\{.*\}", out, re.DOTALL)
            d = json.loads(m.group(0)) if m else {}
        except Exception:
            continue                       # a broken voice never vetoes; the other still votes
        if not bool(d.get("false_positive", True)):
            return False, str(d.get("reason", ""))
        reason = reason or str(d.get("reason", ""))
    return True, reason


# Frustration expletives (often aimed at the assistant) sometimes TRIGGER the safety block itself.
# They must never appear verbatim in a public bug report or a GUI snippet — mask to first letter.
PROFANITY = re.compile(
    r"\b(f+u+c+k\w*|s+h+i+t\w*|bullshit\w*|c+u+n+t\w*|b+i+t+c+h\w*|a+s+s+h+o+l+e\w*|"
    r"motherfuck\w*|goddamn\w*|dickhead\w*|wtf|stfu)\b", re.IGNORECASE)


def scrub(text: str):
    counts = {}
    for label, pattern, repl in SCRUBBERS:
        text, n = pattern.subn(repl, text)
        if n:
            counts[label] = counts.get(label, 0) + n
    text, n = PROFANITY.subn(lambda m: m.group(0)[0] + "•••", text)
    if n:
        counts["profanity"] = counts.get("profanity", 0) + n
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
    p.add_argument("--engine", choices=["auto", "claude", "agy", "tandem"], default=None,
                   help="LLM engine: auto, claude, agy, or tandem (both, cross-checking each other)")
    args = p.parse_args()
    if args.engine:
        globals()["LLM_ENGINE"] = args.engine

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
