# The README demo: `foldyard init` → review + adopt the config → grant its recommended egress →
# `fy box up` → `fy verify` in the box → the box edits foldyard.toml → `fy box up` shows the diff
# and asks → the change, live.
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
# A prompt means "command done": setup.sh's ends in `$ `; the box shell is root (the box runs as
# `--user 0`), so Debian's default prompt there ends in `# `.
Set WaitPattern /[#$]\s*$/
# The first `fy box up` boots a VM (a clone of the base, when one is baked) and builds an image.
# Every prompt that path can raise is answered by a command typed below, so a Wait that runs out
# means one this tape doesn't know about — fail at this, not at the job's own timeout.
Set WaitTimeout 15m

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

# ── 2. your computer runs only a config you adopted, with only the egress you granted ───────────
# Both are asked at the first `fy box up` too: the adoption once, the egress host by host. Doing
# them as commands shows each decision once and doesn't depend on how many hosts the scaffold
# recommends (`--yes` is the bulk form of the same per-host consent).
Type "clear"
Enter
Wait
Type "fy config diff"
Enter
Wait
Sleep 5s
Type "fy config adopt"
Enter
Wait
Sleep 1s
Type "clear"
Enter
Wait
Type "fy allow sync --yes"
Enter
Wait
Sleep 4s

# ── 3. bring the box up ─────────────────────────────────────────────────────────────────────────
Type "clear"
Enter
Wait
Type "fy box up"
Enter
Sleep 3s

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

# ── 4. in the box: verify the boundary ──────────────────────────────────────────────────────────
Type "fy box shell"
Enter
Wait
Sleep 1s
Type "fy verify"
Enter
Wait@10m
Sleep 4s

# ── 5. the box edits the config, as an agent in it would ────────────────────────────────────────
Type "clear"
Enter
Wait
Type "# the box can write foldyard.toml, e.g. to ask for ripgrep:"
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

# ── 6. …but your computer doesn't run it until you say so ───────────────────────────────────────
# `down` first: on a RUNNING box `fy box up` only warns that its image is stale and returns (the
# packages layer is create-time), so the recreate is what puts the adopted change in the box.
Type "clear"
Enter
Wait
Type "fy box down && fy box up"
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
