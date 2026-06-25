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
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_REPO = "anthropics/claude-code"

# (label, compiled pattern, replacement). Order matters: secrets/specific first.
SCRUBBERS = [
    ("anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "[REDACTED_ANTHROPIC_KEY]"),
    ("openai key",    re.compile(r"sk-[A-Za-z0-9]{20,}"),        "[REDACTED_API_KEY]"),
    ("github token",  re.compile(r"gh[opsu]_[A-Za-z0-9]{20,}"),  "[REDACTED_GH_TOKEN]"),
    ("aws key",       re.compile(r"AKIA[0-9A-Z]{16}"),           "[REDACTED_AWS_KEY]"),
    ("bearer token",  re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}"), "Bearer [REDACTED_TOKEN]"),
    ("jwt",           re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED_JWT]"),
    # exemption-form / generic ?token= blobs (e.g. claude.com/form/cyber-use-case?token=...)
    ("url token",     re.compile(r"(?i)([?&]token=)[A-Za-z0-9._\-]{16,}"), r"\1[REDACTED_TOKEN]"),
    ("email",         re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    # home dirs -> keep the structure, drop the username
    ("home path",     re.compile(r"(/home/|/Users/|C:\\Users\\)[^/\\\s]+"), r"\1[USER]"),
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


def scrub(text: str):
    counts = {}
    for label, pattern, repl in SCRUBBERS:
        text, n = pattern.subn(repl, text)
        if n:
            counts[label] = counts.get(label, 0) + n
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
