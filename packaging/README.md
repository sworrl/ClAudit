# Packaging

Two starting points for installing ClAudit without a git clone. Neither has been run on a clean
machine yet; both are here so someone with the right box can try them and report on
[issue #2](https://github.com/sworrl/ClAudit/issues/2).

## Homebrew (macOS, Linux)

```bash
brew install --HEAD --formula ./packaging/homebrew/claudit.rb
```

Installs `claudit` and `claudit-watch` into a Homebrew virtualenv, with `gh` as a dependency. The
tray app needs PyQt6, which the formula does not carry; install it with pipx (the caveats text
prints the command). It is a head formula (builds from `main`) because there is no checksum
workflow for tagged tarballs yet.

## Arch (AUR-style)

```bash
cd packaging/aur && makepkg -si
```

`claudit-git` builds the wheel from `main`, installs the three console scripts, the icon, a
`.desktop` entry, and lists `python-pyqt6` and `libnotify` as optional dependencies. Publishing it
to the AUR needs an AUR account and a generated `.SRCINFO` (`makepkg --printsrcinfo > .SRCINFO`).

## Flatpak

Not started. PyQt6 inside a Flatpak runtime plus access to `~/.claude/projects` and the `gh` login
makes this the most work of the three.
