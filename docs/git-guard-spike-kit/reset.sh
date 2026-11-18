#!/bin/sh
# On your computer: a fresh repo in $1 with a linked worktree, and an origin it pushed to.
set -eu
S=$1; cd "$S"; rm -rf repo wt origin.git
git init -q --bare origin.git
git init -q -b main repo; cd repo
git config user.email spike@example.invalid; git config user.name spike
echo hi > f; git add f; git commit -qm init
git remote add origin "$S/origin.git"; git push -q origin main
git worktree add -q "$S/wt" -b wtb
