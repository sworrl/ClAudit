"""Core tests for ClAudit — no network, no real gh/claude (all mocked/off)."""
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
