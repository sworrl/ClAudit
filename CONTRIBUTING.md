# Contributing to ClAudit

Thanks for helping make false-positive reporting less painful for everyone using Claude Code.

## Ways to help

- **Test on your platform.** Windows and macOS especially — does the tray app render? Do
  notifications work? Open an issue with your OS + what happened.
- **New block signatures.** If you hit a server-side block ClAudit doesn't classify yet, paste
  the (PII-scrubbed) error text and a Request ID into an issue so we can add it to `classify()`.
- **Better PII scrubbing.** Add patterns to `SCRUBBERS` in `claudit.py` with a test case.
- **Docs / packaging.** Homebrew formula, AUR package, `.msi`, Flatpak — all welcome.

## Dev setup

```bash
git clone https://github.com/sworrl/ClAudit.git
cd ClAudit
pip install -r requirements.txt
gh auth login
python3 claudit_scan.py            # dry-run; reads ~/.claude/projects, posts nothing
```

Nothing posts to GitHub unless you pass `--auto`/`--file-pending` or click in the GUI — safe to
hack on. State lives in `~/.claude/claudit/`; delete it to reset.

## Pull requests

- Keep changes focused; match the existing style (stdlib-first, no heavy deps in the core).
- The core (`claudit_scan.py`, `claudit.py`) must stay importable without PyQt6.
- CI runs `py_compile` + `ruff check --select E9,F63,F7,F82`. Run it locally before pushing.
- Add a line to `CHANGELOG.md` and bump `__version__` for user-facing changes.

## Ground rules

ClAudit reports **legitimate, in-scope** false positives. Don't add anything designed to spam a
repo, evade duplicate detection, or post content that isn't a genuine false positive. Quality over
volume — that's the whole point.

By contributing you agree your work is licensed under the project's [MIT License](LICENSE).
