"""Core tests for ClAudit — no network, no real gh/claude (all mocked/off)."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claudit            # noqa: E402
import claudit_scan as cs  # noqa: E402


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
    # '_' is a word char, which is how 'PYTHIA' leaked into a public issue title (PYTHIA_DRY_RUN).
    monkeypatch.setattr(claudit, "_EXTRA", ["Pythia"])
    out, _ = claudit.scrub("Starting PYTHIA_DRY_RUN=0 and pythia-bot and Pythia2 today")
    assert "PYTHIA" not in out.upper().replace("REDACTED", "")   # every occurrence redacted
    assert "[REDACTED]_DRY_RUN" in out
    # letter-substrings still safe: 'Pythian' (letter suffix) and 'Markdown' must be untouched
    out2, _ = claudit.scrub("the Pythian games and Markdown")
    assert "Pythian" in out2 and "Markdown" in out2


def test_scrub_denylist_never_corrupts_request_id(monkeypatch):
    # a short denylist term ('FT') can fall between digits inside a Request ID; the denylist pass
    # must NOT redact it there, or the report loses the ID Anthropic needs to look up.
    monkeypatch.setattr(claudit, "_EXTRA", ["FT"])
    out, _ = claudit.scrub("see req_011CcPL8dVfbJY8FT6drDsEj for details")
    assert "req_011CcPL8dVfbJY8FT6drDsEj" in out      # Request ID intact, FT preserved inside it
    # a standalone 'FT' outside an ID is still redacted
    out2, _ = claudit.scrub("hosted by FT corp")
    assert "FT corp" not in out2 and "[REDACTED]" in out2


# ---------------- classification ----------------
@pytest.mark.parametrize("text,kind", [
    ("safety measures that flagged this message for a cybersecurity topic", "cyber"),
    ("flagged this message as a cybersecurity", "cyber"),          # PR #5
    ("safety filter detected cybersecurity", "cyber"),             # PR #5
    ("API Error: Opus 4.8's safeguards flagged this message for a cybersecurity topic", "cyber"),  # reworded
    ("Sonnet's safeguards flagged this message for a cybersecurity topic", "cyber"),               # reworded
    ("appears to violate our Usage Policy", "aup"),
    ("Claude Code is unable to respond to this request", "aup"),
    ("blocked: against our usage policy", "aup"),                  # PR #5
    ("usage policy violation", "aup"),                             # PR #5
    ("content policy violation", "aup"),                           # PR #5
    ("API Error: 529 Overloaded", "overloaded"),
    ("You've hit your limit", "limit"),
    ("just normal text", "other"),
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
