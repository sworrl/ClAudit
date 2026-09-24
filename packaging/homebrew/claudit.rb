# Homebrew head formula for ClAudit's command-line tools (claudit, claudit-watch).
#
#   brew install --HEAD --formula ./packaging/homebrew/claudit.rb
#
# It installs from the main branch, because the project has no stable tarball checksum workflow
# yet. The tray app (claudit-gui) needs PyQt6, which is not packaged here; install it with
#   pipx install "claudit[gui] @ https://github.com/sworrl/ClAudit/archive/refs/heads/main.tar.gz"
# or from a clone with pip. Untested on a clean Mac as of 2.6.0; reports welcome on issue #2.
class Claudit < Formula
  include Language::Python::Virtualenv

  desc "Catch false-positive Claude Code safety/policy blocks, scrub PII, file GitHub issues"
  homepage "https://github.com/sworrl/ClAudit"
  head "https://github.com/sworrl/ClAudit.git", branch: "main"
  license "GPL-3.0-or-later"

  depends_on "gh"
  depends_on "python@3.12"

  def install
    venv = virtualenv_create(libexec, "python3.12")
    venv.pip_install_and_link buildpath
  end

  def caveats
    <<~EOS
      claudit and claudit-watch are on your PATH. Sign in to GitHub first:
        gh auth login
      Then baseline once so the existing backlog is not filed all at once:
        claudit-watch --baseline
      The tray app (claudit-gui) is not in this formula; it needs PyQt6:
        pipx install "claudit[gui] @ https://github.com/sworrl/ClAudit/archive/refs/heads/main.tar.gz"
    EOS
  end

  test do
    assert_match "ClAudit", shell_output("#{bin}/claudit-watch --version")
  end
end
