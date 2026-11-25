#!/bin/sh
# As guest root: pin .git and the worktree's directory (a mount point can't be renamed), and bind
# the parts that make your git run something read-only over themselves. `guard.sh <S> config`
# re-binds .git/config alone. (The share is mounted `shared`, so each bind also shows a
# read-write twin under the pin in findmnt. Making the binds private after mounting them broke
# the guard in podman's namespace in this spike's second run, so they stay as they are here.)
set -eu
S=$1; R=$S/repo; W=$S/wt
ro() { mount --bind "$1" "$1"; mount -o remount,bind,ro "$1"; }
pin() { mount --bind "$1" "$1"; }
if [ "${2:-}" = config ]; then ro "$R/.git/config"; exit 0; fi
pin "$R/.git"
ro "$R/.git/hooks"
ro "$R/.git/config"
ro "$R/.git/worktrees/wt/commondir"
pin "$W"
ro "$W/.git"
