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
- CI runs `py_compile` + `ruff check --select E9,F63,F7,F82` + `pytest tests/` on Python 3.9, 3.12,
  and 3.13, then builds the wheel and smoke-tests the console scripts. Run the fast part locally
  before pushing: `pip install ruff pytest && ruff check --select E9,F63,F7,F82 . && pytest tests/ -q`.
  Add a test for any behavior change (the suite mocks `gh`/`claude` and never touches the network).
- **Bump `__version__` (in `claudit_scan.py`) on every code change** — no exceptions — and add a
  matching `CHANGELOG.md` section. CI fails if the README badge or the changelog disagrees with
  `__version__`; the pre-commit hook (`git config core.hooksPath scripts/githooks`) keeps the badge
  in step and auto-bumps the patch number when you forget.
- Python 3.9 is the floor: no `match`, no `X | Y` type unions, no `str.removeprefix`-era assumptions
  beyond 3.9.

## Releasing

Releases are tag-driven. Set `__version__`, add the `CHANGELOG.md` section, commit, then:

```bash
git tag v2.3.0 && git push origin main v2.3.0
```

`.github/workflows/release.yml` checks the tag against `__version__`, builds the sdist and wheel,
takes that version's changelog section as the notes, and publishes the GitHub Release.

## Ground rules

ClAudit reports **legitimate, in-scope** false positives. Don't add anything designed to spam a
repo, evade duplicate detection, or post content that isn't a genuine false positive. Quality over
volume — that's the whole point.

By contributing you agree your work is licensed under the project's [GNU GPL v3.0](LICENSE).
