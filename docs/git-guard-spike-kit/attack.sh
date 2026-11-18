#!/bin/sh
# Every way tried to change what your git runs. One line each: LANDED (bad) or refused (good).
# Each attempt that lands is undone at once, so the next starts from the same state.
S=$1; R=$S/repo; W=$S/wt
G="git -c safe.directory=*"
try() { name=$1; shift; if out=$("$@" 2>&1); then echo "LANDED   $name"; else echo "refused  $name :: $(echo "$out" | tail -1)"; fi; }
try "append to .git/config"          sh -c "cp $R/.git/config /tmp/c && echo '#x' >> $R/.git/config && cp /tmp/c $R/.git/config"
try "write .git/hooks/pre-commit"    sh -c "echo x > $R/.git/hooks/pre-commit && rm $R/.git/hooks/pre-commit"
try "git config (lock, rename over)" sh -c "$G -C $R config core.hooksPath /tmp/evil && $G -C $R config --unset core.hooksPath"
try "rename .git aside"              sh -c "mv $R/.git $R/.git-aside && mv $R/.git-aside $R/.git"
try "rename the worktree aside"      sh -c "mv $W $S/wt-aside && mv $S/wt-aside $W"
try "rewrite the worktree's .git"    sh -c "cp $W/.git /tmp/w && echo 'gitdir: /evil' > $W/.git && cp /tmp/w $W/.git"
try "rewrite commondir"              sh -c "cp $R/.git/worktrees/wt/commondir /tmp/d && echo /evil > $R/.git/worktrees/wt/commondir && cp /tmp/d $R/.git/worktrees/wt/commondir"
command -v umount >/dev/null || exit 0
try "remount .git/config read-write" mount -o remount,bind,rw $R/.git/config
try "unmount .git/config"            umount $R/.git/config
try "unmount .git/hooks (lazily)"    umount -l $R/.git/hooks
mkdir -p /tmp/nr
try "non-recursive bind, then write" sh -c "mount --bind $S /tmp/nr && echo '#x' >> /tmp/nr/repo/.git/config"
try "new userns, unmount, write"     unshare -Urm sh -c "umount $R/.git/config && echo '#x' >> $R/.git/config"
