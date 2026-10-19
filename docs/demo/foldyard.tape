# The README demo: `foldyard init` → `fy box up` (review + adopt) → `fy verify` in the box → the
# box edits foldyard.toml → `fy box up` shows the diff and asks → the change, live.
#
# Rendered by VHS (https://github.com/charmbracelet/vhs) against a REAL host: every command runs,
# nothing is mocked. Record it with `just demo` (the VM work makes it slow — minutes, not
# seconds) or the `demo` workflow, which the release workflow runs after each release and which
# attaches the GIF to the GitHub Release.
#
# The long waits (the VM booting, the box image building) are cut with Hide/Show: the GIF shows the
# flow, not the wait, and a `#` comment typed after each cut says so. Everything else plays in
# real time.
#
# Inputs, from the environment the recorder sets (`just demo` and the workflow both do):
#   FY_DEMO_SETUP  docs/demo/setup.sh — the off-camera repo + state setup
#   FY_DEMO_DIR    where the demo repo is created (wiped; the VM is named after it)
#   FY_DEMO_STATE  a throwaway FOLDYARD_STATE_DIR (wiped)
# and the output path is the recorder's `vhs -o …`, so the tape names none.
#
# Every `fy …` typed below must exist in the CLI: tests/test_agent_guide.py checks it, as it does
# for the skills and the docs.

Require git
Require fy
Require foldyard

Set Shell "bash"
Set FontSize 16
Set Width 1280
Set Height 760
Set Padding 20
Set Theme "Catppuccin Mocha"
Set Framerate 24
Set TypingSpeed 45ms
# The prompt setup.sh sets ends in `$ `, and so does the box's — either means "command done".
Set WaitPattern /\$\s*$/
# Generous: the first `fy box up` on a cold machine boots a VM and builds an image.
Set WaitTimeout 25m

# ── off camera: a fresh repo, a fresh state dir ─────────────────────────────────────────────────
Hide
Type `source "$FY_DEMO_SETUP" "$FY_DEMO_DIR" "$FY_DEMO_STATE" && clear`
Enter
Wait
Show

Sleep 500ms
Type "ls"
Enter
Wait
Sleep 1s

# ── 1. scaffold ─────────────────────────────────────────────────────────────────────────────────
Type "foldyard init"
Enter
Wait
Sleep 3s

# ── 2. bring the box up: the host shows what it will run, and asks ──────────────────────────────
Type "clear"
Enter
Wait
Type "fy box up"
Enter
Wait+Screen /\[a\]dopt/
Sleep 4s
Type "a"
Sleep 500ms
Enter
Sleep 2s

# The cut: VM boot + box image build.
Hide
Wait
Type "clear"
Enter
Wait
Show
Type "# (VM boot + box build, a few minutes on first run: trimmed)"
Enter
Wait
Sleep 1s
Type "fy box ps"
Enter
Wait
Sleep 2s

# ── 3. in the box: verify the boundary ──────────────────────────────────────────────────────────
Type "fy box shell"
Enter
Wait
Sleep 1s
Type "fy verify"
Enter
Wait@10m
Sleep 4s

# ── 4. the box edits the config, as an agent in it would ────────────────────────────────────────
Type "clear"
Enter
Wait
Type "# the box can write foldyard.toml — e.g. ask for ripgrep:"
Enter
Wait
Type "cat >> foldyard.toml <<'EOF'"
Enter
Type "[box]"
Enter
Type `packages = ["ripgrep"]`
Enter
Type "EOF"
Enter
Wait
Sleep 1s
Type "exit"
Enter
Wait
Sleep 500ms

# ── 5. …but your computer doesn't run it until you say so ───────────────────────────────────────
Type "clear"
Enter
Wait
Type "fy box up"
Enter
Wait+Screen /\[a\]dopt/
Sleep 4s
Type "a"
Sleep 500ms
Enter
Sleep 2s

# The cut: the box image rebuilt with the new layer, the box recreated.
Hide
Wait
Type "clear"
Enter
Wait
Show
Type "# (box rebuilt with the adopted config: trimmed)"
Enter
Wait
Type `fy box exec "rg --version"`
Enter
Wait
Sleep 4s
