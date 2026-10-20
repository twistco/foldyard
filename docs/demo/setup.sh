#!/usr/bin/env bash
# The demo's off-camera half: a fresh, tiny git repo for `foldyard.tape` to run `foldyard init`
# in, and a throwaway foldyard state dir so every recording starts from "nothing adopted".
#
# Sourced by the tape (hidden), so it sets the shell up too — never run it in your own shell:
#
#   source docs/demo/setup.sh <demo-dir> <state-dir>
#
# Both dirs are WIPED. Each carries a marker file and is refused without it, so a typo can never
# point this at a real checkout. The VM is named after the demo dir (`acme-api`); it is kept
# between recordings (a re-run reuses it) — `limactl delete -f acme-api` when you're done.

demo_dir="${1:?usage: source setup.sh <demo-dir> <state-dir>}"
state_dir="${2:?usage: source setup.sh <demo-dir> <state-dir>}"
marker=".fy-demo"

for d in "$demo_dir" "$state_dir"; do
    if [ -e "$d" ] && [ ! -e "$d/$marker" ]; then
        echo "✗ $d exists and isn't a demo dir (no $marker) — refusing to wipe it" >&2
        return 1
    fi
    rm -rf "$d"
    mkdir -p "$d"
    touch "$d/$marker"
done

# The host keeps adoptions, the allow-store and the supervisor's state here; a fresh one per
# recording is what makes every recording start from "nothing adopted, nothing granted".
export FOLDYARD_STATE_DIR="$state_dir"

cd "$demo_dir" || return 1
cat > README.md <<'EOF'
# acme-api

A small service.
EOF
cat > main.py <<'EOF'
print("hello from acme-api")
EOF
printf '%s\n' "$marker" > .gitignore
git init -q
git add -A
git -c user.email=demo@foldyard -c user.name=demo commit -qm "initial commit"
# A real repo has an origin, and `fy verify` needs one: its push probe proves "no credential
# reaches origin" only from a dry-run push that origin refuses for want of one, and with no
# remote it reports the refusal UNPROVEN (a FAIL). Any repo works for that; this public one is
# what tests/test_box_e2e.py gives its copy for the same check.
git remote add origin https://github.com/twistco/foldyard.git

# A short prompt that says which side of the boundary you're on — the box's own prompt is the
# Debian default for root (`root@box:/path#`), so the two read differently on screen.
export PS1='\[\e[1;32m\]your-computer\[\e[0m\]:\[\e[1;34m\]\W\[\e[0m\]$ '
