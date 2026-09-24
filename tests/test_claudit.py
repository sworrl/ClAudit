"""Core tests for ClAudit — no network, no real gh/claude (all mocked/off)."""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claudit
import claudit_scan as cs


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep every test off the real ~/.claude state and off the network."""
    monkeypatch.setattr(cs, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(cs, "STATE_FILE", str(tmp_path / "filed.json"))
    monkeypatch.setattr(cs, "ISSUES_DB", str(tmp_path / "issues.jsonl"))
    monkeypatch.setattr(cs, "ERROR_LOG", str(tmp_path / "err.jsonl"))
    monkeypatch.setattr(claudit, "LLM_SCRUB", False)
    monkeypatch.setattr(claudit, "BURN_TOKENS", False)
    monkeypatch.setattr(claudit, "_EXTRA", [])   # empty denylist by default
    yield


# ---------------- PII scrubbing ----------------
def test_token_meter_accumulates(tmp_path, monkeypatch):
    monkeypatch.setattr(claudit, "TOKENS_FILE", str(tmp_path / "tokens.json"))
    assert claudit.load_tokens()["total"] == 0
    claudit._record_tokens(
        {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 17000}, 0.09)
    claudit._record_tokens(
        {"input_tokens": 200, "output_tokens": 25, "cache_read_input_tokens": 5000}, 0.03)
    t = claudit.load_tokens()
    assert (t["input"], t["output"], t["cache_read"], t["calls"]) == (300, 75, 22000, 2)
    assert abs(t["cost"] - 0.12) < 1e-9
    assert t["total"] == 300 + 75 + 22000          # cumulative across "sessions"


def test_weekly_cost_and_plan_estimates(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(claudit, "TOKENS_FILE", str(tmp_path / "tokens.json"))
    monkeypatch.setattr(claudit, "PLAN_WEEKLY_USD", {"Pro": 30.0, "Max 5x": 150.0, "Max 20x": 600.0})
    now = time.time()
    # two calls inside the rolling week ($3 + $1.50), one 10 days ago that must be excluded
    d = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "calls": 3, "cost": 9.5,
         "history": [[int(now - 3600), 3.0], [int(now - 2 * 86400), 1.5], [int(now - 10 * 86400), 5.0]]}
    (tmp_path / "tokens.json").write_text(json.dumps(d))
    assert abs(claudit.weekly_cost() - 4.5) < 1e-9              # only the two recent ones
    wk, plans = claudit.plan_estimates()
    assert abs(wk - 4.5) < 1e-9
    assert abs(plans["Pro"] - 15.0) < 1e-6                       # 4.5 / 30
    assert abs(plans["Max 5x"] - 3.0) < 1e-6                     # 4.5 / 150
    assert abs(plans["Max 20x"] - 0.75) < 1e-6                   # 4.5 / 600


def test_record_tokens_prunes_history_to_week(tmp_path, monkeypatch):
    import time
    monkeypatch.setattr(claudit, "TOKENS_FILE", str(tmp_path / "tokens.json"))
    old = [[int(time.time() - 9 * 86400), 2.0]]
    (tmp_path / "tokens.json").write_text(json.dumps(
        {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "calls": 1,
         "cost": 2.0, "history": old}))
    claudit._record_tokens({"input_tokens": 10, "output_tokens": 5}, 0.4)
    with open(claudit.TOKENS_FILE) as fh:
        hist = json.load(fh)["history"]
    assert hist == [hist[0]] and len(hist) == 1                  # stale entry pruned, new one kept
    assert abs(claudit.weekly_cost() - 0.4) < 1e-9


def test_scrub_core_pii():
    s = ("email a@b.com ip 10.0.0.5 key sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA "
         "path /home/bob/x req_011CcABC")
    out, _ = claudit.scrub(s)
    assert "a@b.com" not in out and "[EMAIL]" in out
    assert "10.0.0.5" not in out and "[IP]" in out
    assert "sk-ant-" not in out
    assert "/home/bob" not in out and "/home/[USER]" in out
    assert "req_011CcABC" in out          # Request IDs must be preserved


def test_scrub_encoded_home_path():
    out, _ = claudit.scrub("/tmp/claude-1000/-var-home-bob-Documents-GitHub-x/tasks/y.output")
    assert "-var-home-bob" not in out and "[USER]" in out


def test_scrub_denylist_word_boundary(monkeypatch):
    monkeypatch.setattr(claudit, "_EXTRA", ["Acme"])
    out, _ = claudit.scrub("Acme ships Markdown daily")
    assert "Acme " not in out and "[REDACTED]" in out
    assert "Markdown" in out               # must NOT over-redact substrings


def test_scrub_denylist_underscore_glued(monkeypatch):
    # a denylisted name glued to '_' / '-' / a digit must still be caught — \b misses it because
    # '_' is a word char (this is how a denylisted name once leaked inside an env-var like NAME_DRY_RUN).
    monkeypatch.setattr(claudit, "_EXTRA", ["Acme"])
    out, _ = claudit.scrub("Starting ACME_DRY_RUN=0 and acme-bot and Acme2 today")
    assert "ACME" not in out.upper().replace("REDACTED", "")   # every occurrence redacted
    assert "[REDACTED]_DRY_RUN" in out
    # letter-substrings still safe: 'Acmegate' (letter suffix) and 'Markdown' must be untouched
    out2, _ = claudit.scrub("the Acmegate saga and Markdown")
    assert "Acmegate" in out2 and "Markdown" in out2


def test_scrub_denylist_never_corrupts_request_id(monkeypatch):
    # a short denylist term ('NV') can fall between digits inside a Request ID; the denylist pass
    # must NOT redact it there, or the report loses the ID Anthropic needs to look up.
    monkeypatch.setattr(claudit, "_EXTRA", ["NV"])
    out, _ = claudit.scrub("see req_011CcPL8dVfbJY8NV6drDsEj for details")
    assert "req_011CcPL8dVfbJY8NV6drDsEj" in out      # Request ID intact, NV preserved inside it
    # a standalone 'NV' outside an ID is still redacted
    out2, _ = claudit.scrub("hosted by NV corp")
    assert "NV corp" not in out2 and "[REDACTED]" in out2


def test_profanity_masked_in_public_text():
    out, counts = claudit.scrub("I said FUCK YOU! and also wtf, this is bullshit-adjacent shit")
    assert "FUCK" not in out and "F•••" in out
    assert "wtf" not in out and "w•••" in out
    assert out.count("•••") >= 3
    assert counts.get("profanity", 0) >= 3


def test_flag_model_extraction():
    assert cs.flag_model("API Error: Fable 5's safeguards flagged this message "
                         "(https://www.anthropic.com/legal/aup).") == "Fable 5"
    assert cs.flag_model("API Error: Opus 4.8 (1M context)'s safeguards flagged this message "
                         "for a cybersecurity topic.") == "Opus 4.8 (1M context)"
    assert cs.flag_model("Sonnet 5’s safeguards flagged this message") == "Sonnet 5"
    assert cs.flag_model("no model named here") == ""


# ---------------- classification ----------------
@pytest.mark.parametrize("text,kind", [
    ("safety measures that flagged this message for a cybersecurity topic", "cyber"),
    ("flagged this message as a cybersecurity", "cyber"),          # PR #5
    ("safety filter detected cybersecurity", "cyber"),             # PR #5
    ("API Error: Opus 4.8's safeguards flagged this message for a cybersecurity topic", "cyber"),  # reworded
    ("Sonnet's safeguards flagged this message for a cybersecurity topic", "cyber"),               # reworded
    ("this request was blocked due to cybersecurity policy", "cyber"),       # new
    ("message classified as a cybersecurity risk", "cyber"),                 # new
    ("security topic classifier blocked this request", "cyber"),             # new
    ("cybersecurity block: request cannot be completed", "cyber"),           # new
    ("appears to violate our Usage Policy", "aup"),
    ("Claude Code is unable to respond to this request", "aup"),
    ("blocked: against our usage policy", "aup"),                  # PR #5
    ("usage policy violation", "aup"),                             # PR #5
    ("content policy violation", "aup"),                           # PR #5
    ("this violates our acceptable use policy", "aup"),                      # new
    ("terms of service violation detected", "aup"),                          # new
    ("this request violates our guidelines", "aup"),                         # new
    ("action prohibited by our policies", "aup"),                            # new
    ("API Error: 529 Overloaded", "overloaded"),
    ("You've hit your limit", "limit"),
    ("just normal text", "other"),
    # 2026-07 Fable 5 rewordings — these matched NOTHING and silently dropped to 'other'
    ("API Error: Fable 5's safeguards flagged this message (https://www.anthropic.com/legal/aup). "
     "This sometimes happens with safe, normal conversations. Claude Code can't respond to this "
     "request with Fable 5.", "aup"),
    ("API Error: Fable 5's safeguards flagged this message (https://www.anthropic.com/legal/aup). "
     "They may flag safe, normal content as well.", "aup"),
    ("API Error: Fable 5's safeguards flagged this message for a cybersecurity topic. If your work "
     "requires this access, you can apply for an exemption: "
     "https://claude.com/form/cyber-use-case?token=xyz", "cyber"),
    # future-proofing: an unseen rewording that keeps 'safeguards flagged' still files as aup
    ("API Error: The model's safeguards flagged this message. Try rephrasing.", "aup"),
])
def test_classify(text, kind):
    assert cs.classify(text) == kind


@pytest.mark.parametrize("text", [
    "I'm not able to assist with that",
    "I cannot help with that request",
    "that would be inappropriate content",
    "I won't produce harmful content",
])
def test_legit_refusals_are_not_reportable(text):
    # ordinary model refusals are NOT server-side policy blocks — they must stay 'other'
    # (logged, never filed), the same bucket as overloaded/rate-limit. Guards PR #5 scope.
    assert cs.classify(text) == "other"


@pytest.mark.parametrize("text,refusal", [
    ("I can't write that report. The block is not a false positive.", True),
    ("This is a true positive; the policy block is accurate.", True),
    ("I won't do that — it would be dishonest.", True),
    ("Legitimate in-scope audit of my own host was wrongly blocked.", False),
])
def test_refusal_guard(text, refusal):
    # the burn-tokens composer must never post the model's refusal/editorial into a report
    assert cs._is_refusal(text) is refusal


def test_should_file_requires_cyber_aup_with_request_id():
    assert cs.should_file(_finding(kind="cyber", req="req_011CcABC")) is True
    assert cs.should_file(_finding(kind="aup", req="req_011CcXYZ")) is True
    assert cs.should_file(_finding(kind="cyber", req=None)) is False     # no Request ID to reference
    assert cs.should_file(_finding(kind="harness", req="req_011CcABC")) is False  # harness is log-only


def test_harness_denial_detection():
    denied = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "Permission for this action was denied by the "
                                            "Claude Code auto mode classifier. Reason: x"}]}}
    assert cs.harness_denial(denied)
    fine = {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}
    assert cs.harness_denial(fine) is None


# ---------------- dedup signature ----------------
def test_sig_is_stable_and_distinct():
    a = cs.sig("cyber", "do the thing")
    assert a == cs.sig("cyber", "do the thing")
    assert a != cs.sig("cyber", "do another thing")
    assert a != cs.sig("aup", "do the thing")


# ---------------- issue building ----------------
def _finding(kind="cyber", req="req_011CcABC"):
    block = "cybersecurity topic" + (f" Request ID: {req}" if req else "")
    return {"sig": "s1", "kind": kind, "prompt": "scan my host 10.0.0.5",
            "occ": [{"req": req, "ts": "2026-06-25T00:00:00Z", "session": "s",
                     "proj": "-h-u-Documents-GitHub-x"}],
            "block_text": block,
            "leadup": [("user", "secret stuff 10.0.0.5")]}


def test_build_issue_title_pii_and_no_leadup():
    title, body = cs.build_issue(_finding(), "")
    assert title.startswith("[Bug][cyber]")
    assert "req_011CcABC" in title                  # Request ID survives in title
    assert "Request IDs" in body and "req_011CcABC" in body
    assert "Conversation leadup" not in body        # leadup never goes to the public post
    assert "Working context" not in body
    assert "10.0.0.5" not in body                   # PII scrubbed in body


# ---------------- filing / dedup ----------------
def test_file_one_files_once(monkeypatch):
    posted = []
    monkeypatch.setattr(cs, "gh_create",
                        lambda r, t, b: posted.append(t) or f"https://github.com/{r}/issues/{len(posted)}")
    monkeypatch.setattr(cs, "gh_comment", lambda *a: None)
    state = {}
    first = cs.file_one(_finding(), "", "o/r", state)
    second = cs.file_one(_finding(), "", "o/r", state)
    assert first[0] == "new"
    assert second[0] is None                        # same finding -> not filed again
    assert len(posted) == 1


# ---------------- the honesty gate ----------------
def _multi(sig, kind, reqs, proj="-h-u-Documents-GitHub-x"):
    return {"sig": sig, "kind": kind, "prompt": "scan my host",
            "occ": [{"req": r, "ts": "2026-06-27T00:00:00Z", "session": "s", "proj": proj} for r in reqs],
            "block_text": f"{kind} block", "leadup": [("user", "in-scope work")]}


def test_dwell_files_one_linked_issue_per_request_id(monkeypatch):
    # the bespoke model: each Request ID becomes its OWN issue, cross-linked into a string.
    findings = [({}, {})]
    monkeypatch.setattr(cs, "scan", lambda ttl=0: findings[0])
    posted, comments = [], []
    monkeypatch.setattr(cs, "gh_create",
                        lambda r, t, b: posted.append((t, b)) or f"https://github.com/{r}/issues/{len(posted)}")
    monkeypatch.setattr(cs, "gh_comment", lambda r, n, b: comments.append((n, b)))
    monkeypatch.setattr(cs, "log_issue", lambda *a: None)
    monkeypatch.setattr(claudit, "llm_is_false_positive", lambda *a: (True, ""))
    state = {}
    assert cs.dwell_cycle(state, "o/r", 0, lambda *a: None, dwell=0) == 0   # baseline: files nothing
    findings[0] = ({"s1": _multi("s1", "cyber", ["A", "B"]), "s2": _multi("s2", "aup", ["C"])}, {})
    n = cs.dwell_cycle(state, "o/r", 0, lambda *a: None, dwell=0)
    assert n == 3                                          # 3 Request IDs -> 3 bespoke issues
    assert len(posted) == 3
    assert set(state["__filed_reqs__"]) == {"A", "B", "C"}
    assert "A" in posted[0][0] and "B" in posted[1][0]    # each title carries its own Request ID
    assert len(comments) == 2                             # 2nd and 3rd back-link the prior sibling
    assert "#2" in posted[2][1] or "#1" in posted[2][1]   # later report forward-links earlier ones


def test_dwell_skips_blocks_the_gate_rejects(monkeypatch):
    findings = [({}, {})]
    monkeypatch.setattr(cs, "scan", lambda ttl=0: findings[0])
    monkeypatch.setattr(cs, "gh_create", lambda r, t, b: "https://github.com/o/r/issues/1")
    monkeypatch.setattr(cs, "log_issue", lambda *a: None)
    monkeypatch.setattr(claudit, "llm_is_false_positive", lambda *a: (False, "correct block"))
    state = {}
    cs.dwell_cycle(state, "o/r", 0, lambda *a: None, dwell=0)
    findings[0] = ({"s1": _multi("s1", "cyber", ["A"])}, {})
    assert cs.dwell_cycle(state, "o/r", 0, lambda *a: None, dwell=0) == 0   # gate rejects -> not filed
    assert "A" in state["__skipped_reqs__"]


def test_dwell_failed_create_backs_off_no_token_reburn(monkeypatch):
    # a create that keeps failing must NOT re-judge (re-burn the LLM gate) every tick — it backs off.
    findings = [({}, {})]
    monkeypatch.setattr(cs, "scan", lambda ttl=0: findings[0])

    def boom(r, t, b):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(cs, "gh_create", boom)
    monkeypatch.setattr(cs, "log_issue", lambda *a: None)
    gate_calls = []
    monkeypatch.setattr(claudit, "llm_is_false_positive",
                        lambda *a: gate_calls.append(1) or (True, ""))
    state = {}
    cs.dwell_cycle(state, "o/r", 0, lambda *a: None, dwell=0)             # baseline
    findings[0] = ({"s1": _multi("s1", "cyber", ["A"])}, {})
    cs.dwell_cycle(state, "o/r", 0, lambda *a: None, dwell=0)             # create fails -> backoff stamp
    assert "A" in state["__dwell_fail__"]
    assert gate_calls == [1]                                             # judged once
    cs.dwell_cycle(state, "o/r", 0, lambda *a: None, dwell=0)            # within cooldown -> skipped
    cs.dwell_cycle(state, "o/r", 0, lambda *a: None, dwell=0)
    assert gate_calls == [1]                                             # NOT re-judged -> no token re-burn


def test_build_issue_has_triage_header():
    _, body = cs.build_issue(_finding(), "")
    assert body.lstrip().startswith("**Triage:**")          # structured triage line for maintainers
    assert "kind `cyber`" in body and "session-halted" in body


def test_amplify_skips_own_and_harness_and_dedups(monkeypatch):
    issues = [{"number": 1, "author": {"login": "me"}, "title": "[cyber] mine"},
              {"number": 2, "author": {"login": "other"}, "title": "[aup] theirs"},
              {"number": 3, "author": {"login": "other"}, "title": "[harness] theirs"},
              {"number": 4, "author": {"login": "other"}, "title": "[cyber] theirs"}]
    monkeypatch.setattr(cs, "_gh_json", lambda a: issues)
    reacted = []
    monkeypatch.setattr(cs.subprocess, "run",
                        lambda args, **k: reacted.append(args[2]) or type("R", (), {"returncode": 0})())
    state = {}
    n = cs.amplify_community("o/r", state, me="me")
    assert n == 2                                           # #2 and #4 only (others' cyber/aup)
    assert "repos/o/r/issues/2/reactions" in reacted and "repos/o/r/issues/4/reactions" in reacted
    assert not any("issues/1/" in u or "issues/3/" in u for u in reacted)   # never own / harness
    assert cs.amplify_community("o/r", state, me="me") == 0  # idempotent: nothing new


def test_defense_note_is_contextual(monkeypatch):
    # the 'not a duplicate' note must name the SPECIFIC issues the bot cited and rebut each as its
    # own Request ID — not a generic note.
    posted = {}

    def fake_run(args, **k):
        if "comment" in args:
            posted["body"] = args[args.index("--body") + 1]
        return type("R", (), {"returncode": 0, "stdout": "THUMBS_DOWN", "stderr": ""})()

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    ok = cs._push_not_dup("o/r", 71857, "CMT", "#71860", "reason.", "body", compose=False,
                          flag_body="Found 3 duplicates: #71860 #71858 #71861",
                          cited=["71860", "71858", "71861"])
    assert ok
    b = posted["body"]
    assert "#71860" in b and "#71858" in b and "#71861" in b   # names every cited issue
    assert "distinct" in b.lower() and "Request ID" in b


def test_cited_issues_parses_urls_and_hashes():
    # the dup-bot cites via full URLs, sometimes #NNN; both must parse, excluding the issue itself
    body = ("Found duplicates:\n1. https://github.com/anthropics/claude-code/issues/71867\n"
            "2. https://github.com/anthropics/claude-code/issues/71868\nalso #71861 and self #71866")
    assert cs._cited_issues(body, "71866") == ["71861", "71867", "71868"]


def test_scan_excludes_claudit_own_llm_prompts(tmp_path):
    # ClAudit's own compose/scrub/gate `claude -p` calls get blocked and land in transcripts; scan
    # must NOT re-report them (feedback loop). A block preceded by an internal prompt is dropped.
    own = tmp_path / "own.jsonl"
    own.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"content": "Write ONE specific GitHub issue title for this block"}},
        {"isApiErrorMessage": True, "timestamp": "2026-06-27T00:00:00Z",
         "message": {"content": "API Error: flagged this message for a cybersecurity topic. "
                                "Request ID: req_011CcOWN"}}]))
    assert cs._parse_file(str(own), "own.jsonl", str(tmp_path))[0] == []   # excluded
    # a REAL user prompt before the same block IS captured
    real = tmp_path / "real.jsonl"
    real.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"content": "audit my own firewall config"}},
        {"isApiErrorMessage": True, "timestamp": "2026-06-27T00:00:00Z",
         "message": {"content": "API Error: flagged this message for a cybersecurity topic. "
                                "Request ID: req_011CcREAL"}}]))
    findings = cs._parse_file(str(real), "real.jsonl", str(tmp_path))[0]
    assert len(findings) == 1 and findings[0]["kind"] == "cyber"


def test_gate_is_noop_without_llm():
    ok, _ = claudit.llm_is_false_positive("cyber", "some block reason", "context")
    assert ok is True                               # LLM off -> file everything (prior behavior)


def test_gate_off_by_default_files_everything(monkeypatch):
    # GATE off (default): passes_gate never pre-judges, never calls the LLM, files everything.
    monkeypatch.setattr(cs, "GATE", False)
    called = []
    monkeypatch.setattr(claudit, "llm_is_false_positive",
                        lambda *a: called.append(1) or (False, "would-skip"))
    assert cs.passes_gate(_finding(), {}) is True
    assert called == []                             # gate off -> LLM judge is never consulted


def test_block_message_is_scrubbed():
    f = _finding()
    f["block_text"] = "blocked while contacting 10.0.0.9 over ssh badhost"
    _, body = cs.build_issue(f, "")
    assert "10.0.0.9" not in body                   # block message must be PII-scrubbed too


def test_harness_block_shows_reason_only_not_command():
    f = _finding(kind="harness", req=None)
    f["occ"][0]["req"] = None
    f["block_text"] = ("Permission for this action was denied by the Claude Code auto mode classifier. "
                       "Reason: writing to a production host. If you have other tasks that don't depend "
                       "on this. scp /tmp/x HOST:/tmp/x && ssh HOST 'run it'")
    _, body = cs.build_issue(f, "")
    assert "writing to a production host" in body
    assert "scp /tmp/x" not in body                 # never echo the quoted command
    assert "If you have other tasks" not in body


def test_is_meta_reply_rejects_paste_the_comment():
    assert cs._is_meta_reply("The bot's comment appears to be missing — could you paste it?")
    assert cs._is_meta_reply("Without the bot's actual comment, I can't reference them.")
    assert not cs._is_meta_reply("Not a duplicate. #71918 is a distinct block with its own Request ID.")


def test_bot_dup_flags_ignore_human_comments():
    # a HUMAN saying "possible duplicate" is a legit response, never an auto-close attempt —
    # only bot-authored flags may trigger a defense.
    comments = [
        {"author": {"login": "some-maintainer"}, "body": "possible duplicate of #1?", "createdAt": "A"},
        {"author": {"login": "github-actions"}, "body": "Found 3 possible duplicate issues", "createdAt": "B"},
        {"author": {"login": "dependabot[bot]"}, "body": "closed as a duplicate", "createdAt": "C"},
    ]
    flags = cs._bot_dup_flags(comments)
    assert [c["createdAt"] for c in flags] == ["B", "C"]


def test_issue_check_minute_stable_and_spread():
    # static per issue (exactly-24h cadence) and inside a day
    for n in (1, 75160, 999999):
        m = cs._issue_check_minute(n)
        assert m == cs._issue_check_minute(n) and 0 <= m < 1440


def test_daily_recheck_window_selection(monkeypatch):
    # an issue is due exactly when its scheduled minute falls in (start, end]; a >24h window
    # selects everything (catch-up after throttled runs)
    nums = [11, 22, 33]
    monkeypatch.setattr(cs, "_gh_json", lambda a: [{"number": n} for n in nums])
    monkeypatch.setattr(cs, "gh_login", lambda: "me")
    checked = []
    monkeypatch.setattr(cs, "_defend_issue",
                        lambda repo, num, me, state, compose=False: checked.append(num) or "ok")
    m = cs._issue_check_minute(nums[0]) * 60        # schedule epoch second of issue 11 (day 0)
    cs.daily_recheck("o/r", {}, window_start=m - 120, window_end=m + 60)
    assert checked == [nums[0]]                     # only the issue whose minute is in-window
    checked.clear()
    cs.daily_recheck("o/r", {}, window_start=0, window_end=25 * 3600)
    assert checked == sorted(nums)                  # >24h window -> full sweep


def test_get_available_llm_engine(monkeypatch):
    monkeypatch.setattr(claudit, "LLM_ENGINE", "auto")
    monkeypatch.setattr(claudit.shutil, "which", lambda cmd: "/bin/" + cmd if cmd in ("agy", "claude") else None)
    assert claudit.get_available_llm_engine() == "tandem"    # auto + both installed = tandem
    assert claudit.available_engines() == ["agy", "claude"]  # agy leads, claude reviews

    monkeypatch.setattr(claudit.shutil, "which", lambda cmd: "/bin/claude" if cmd == "claude" else None)
    assert claudit.get_available_llm_engine() == "claude"

    monkeypatch.setattr(claudit, "LLM_ENGINE", "agy")
    assert claudit.get_available_llm_engine() is None  # no agy on path
    assert claudit.available_engines() == []

    monkeypatch.setattr(claudit.shutil, "which", lambda cmd: "/bin/agy" if cmd == "agy" else None)
    assert claudit.get_available_llm_engine() == "agy"

    monkeypatch.setattr(claudit, "LLM_ENGINE", "tandem")
    assert claudit.available_engines() == ["agy"]      # tandem degrades to whatever is installed
    assert claudit.get_available_llm_engine() == "agy"


def _both_engines(monkeypatch):
    monkeypatch.setattr(claudit, "LLM_ENGINE", "tandem")
    monkeypatch.setattr(claudit.shutil, "which",
                        lambda cmd: "/bin/" + cmd if cmd in ("agy", "claude") else None)


def test_llm_redact_tandem_unions_both_engines(monkeypatch):
    _both_engines(monkeypatch)
    monkeypatch.setattr(claudit, "LLM_SCRUB", True)
    monkeypatch.setattr(claudit, "_agy", lambda prompt, timeout: '["AcmeCorp"]')
    monkeypatch.setattr(claudit, "_claude", lambda prompt, timeout: '["jsmith", "req_KEEPME"]')
    out = claudit.llm_redact("AcmeCorp ticket from jsmith about req_KEEPME failing")
    assert "AcmeCorp" not in out and "jsmith" not in out   # each engine's find is applied
    assert "req_KEEPME" in out                             # protected even when a model lists it
    assert out.count("[REDACTED]") == 2


def test_llm_redact_tandem_survives_one_engine_failing(monkeypatch):
    _both_engines(monkeypatch)
    monkeypatch.setattr(claudit, "LLM_SCRUB", True)
    monkeypatch.setattr(claudit, "_agy", lambda prompt, timeout: (_ for _ in ()).throw(RuntimeError))
    monkeypatch.setattr(claudit, "_claude", lambda prompt, timeout: '["hostname9"]')
    assert "hostname9" not in claudit.llm_redact("ssh to hostname9 failed")


def test_llm_compose_tandem_review_redacts_and_rejects_slop(monkeypatch):
    _both_engines(monkeypatch)
    monkeypatch.setattr(claudit, "BURN_TOKENS", True)
    monkeypatch.setattr(claudit, "_agy", lambda prompt, timeout: "Block fired on AcmeCorp work.")
    monkeypatch.setattr(claudit, "_claude",
                        lambda prompt, timeout: '{"pii": ["AcmeCorp"], "slop": false}')
    out = claudit.llm_compose("write note", "ctx")
    assert out == "Block fired on [REDACTED] work."       # reviewer's PII find is redacted

    monkeypatch.setattr(claudit, "_claude", lambda prompt, timeout: '{"pii": [], "slop": true}')
    assert claudit.llm_compose("write note", "ctx") is None  # slop verdict -> template fallback

    # reviewer breakage never blocks the draft
    monkeypatch.setattr(claudit, "_claude", lambda prompt, timeout: "not json at all")
    assert claudit.llm_compose("write note", "ctx") == "Block fired on AcmeCorp work."


def test_llm_is_false_positive_tandem_veto(monkeypatch):
    _both_engines(monkeypatch)
    monkeypatch.setattr(claudit, "LLM_SCRUB", True)
    monkeypatch.setattr(claudit, "_agy",
                        lambda prompt, timeout: '{"false_positive": true, "reason": "in-scope"}')
    monkeypatch.setattr(claudit, "_claude",
                        lambda prompt, timeout: '{"false_positive": false, "reason": "mass posting"}')
    fp, reason = claudit.llm_is_false_positive("cyber", "blocked")
    assert fp is False and reason == "mass posting"       # either engine's clear no vetoes

    monkeypatch.setattr(claudit, "_claude",
                        lambda prompt, timeout: '{"false_positive": true, "reason": "fine"}')
    fp, _ = claudit.llm_is_false_positive("cyber", "blocked")
    assert fp is True                                     # both agree -> file it


def test_agy_llm_call_and_token_meter(monkeypatch, tmp_path):
    tok_file = str(tmp_path / "tokens.json")
    monkeypatch.setattr(claudit, "TOKENS_FILE", tok_file)

    def fake_run(cmd, capture_output, text, timeout):
        assert cmd[0] == "agy"
        assert "-p" in cmd
        # every headless call must be filed under the ClAudit Antigravity project, so the
        # user's own chat history stays clean and their manual conversations resumable
        assert cmd[cmd.index("--project") + 1] == "ClAudit"
        payload = json.dumps({
            "status": "SUCCESS",
            "response": " This is an agy generated defense. ",
            "usage": {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 20}
        })
        return type("R", (), {"stdout": payload, "stderr": "", "returncode": 0})()

    monkeypatch.setattr(claudit.subprocess, "run", fake_run)
    res = claudit._agy("test prompt", 30)
    assert res == "This is an agy generated defense."

    tokens = claudit.load_tokens()
    assert tokens["input"] == 100
    assert tokens["output"] == 50
    assert tokens["cache_read"] == 20
    assert tokens["calls"] == 1


def test_run_llm_dispatches_to_agy(monkeypatch):
    monkeypatch.setattr(claudit, "LLM_ENGINE", "agy")
    monkeypatch.setattr(claudit.shutil, "which", lambda cmd: "/bin/agy" if cmd == "agy" else None)
    monkeypatch.setattr(claudit, "_agy", lambda prompt, timeout: "agy response")
    assert claudit._run_llm("hello") == "agy response"


def test_llm_compose_with_agy(monkeypatch):
    monkeypatch.setattr(claudit, "BURN_TOKENS", True)
    monkeypatch.setattr(claudit, "LLM_ENGINE", "agy")
    monkeypatch.setattr(claudit.shutil, "which", lambda cmd: "/bin/agy" if cmd == "agy" else None)
    monkeypatch.setattr(claudit, "_run_llm", lambda prompt, timeout: "Composed defense note.")
    res = claudit.llm_compose("write defense", "ctx")
    assert res == "Composed defense note."


def test_reopen_dupe_closes_with_compose(monkeypatch):
    state = {}
    monkeypatch.setattr(cs, "_dup_flagged_numbers", lambda repo, state, cutoff, limit: [42])
    monkeypatch.setattr(cs, "closure_info", lambda repo, num: {"num": 42, "actor": "github-actions[bot]", "reason": "duplicate", "self": False})
    reopened_calls = []
    comments_posted = []

    def fake_run(cmd, capture_output, text):
        reopened_calls.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(cs.subprocess, "run", fake_run)
    monkeypatch.setattr(cs, "gh_comment", lambda repo, num, body: comments_posted.append((num, body)))
    monkeypatch.setattr(claudit, "llm_compose", lambda instr, ctx: "Bespoke reopen explanation for issue.")

    n = cs.reopen_dupe_closes("o/r", state, compose=True)
    assert n == 1
    assert len(reopened_calls) == 1
    assert comments_posted[0][0] == "42"
    assert "Bespoke reopen explanation for issue." in comments_posted[0][1]



# ---------------- closure intelligence: sweeps + merges ----------------
def test_merge_target_re_parses_hash_and_url():
    assert cs.MERGE_TARGET_RE.search("Closing as a duplicate of #85365 — please follow").group(1) == "85365"
    assert cs.MERGE_TARGET_RE.search(
        "closed as duplicate of https://github.com/o/r/issues/123").group(1) == "123"
    assert cs.MERGE_TARGET_RE.search("possible duplicate issues: #1, #2") is None


def test_classify_closure_swept_merged_and_open(monkeypatch):
    views = {
        "10": {"state": "CLOSED", "title": "t (req_A1)",
               "closedAt": "2026-08-15T10:10:00Z", "comments": [
                   {"author": {"login": "github-actions"},
                    "body": "Closing for now — inactive for too long."}]},
        "11": {"state": "CLOSED", "title": "t (req_B2)",
               "closedAt": "2026-08-15T16:08:00Z", "comments": [
                   {"author": {"login": "bcherny"},
                    "body": "Closing as a duplicate of #85365 — please follow and 👍 that issue."}]},
        "12": {"state": "CLOSED", "title": "t", "closedAt": "", "comments": []},
        "13": {"state": "OPEN"},
    }
    monkeypatch.setattr(cs, "_gh_json_wait", lambda args: views.get(args[2]))
    monkeypatch.setattr(cs, "_gh_json", lambda args: {"r": "duplicate"})   # REST reason fallback
    swept = cs.classify_closure("o/r", 10)
    assert (swept["kind"], swept["by"]) == ("swept", "github-actions")
    merged = cs.classify_closure("o/r", 11)
    assert (merged["kind"], merged["by"], merged["into"]) == ("merged", "bcherny", 85365)
    assert cs.classify_closure("o/r", 12)["kind"] == "merged"   # dup reason, no parseable comment
    assert cs.classify_closure("o/r", 13) is None


def test_swept_and_fold_markers_recognize_manual_run():
    manual_swept = ("Still relevant. An issue author cannot reopen after a bot close, so this "
                    "report is tracked with the rest of the 2026-08-15 sweep in #86940.")
    assert cs._is_swept_defense(manual_swept)
    assert cs._is_swept_defense("anything\n\n" + cs.SWEPT_MARKER)
    assert not cs._is_swept_defense("Not a duplicate — distinct Request ID.")
    manual_fold = ("Request IDs from the duplicates folded into this issue on 2026-08-15, same "
                   "trigger, separate blocked calls:\n- req_X (#85363)")
    assert cs._is_fold_note(manual_fold)
    assert cs._is_fold_note("body\n" + cs.FOLD_MARKER)
    assert not cs._is_fold_note("Comment citing #85363 without folding.")


def test_fold_note_md_lists_reqs_and_dups():
    md = cs.fold_note_md([(85363, {"title": "FP while: x (req_011AAA)"}),
                          (85364, {"title": "no req id here"})])
    assert "req_011AAA (#85363)" in md
    assert "request ID in issue body (#85364)" in md
    assert cs.FOLD_MARKER in md


def test_umbrella_md_groups_by_sweep_day():
    md = cs.umbrella_md([(70811, {"at": "2026-08-15T10:10:46Z", "title": "t (req_A)"}),
                         (70812, {"at": "2026-08-15T10:10:47Z", "title": "t (req_B)"}),
                         (60000, {"at": "2026-07-01T09:00:00Z", "title": "t (req_C)"})])
    assert "2026-08-15 sweep — 2 report(s)" in md
    assert "2026-07-01 sweep — 1 report(s)" in md
    assert "- #70811 req_A" in md
    assert md.index("2026-08-15") < md.index("2026-07-01")      # newest sweep first


def test_defend_swept_skips_already_defended_and_posts_note(monkeypatch):
    state = {"__closures__": {
        "70811": {"kind": "swept", "at": "2026-08-15T10:10:00Z", "title": "t (req_A)"},
        "70812": {"kind": "swept", "at": "2026-08-15T10:10:01Z", "title": "t (req_B)"},
        "85365": {"kind": "merged", "into": 85348, "title": "t"},
    }}
    views = {
        "70811": {"state": "CLOSED", "comments": [                # manually defended earlier today
            {"author": {"login": "me"},
             "body": "Still relevant. An issue author cannot reopen after a bot close, so this "
                     "report is tracked with the rest of the 2026-08-15 sweep in #86940."}]},
        "70812": {"state": "CLOSED", "comments": []},
    }
    monkeypatch.setattr(cs, "_gh_json_wait", lambda args: views.get(args[2]))
    monkeypatch.setattr(cs, "gh_login", lambda: "me")
    monkeypatch.setattr(cs.subprocess, "run", lambda cmd, capture_output, text: type(
        "R", (), {"returncode": 1, "stdout": "", "stderr": "GraphQL: Could not reopen"})())
    posted = []
    monkeypatch.setattr(cs, "gh_comment", lambda repo, num, body: posted.append((num, body)))
    n = cs.defend_swept("o/r", state, delay=0)
    assert n == 1                                                 # only 70812 needed a note
    assert posted[0][0] == "70812"
    assert "#86940" in posted[0][1] and cs.SWEPT_MARKER in posted[0][1]
    assert state["__swept_defended__"]["70811"] == "noted:#86940"
    assert state["__swept_defended__"]["70812"] == "noted:#86940"
    assert cs.defend_swept("o/r", state, delay=0) == 0            # idempotent second pass


def test_fold_merged_folds_only_new_dups(monkeypatch):
    state = {"__closures__": {
        "85363": {"kind": "merged", "into": 85348, "title": "t (req_X1)"},
        "85364": {"kind": "merged", "into": 85348, "title": "t (req_X2)"},
    }}
    canon_comments = {"comments": [
        {"author": {"login": "me"},
         "body": "Request IDs from the duplicates folded into this issue on 2026-08-15, same "
                 "trigger, separate blocked calls:\n- req_X1 (#85363)"}]}
    monkeypatch.setattr(cs, "_gh_json_wait", lambda args: canon_comments)
    monkeypatch.setattr(cs, "gh_login", lambda: "me")
    posted, reacted = [], []
    monkeypatch.setattr(cs, "gh_comment", lambda repo, num, body: posted.append((num, body)))
    monkeypatch.setattr(cs.subprocess, "run", lambda cmd, capture_output, text: (
        reacted.append(cmd), type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())[1])
    n = cs.fold_merged("o/r", state, delay=0)
    assert n == 1
    assert posted[0][0] == "85348"
    assert "req_X2 (#85364)" in posted[0][1]                      # only the un-folded dup
    assert "req_X1" not in posted[0][1]
    assert any("reactions" in " ".join(c) for c in reacted)       # 👍 the canonical
    assert sorted(state["__folded__"]["85348"]) == [85363, 85364]
    assert cs.fold_merged("o/r", state, delay=0) == 0             # idempotent second pass


def test_closure_summary_rollup():
    state = {"__closures__": {
        "1": {"kind": "swept", "at": "2026-08-15T10:00:00Z"},
        "2": {"kind": "swept", "at": "2026-08-15T10:01:00Z"},
        "3": {"kind": "merged", "into": 99, "at": "2026-08-15T16:00:00Z"},
        "4": {"kind": "closed", "at": ""},
    }, "__swept_defended__": {"1": "noted:#86940"}, "__folded__": {"99": [3]}}
    s = cs.closure_summary(state)
    assert (s["swept"], s["merged"], s["defended"], s["folded"]) == (2, 1, 1, 1)
    assert s["sweep_days"] == {"2026-08-15": 2}
    assert s["merged_map"] == {"3": 99}


# ---------------- mute list: legal-case / too-sensitive-to-post findings ----------------
def _mute_finding(**kw):
    f = {"kind": "cyber", "sig": "abc123def456", "block_text": "blocked", "prompt": "",
         "leadup": [], "occ": [{"req": "req_X1", "ts": "2026-08-21", "session": "s",
                                "proj": "/home/u/proj"}]}
    f.update(kw)
    return f


def test_muted_terms_reload_on_change(tmp_path, monkeypatch):
    mf = tmp_path / "mute.txt"
    monkeypatch.setattr(cs, "MUTE_FILE", str(mf))
    monkeypatch.setattr(cs, "_MUTE_CACHE", (None, []))
    assert cs.muted_terms() == []                    # no file -> nothing muted
    mf.write_text("# comment\npepper\n\nPEPCON\n")
    assert cs.muted_terms() == ["pepper", "pepcon"]  # lowered, comments/blanks dropped
    os.utime(mf, (1, 1))
    mf.write_text("other\n")
    os.utime(mf, (2, 2))
    assert cs.muted_terms() == ["other"]             # mtime change -> reload, no restart needed


def test_is_muted_matches_all_finding_fields(tmp_path, monkeypatch):
    mf = tmp_path / "mute.txt"
    mf.write_text("pepper\npepcon\n")
    monkeypatch.setattr(cs, "MUTE_FILE", str(mf))
    monkeypatch.setattr(cs, "_MUTE_CACHE", (None, []))
    assert not cs.is_muted(_mute_finding())
    assert cs.is_muted(_mute_finding(block_text="work on PepperConnector blocked"))  # substring, any case
    assert cs.is_muted(_mute_finding(prompt="fix the pepcon sync"))
    assert cs.is_muted(_mute_finding(leadup=[("user", "the Pepper VPN keeps dropping")]))
    assert cs.is_muted(_mute_finding(occ=[{"req": "req_Y", "ts": "", "session": "s",
                                      "proj": "/home/u/pepper-keeper"}]))


def test_should_file_and_file_one_refuse_muted(tmp_path, monkeypatch):
    mf = tmp_path / "mute.txt"
    mf.write_text("pepper\n")
    monkeypatch.setattr(cs, "MUTE_FILE", str(mf))
    monkeypatch.setattr(cs, "_MUTE_CACHE", (None, []))
    f = _mute_finding(block_text="pepper-vpn work blocked")
    assert cs.should_file(f) is False                # auto paths never see it
    created = []
    monkeypatch.setattr(cs, "gh_create", lambda repo, t, b: created.append(t) or "u/1")
    assert cs.file_one(f, "", "o/r", {}) == (None, None, None)   # manual push refused too
    assert created == []


# ---------------- agy-only mode: cross-checking without claude quota ----------------
def _agy_only(monkeypatch):
    monkeypatch.setattr(claudit, "LLM_ENGINE", "agy")
    monkeypatch.setattr(claudit.shutil, "which", lambda cmd: "/bin/agy" if cmd == "agy" else None)


def test_engine_slots_agy_only_second_voice(monkeypatch):
    _agy_only(monkeypatch)
    monkeypatch.setattr(claudit, "AGY_REVIEW_MODEL", "gemini-3.1-pro-low")
    assert claudit.engine_slots() == [("agy", None), ("agy", "gemini-3.1-pro-low")]
    monkeypatch.setattr(claudit, "AGY_REVIEW_MODEL", "")
    assert claudit.engine_slots() == [("agy", None)]        # disabled -> single voice
    # tandem (both CLIs) keeps the two real engines, no extra agy voice
    monkeypatch.setattr(claudit, "LLM_ENGINE", "tandem")
    monkeypatch.setattr(claudit, "AGY_REVIEW_MODEL", "gemini-3.1-pro-low")
    monkeypatch.setattr(claudit.shutil, "which", lambda cmd: "/bin/" + cmd if cmd in ("agy", "claude") else None)
    assert claudit.engine_slots() == [("agy", None), ("claude", None)]


def test_agy_model_override_in_cmd(monkeypatch):
    seen = []

    def fake_run(cmd, capture_output, text, timeout):
        seen.append(cmd)
        return type("R", (), {"stdout": "{}", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(claudit.subprocess, "run", fake_run)
    claudit._agy("x", 30, model="gemini-3.1-pro-low")
    assert seen[0][seen[0].index("--model") + 1] == "gemini-3.1-pro-low"


def test_llm_redact_agy_only_runs_both_voices(monkeypatch):
    _agy_only(monkeypatch)
    monkeypatch.setattr(claudit, "LLM_SCRUB", True)
    monkeypatch.setattr(claudit, "AGY_REVIEW_MODEL", "gemini-3.1-pro-low")
    calls = []

    def fake_agy(prompt, timeout, model=None):
        calls.append(model)
        return '["AcmeCorp"]' if model is None else '["jsmith"]'

    monkeypatch.setattr(claudit, "_agy", fake_agy)
    out = claudit.llm_redact("AcmeCorp ticket from jsmith")
    assert calls == [None, "gemini-3.1-pro-low"]            # two agy voices, zero claude calls
    assert "AcmeCorp" not in out and "jsmith" not in out    # findings unioned


def test_version_is_consistent_across_badge_changelog_and_package():
    """The README badge, the CHANGELOG, and pyproject's dynamic version all key off __version__;
    2.0.111 sat on the badge through five releases before CI checked this."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    v = cs.__version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", v)
    with open(os.path.join(root, "README.md")) as fh:
        assert f"version-{v}-" in fh.read()
    with open(os.path.join(root, "CHANGELOG.md")) as fh:
        assert f"## [{v}]" in fh.read()
    with open(os.path.join(root, "pyproject.toml")) as fh:
        assert 'version = { attr = "claudit_scan.__version__" }' in fh.read()


SAMPLE_USAGE = {
    "five_hour": {"utilization": 16.0, "resets_at": "2099-01-01T07:50:00.370022+00:00"},
    "seven_day": {"utilization": 38.0, "resets_at": "2099-01-03T06:00:00.370043+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 16, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 38, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 62, "is_active": True,
         "resets_at": "2099-01-03T06:00:00.370251+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
    "extra_usage": {"is_enabled": False},
}


def _fake_urlopen(payload, calls):
    class R:
        def __init__(self):
            self._b = json.dumps(payload).encode()
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    def urlopen(req, timeout=0):
        calls.append(req)
        assert req.full_url == claudit.USAGE_URL
        assert req.get_header("Authorization") == "Bearer tok123"
        return R()
    return urlopen


def test_plan_usage_reads_login_parses_windows_and_caches(tmp_path, monkeypatch):
    """The meter shows the REAL 5h/7d windows (what Claude Code's /usage shows), read with the
    machine's own Claude Code login; one fetch per USAGE_TTL, served from the disk cache between."""
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cred = tmp_path / "creds.json"
    cred.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok123", "subscriptionType": "max"}}))
    monkeypatch.setattr(claudit, "CREDENTIALS_FILE", str(cred))
    monkeypatch.setattr(claudit, "USAGE_CACHE", str(tmp_path / "usage.json"))
    calls = []
    monkeypatch.setattr(claudit.urllib.request, "urlopen", _fake_urlopen(SAMPLE_USAGE, calls))
    u = claudit.plan_usage()
    assert u["plan"] == "max"
    assert (u["five_hour"]["pct"], u["seven_day"]["pct"]) == (16.0, 38.0)
    assert u["scoped"] == [{"name": "Fable", "pct": 62.0, "active": True,
                            "resets_at": "2099-01-03T06:00:00.370251+00:00"}]
    assert claudit.usage_peak(u) == 62.0
    assert len(calls) == 1
    # second call inside the TTL: cache only, no network; fetch=False never touches the network
    assert claudit.plan_usage()["seven_day"]["pct"] == 38.0
    assert claudit.plan_usage(fetch=False)["fetched"] == u["fetched"]
    assert len(calls) == 1
    # expired cache -> refetch
    assert claudit.plan_usage(max_age=0)["plan"] == "max"
    assert len(calls) == 2
    text = claudit.usage_summary(u, claudit.load_tokens())
    assert "7-day window     38.0%" in text and "Fable" in text and "(active limit)" in text
    assert "tok123" not in text                                 # the token never appears in output


def test_plan_usage_without_login_is_none_and_fetch_failure_keeps_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setattr(claudit, "CREDENTIALS_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setattr(claudit, "USAGE_CACHE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(claudit.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(claudit.urllib.request, "urlopen", _fake_urlopen(SAMPLE_USAGE, calls))
    assert claudit.plan_usage() is None and calls == []         # API-key user: estimate fallback
    assert "Live plan usage unavailable" in claudit.usage_summary(None, claudit.load_tokens())
    # env token works, then a failed refresh keeps the last snapshot instead of blanking the meter
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok123")
    assert claudit.plan_usage()["seven_day"]["pct"] == 38.0
    def boom(req, timeout=0):
        raise claudit.urllib.error.URLError("offline")
    monkeypatch.setattr(claudit.urllib.request, "urlopen", boom)
    assert claudit.plan_usage(max_age=0)["seven_day"]["pct"] == 38.0


def test_record_tokens_per_engine_and_weekly_usage(tmp_path, monkeypatch):
    """agy calls carry no dollar figure, which used to leave the weekly history empty (the meter
    read $0 / 0% forever in agy mode). Every call now lands in history with its engine and token
    count, and the tallies are kept per engine as well as in the legacy totals."""
    import time
    monkeypatch.setattr(claudit, "TOKENS_FILE", str(tmp_path / "tokens.json"))
    now = time.time()
    legacy = {"input": 10, "output": 5, "cache_read": 0, "cache_creation": 0, "calls": 1, "cost": 1.0,
              "history": [[int(now - 3600), 1.0], [int(now - 10 * 86400), 9.0]]}   # old 2-field shape
    (tmp_path / "tokens.json").write_text(json.dumps(legacy))
    claudit._record_tokens({"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 20},
                           cost=None, engine="agy")
    claudit._record_tokens({"input_tokens": 30, "output_tokens": 10}, 0.25, engine="claude")
    t = claudit.load_tokens()
    assert t["calls"] == 3 and t["total"] == 15 + 170 + 40
    assert t["engines"]["agy"] == {"input": 100, "output": 50, "cache_read": 20, "cache_creation": 0,
                                   "calls": 1, "cost": 0.0, "total": 170}
    assert t["engines"]["claude"]["calls"] == 1 and abs(t["engines"]["claude"]["cost"] - 0.25) < 1e-9
    wk = claudit.weekly_usage(t)
    assert wk["agy"] == {"calls": 1, "tokens": 170, "cost": 0.0}
    assert wk["claude"]["calls"] == 2 and abs(wk["claude"]["cost"] - 1.25) < 1e-9   # legacy entry counted
    assert abs(claudit.weekly_cost(t) - 1.25) < 1e-9
    assert all(len(e) == 4 for e in t["history"][1:]) and len(t["history"]) == 3     # 10-day-old pruned


def test_fmt_reset():
    import calendar, time
    now = calendar.timegm(time.strptime("2026-09-24T05:00:00", "%Y-%m-%dT%H:%M:%S"))
    assert claudit.fmt_reset("2026-09-24T07:50:00.370022+00:00", now) == "in 2h 50m"
    assert claudit.fmt_reset("2026-09-27T06:00:00+00:00", now) == "in 3d 1h"
    assert claudit.fmt_reset("2026-09-24T04:00:00+00:00", now) == "resetting"
    assert claudit.fmt_reset("", now) == "" and claudit.fmt_reset("garbage", now) == ""


def test_usage_guard_abstains_claude_calls_only(monkeypatch):
    """Past the guard threshold, claude calls return '' (callers treat that as a broken engine and
    fall back); agy calls are untouched; at 100 the guard is off."""
    calls = []
    monkeypatch.setattr(claudit, "_claude", lambda prompt, timeout, model=None: calls.append("claude") or "c")
    monkeypatch.setattr(claudit, "_agy", lambda prompt, timeout, model=None: calls.append("agy") or "a")
    monkeypatch.setattr(claudit, "available_engines", lambda: ["agy", "claude"])
    snap = {"five_hour": {"pct": 20.0}, "seven_day": {"pct": 93.0}, "scoped": []}
    monkeypatch.setattr(claudit, "plan_usage", lambda *a, **k: snap)
    monkeypatch.setattr(claudit, "USAGE_GUARD_PCT", 90)
    assert claudit.usage_guarded() == (True, "7-day window at 93% (guard 90%)")
    assert claudit._run_llm("p", engine="claude") == ""
    assert claudit._run_llm("p", engine="agy") == "a"
    monkeypatch.setattr(claudit, "USAGE_GUARD_PCT", 100)
    assert claudit.usage_guarded() == (False, "")
    assert claudit._run_llm("p", engine="claude") == "c"
    monkeypatch.setattr(claudit, "USAGE_GUARD_PCT", 90)
    monkeypatch.setattr(claudit, "plan_usage", lambda *a, **k: None)   # no login: never guards
    assert claudit._run_llm("p", engine="claude") == "c"
    assert calls == ["agy", "claude", "claude"]


def test_latest_release_and_version_tuple(monkeypatch):
    class R:
        def read(self):
            return b'{"tag_name": "v9.1.0"}'
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: R())
    assert cs.latest_release_version() == "9.1.0"
    assert cs.version_tuple("v2.10.3") == (2, 10, 3) > cs.version_tuple("2.9.99")
    assert cs.version_tuple("garbage") == ()
    def boom(req, timeout=0):
        raise OSError("offline")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert cs.latest_release_version() == ""


def test_doctor_rows_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(cs, "LOCK_FILE", str(tmp_path / "state" / "watcher.lock"))
    monkeypatch.setattr(cs, "CONFIG_FILE", str(tmp_path / "state" / "config.json"))
    monkeypatch.setattr(cs, "MUTE_FILE", str(tmp_path / "state" / "mute.txt"))
    monkeypatch.setattr(cs, "PROJECTS", str(tmp_path / "projects"))
    monkeypatch.setattr(claudit, "CREDENTIALS_FILE", str(tmp_path / "nocreds.json"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    rows = cs.doctor_rows(fetch=False)
    labels = [r[1] for r in rows]
    assert {"Python", "gh CLI", "claude CLI", "agy CLI", "LLM engine", "Claude plan usage",
            "PyQt6 (GUI)", "Desktop notifications", "Claude Code sessions", "State dir",
            "config.json", "scrub.txt", "mute.txt", "Watcher"} <= set(labels)
    assert all(r[0] in ("ok", "warn", "fail") for r in rows)
    by = {r[1]: r for r in rows}
    assert by["Claude Code sessions"][0] == "fail"                # no transcripts dir in tmp
    assert by["Claude plan usage"][0] == "warn"                   # no login
    assert by["State dir"][0] == "ok" and by["Watcher"] == ("ok", "Watcher", "not running")
    text = cs.doctor_text(rows)
    assert text.count("\n") == len(rows) - 1 and "✗ Claude Code sessions" in text


def test_render_poll_helpers(tmp_path, monkeypatch):
    """The hourly Action rewrites the README blocks and the trend SVG from these; a break here
    silently stalls the public counter."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import render_poll as rp
    md = rp.render_md({"plus": 1, "minus": 3, "eyes": 0, "total": 4}, "2026-09-23 21:00")
    assert md.startswith(rp.START) and md.rstrip().endswith(rp.END)
    assert "| 👎 Claude Code stays broken | `████████░░` | **75%** (3) |" in md
    assert "4 vote(s)" in md
    monkeypatch.setattr(rp, "HISTORY_JSON", str(tmp_path / "hist.json"))
    h = rp.append_history({"open_api": 5, "closed_api": 1, "harness": 0}, "2026-09-23 21:00 UTC")
    h = rp.append_history({"open_api": 6, "closed_api": 1, "harness": 0}, "2026-09-23 21:40 UTC")
    assert len(h) == 1 and h[0]["open_api"] == 6                  # same hour overwrites
    h = rp.append_history({"open_api": 7, "closed_api": 2, "harness": 1}, "2026-09-23 22:05 UTC")
    assert len(h) == 2 and json.load(open(tmp_path / "hist.json"))[-1]["harness"] == 1
    svg = rp.render_trend_svg(h)
    assert svg.startswith("<svg") and "open 7" in svg and "closed 2" in svg and "harness 1" in svg
    assert rp._g({"open": 3}, "open_api") == 3                    # legacy key name still read
    assert "trend builds hourly" in rp.render_trend_svg([])


def test_fold_merged_records_locked_canonical_instead_of_retrying(monkeypatch):
    """Maintainers locked three canonicals; every 15-minute pass re-ran `gh issue comment` on each
    and logged a failure. A locked canonical is now recorded and left alone."""
    import subprocess
    state = {"__closures__": {"72091": {"kind": "merged", "into": 71888, "title": "t (req_A)"}}}
    monkeypatch.setattr(cs, "_gh_json_wait", lambda args: {"comments": []})
    monkeypatch.setattr(cs, "gh_login", lambda: "me")
    attempts = []
    def comment(repo, num, body):
        attempts.append(num)
        raise subprocess.CalledProcessError(1, ["gh", "issue", "comment"])
    monkeypatch.setattr(cs, "gh_comment", comment)
    monkeypatch.setattr(cs, "_issue_locked", lambda repo, num: True)
    monkeypatch.setattr(cs, "save_state", lambda st: None)
    assert cs.fold_merged("o/r", state, delay=0) == 0
    assert state["__folded__"]["71888"] == [72091] and state["__locked__"] == {"71888": "fold"}
    assert cs.fold_merged("o/r", state, delay=0) == 0 and attempts == ["71888"]   # no second try
    # an unlocked failure is still retried next pass
    state2 = {"__closures__": {"72091": {"kind": "merged", "into": 71888, "title": "t (req_A)"}}}
    monkeypatch.setattr(cs, "_issue_locked", lambda repo, num: False)
    cs.fold_merged("o/r", state2, delay=0)
    assert state2["__folded__"].get("71888", []) == [] and len(attempts) == 2
