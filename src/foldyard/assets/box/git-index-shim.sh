#!/usr/bin/env bash
# foldyard git shim — per-invocation GIT_INDEX_FILE split. Installed to /usr/local/bin/git by
# the box bootstrap (opt out: `[box] git_index_split = false`; per-call: FY_GIT_SHIM_OFF=1).
#
# Why: on two-kernel machine backends the checkout is shared with the host over virtiofs, and
# git's index lockfile protocol is not atomic across kernels — box-side git ops racing
# host-side status pollers can empty .git/index. So box-side git keeps its OWN index file
# (<gitdir>/index-box); objects and refs stay shared, commits are host-visible instantly.
# Resolved PER INVOCATION (honouring -C/--git-dir): a fixed path silently cross-writes another
# repo's index the moment a shell cds between checkouts (verified). Full design, divergence
# symptoms, and recovery: foldyard docs/adrs/0021-per-kernel-git-index-split.md.
#
# The split's own side effect — HEAD is shared, indexes aren't, so the other side committing
# leaves THIS index describing the old HEAD ("phantom staged deletions") and a blind commit
# would then silently REVERT that work — is self-healed here. Two sync files next to the index:
#   <gitdir>/index-box.head  the HEAD this index's state was last intentional at
#   <gitdir>/fy-box-head     the last HEAD a BOX-side git produced (stamped post-command below)
# fy-box-head is the side-attribution that keeps healing free of false positives: "index still
# matches the old HEAD after HEAD moved" is ALSO the signature of a deliberate `git reset
# --soft`, so we fast-forward only when the OTHER side moved HEAD (heal: FY_GIT_SHIM_NO_HEAL=1
# to disable; guarded `commit`: FY_GIT_SHIM_STALE_OK=1 to override).
#
# Known edge: git prepends its exec-path to hooks' PATH, so in-hook `git` bypasses this shim
# and inherits GIT_INDEX_FILE — correct for same-repo hook work; a hook driving git in a
# DIFFERENT repo must scrub git env itself (env -u GIT_INDEX_FILE), as with stock git's
# temp-index commits.
set -u

self=$(readlink -f "$0" 2>/dev/null || echo "$0")
real=
for c in $(type -ap git); do
    [ "$(readlink -f "$c" 2>/dev/null || echo "$c")" = "$self" ] && continue
    real=$c
    break
done
[ -n "$real" ] || {
    echo "foldyard git shim: no real git on PATH" >&2
    exit 127
}

# ── The under-lock check (ADR-0021's ref section) ─────────────────────────────────────────────
# Over the VM mount this kernel can read a ref the HOST just replaced as absent or old (see
# fy_refresh below), and git reads it AGAIN under its own ref lock only to compare it with what it
# read the first time — both stale, so a box commit moved a branch the host had moved moments
# before: onto an old commit, or as a root commit. Once git holds the lock the host can't move that
# ref (its git needs the same lock file, created exclusively on the host's filesystem), so a
# REFRESHED read then is the truth: git's `reference-transaction` hook, in its `prepared` state,
# compares the two and aborts the transaction on a mismatch — nothing moved, the command fails,
# a retry sees the new state. Git 2.47 has no config-declared hooks, so the shim points
# `core.hooksPath` at a directory of symlinks to itself (one per hook name; installed next to the
# shim by `FY_GIT_SHIM_INSTALL_HOOKS=1 git`, which the box bootstrap runs), and every hook but
# ours is handed straight to the repo's own hooks directory — computed before the override.
FY_HOOK_NAMES="applypatch-msg pre-applypatch post-applypatch pre-commit pre-merge-commit
prepare-commit-msg commit-msg post-commit pre-rebase post-checkout post-merge pre-push
pre-receive update proc-receive post-receive post-update reference-transaction push-to-checkout
pre-auto-gc post-rewrite sendemail-validate fsmonitor-watchman p4-changelist
p4-prepare-changelist p4-post-changelist p4-pre-submit post-index-change"
fy_hooks_dir="${self%/*/*}/libexec/foldyard-git-hooks"
# A directory holding nothing but `git` → this shim: put first on a REPO hook's PATH, where git
# has put its own exec-path first — so lefthook's `git` gets the refresh too, and nothing else a
# hook runs resolves differently.
fy_bin_dir="${self%/*/*}/libexec/foldyard-git-bin"

if [ -n "${FY_GIT_SHIM_INSTALL_HOOKS:-}" ]; then
    mkdir -p "$fy_hooks_dir" "$fy_bin_dir" || exit 1
    for h in $FY_HOOK_NAMES; do ln -sfn "$self" "$fy_hooks_dir/$h" || exit 1; done
    ln -sfn "$self" "$fy_bin_dir/git" || exit 1
    exit 0
fi

fy_zero() { [[ $1 =~ ^0+$ ]]; }

fy_common() { # $1 = the absolute git dir → its common dir (a linked worktree's shared half)
    local common
    common=$(cat "$1/commondir" 2>/dev/null) || common=$1
    case $common in /*) ;; *) common="$1/$common" ;; esac
    printf '%s' "$common"
}

# Over virtiofs this kernel caches a name's lookup, and host git writes HEAD, refs, packed-refs
# and config by lock→rename: after a HOST replace the name still resolves to the replaced file —
# absent — until the entry times out (~5 s on podman machine's libkrun, up to ~1 s on Lima;
# measured, ADR-0021). Git reads an absent loose ref as "fall back to packed-refs, else unborn",
# so in that window a box `git commit` parented on an old commit or made a ROOT commit, moved the
# branch there — its under-lock check reads the same stale name — and left every host commit
# since reachable only from the reflog. A create-style lookup always asks the server again, and
# link(2) does that lookup on its target before anything else can fail: `ln -dT / <name>`
# refreshes the entry, then fails EEXIST (or EXDEV, or EPERM — a directory is never linked), and
# creates nothing. What remains is the host replacing a name DURING the command: milliseconds.
fy_refresh() { # $1 = the absolute git dir
    local common head
    common=$(fy_common "$1")
    fy_relookup "$1/HEAD" "$common/packed-refs" "$common/config"
    head=$(cat "$1/HEAD" 2>/dev/null) || head=
    case $head in "ref: refs/"*) fy_relookup "$common/${head#ref: }" ;; esac
}
fy_relookup() { local n; for n; do ln -dT / "$n" 2>/dev/null || true; done; }

fy_ref_file() { # $1 ref, $2 git dir, $3 common dir → where its loose file lives
    case $1 in
    refs/bisect/* | refs/worktree/* | refs/rewritten/* | *HEAD) printf '%s/%s' "$2" "$1" ;;
    *) printf '%s/%s' "$3" "$1" ;;
    esac
}

# A `pull --rebase --autostash` that stops before its rebase begins (the `reset --hard` after
# `stash create` failed: a lock held, a file it couldn't replace) leaves its state dir holding
# ONLY `autostash` — the stashed changes' commit. No rebase to finish, `rebase --abort` can't
# clear it (no head-name), and every later pull refuses the directory: waiting never ends it. A
# rebase in its first moments has the same shape (the autostash is its first file), so it is
# still refused, only named. Asked only once an operation state was found: the common path pays
# nothing.
fy_leftover_autostash() { # $1 = an operation's state path → true when `autostash` is all it holds
    local f
    for f in "$1"/*; do # an empty dir's glob stays literal: no match, so false
        [ "$f" = "$1/autostash" ] || return 1
    done
}
fy_said_leftover() { # $1 = the refusal's lead, $2 = the state path → said, when it's the leftover
    fy_leftover_autostash "$2" || return 1
    echo "foldyard git shim: $1 — your computer has a leftover ${2##*/}: the changes a 'git pull" \
        "--rebase' set aside (its autostash), left when it stopped before it began — not a rebase" \
        "in progress, and it won't clear by itself. Recover it on your computer, not in this box:" \
        "'fy doctor' there shows how (ADR-0021). Nothing was changed; run this again after." >&2
}

# `prepared` input: `<old> <new> <ref>` per line. A zero <old> is "no expected value" as often as
# "must not exist" (branch -f, tag, a symref's log-only line), so only a real <old> is checked
# against the refreshed truth — except the checked-out branch's move by a box `git commit`, whose
# expected parent the shim itself verified (FY_HEAL_TOKEN: the ref and the HEAD this index was
# healed for). That catches the ROOT commit (git read the branch as unborn) and the commit whose
# parent moved after the heal (it would carry the old index: a silent revert). One-shot, so a
# later transaction in a child of this git (auto-gc's pack-refs) is judged on its own.
fy_verify_transaction() {
    local gd common old new ref actual tref= tcur= sref scur
    if [ "$(pwd -P)" = "${FY_ORIG_TOP:-}" ]; then
        gd=$FY_ORIG_GITDIR common=$FY_ORIG_COMMON
    else
        gd=$("$real" rev-parse --absolute-git-dir 2>/dev/null) || return 0
        common=$(fy_common "$gd")
    fi
    [ -n "${FY_HEAL_TOKEN:-}" ] && { read -r tref tcur <"$FY_HEAL_TOKEN"; } 2>/dev/null
    while read -r old new ref; do
        # old = new is NOT a no-op to skip: git sends the value it READ as both when it rewrites a
        # ref in place (`git reset` to HEAD) — read through a stale view, writing it is a rewind.
        fy_zero "$old" && fy_zero "$new" && continue
        case "$old $new" in *ref:*) continue ;; esac
        if [ -n "$tref" ] && [ "$ref" = "$tref" ]; then
            rm -f "$FY_HEAL_TOKEN"
            tref=
            if { [ -z "$tcur" ] && ! fy_zero "$old"; } || { [ -n "$tcur" ] && [ "$old" != "$tcur" ]; }; then
                echo "foldyard git shim: not moving $ref — this commit was prepared on" \
                    "${tcur:-an unborn branch}, but git read its parent as $old: your computer" \
                    "moved the branch while it ran (ADR-0021). Nothing was changed; run it again." >&2
                return 1
            fi
        fi
        # A move of the CHECKED-OUT branch is held to the HEAD this command started from — the one
        # the shim resolved before git ran (FY_HEAD_STATE), advanced by the command's own committed
        # moves — whatever git sends as <old>. Git reads that expected value when it gets there,
        # not when it read its target: `git reset` resolves HEAD, rewrites the index (seconds on a
        # loaded VM), and only then reads the value to replace — a host commit in between is that
        # value, git's own under-lock check passes, and the branch goes back to the target (7 host
        # commits rewound). A zero <old> says nothing about git's view at all: then the target
        # must also be that HEAD or ahead of it, since a stale read of HEAD yields an older value
        # (`checkout -B`, `update-ref <branch> HEAD` take their target from that read).
        sref= scur=
        [ -n "${FY_HEAD_STATE:-}" ] && [ "$(pwd -P)" = "${FY_ORIG_TOP:-}" ] &&
            { read -r sref scur <"$FY_HEAD_STATE"; } 2>/dev/null
        [ "$ref" = "$sref" ] || sref=
        fy_zero "$old" && [ -z "$sref" ] && continue # not ours to judge: branch -f, tag, …
        # A host operation that started after the shim's own check (a `pull --rebase`): moving the
        # branch under it makes the host's rebase fail to finish. Asked again here, under the lock.
        if [ -n "$sref" ] && [ ! -e "$gd/fy-box-op" ]; then
            for op in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD; do
                [ -e "$gd/$op" ] && fy_relookup "$gd/$op" && [ -e "$gd/$op" ] || continue
                fy_said_leftover "not moving $ref" "$gd/$op" && return 1
                echo "foldyard git shim: not moving $ref — your computer is in the middle of a git" \
                    "operation ($op) (ADR-0021). Nothing was changed; run it again once it's done." >&2
                return 1
            done
        fi
        # The forced lookup keeps a host rewrite of the SAME value from refusing a move for nothing.
        ln -dT / "$(fy_ref_file "$ref" "$gd" "$common")" 2>/dev/null
        ln -dT / "$common/packed-refs" 2>/dev/null
        actual=$("$real" rev-parse -q --verify --end-of-options "$ref" 2>/dev/null) || actual=
        if [ -n "$sref" ] && [ "$actual" != "$scur" ]; then
            echo "foldyard git shim: not moving $ref — this ran on ${scur:-an unborn branch}," \
                "but it is ${actual:-absent} now: your computer moved it while this ran" \
                "(ADR-0021). Nothing was changed; run it again." >&2
            return 1
        fi
        # No expected value, and a target that isn't this HEAD or ahead of it: git computed it off
        # a stale read of HEAD, or it is a deliberate move back (`checkout -B <branch> <older>`) —
        # indistinguishable here, so refused; `git reset` sends an expected value and gets through.
        if [ -n "$sref" ] && fy_zero "$old" && [ -n "$scur" ] && [ "$new" != "$scur" ] &&
            ! "$real" merge-base --is-ancestor "$scur" "$new" 2>/dev/null; then
            echo "foldyard git shim: not moving $ref back from $scur to $new — git gave no value to" \
                "check that against. If you meant to move it there, use 'git reset --hard $new'" \
                "(or --soft/--mixed); if not, your computer just moved it: run it again" \
                "(ADR-0021). Nothing was changed." >&2
            return 1
        fi
        fy_zero "$old" && continue
        if [ "$actual" != "$old" ]; then
            echo "foldyard git shim: not moving $ref — git read it as $old, but it is" \
                "${actual:-absent}: your computer moved it while this ran (ADR-0021). Nothing was" \
                "changed; run it again." >&2
            return 1
        fi
    done
    return 0
}

fy_orig_hooks() { # the repo's own hooks directory, as git would have used it without us
    if [ "$(pwd -P)" = "${FY_ORIG_TOP:-}" ]; then
        printf '%s' "$FY_ORIG_HOOKS"
    else # a hook's git in ANOTHER repo inherited our override (GIT_CONFIG_PARAMETERS): drop it
        env -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT \
            "$real" rev-parse --path-format=absolute --git-path hooks 2>/dev/null
    fi
}

# Run the real git with the caller's own discovery flags, so heal probes resolve the same repo
# (and, once exported, the same injected index) as the command itself.
pregit() { "$real" ${pre[@]+"${pre[@]}"} "$@"; }

# The `update-index --index-info` payload that re-stages the shared index's staged-vs-$1 work on
# top of $2's tree (index file $3), on stdout — or status 1 when a staged path was ALSO changed by
# the move (both sides touched it: never merged silently), status 2 when git couldn't say. Adds,
# modifications and staged deletes. Two git calls whatever the number of paths: the sealed heal
# runs under git's ref lock, where a call per path kept every host commit out for ~28 s (19 paths,
# a loaded VM).
fy_carry() {
    local rec=$1 head=$2 work=$3 out="$3.fyc" meta p i j=0 paths=() imode=() isha=() rsha=() npath=() nsha=()
    { GIT_INDEX_FILE="$work" pregit diff-index --cached --no-renames -z "$rec" >"$out"; } 2>/dev/null || {
        rm -f "$out"
        return 2
    }
    while IFS= read -r -d '' meta && IFS= read -r -d '' p; do
        set -- ${meta#:} # rec's mode, the index's mode, rec's oid, the index's oid, status
        [ "$5" = U ] && { rm -f "$out"; return 1; } # a conflict isn't staged work to carry
        paths+=("$p") imode+=("$2") rsha+=("$3") isha+=("$4")
    done <"$out"
    ((${#paths[@]})) || { rm -f "$out"; return 0; }
    # The same paths against the new HEAD, whose oid comes first (zeros: absent there). One that
    # reads the same in the index and the new HEAD isn't listed: already absorbed into the move.
    { (   # exactly these names: as globs, `a*` would re-stage `ab`'s old entry. Literal clashes
        # with any other global pathspec setting, and the caller's env has its own.
        unset GIT_GLOB_PATHSPECS GIT_NOGLOB_PATHSPECS GIT_ICASE_PATHSPECS
        GIT_LITERAL_PATHSPECS=1 GIT_INDEX_FILE="$work" pregit diff-index --cached --no-renames -z \
            "$head" -- "${paths[@]}"
    ) >"$out"; } 2>/dev/null || { rm -f "$out"; return 2; }
    while IFS= read -r -d '' meta && IFS= read -r -d '' p; do
        set -- ${meta#:}
        npath+=("$p") nsha+=("$3")
    done <"$out"
    rm -f "$out"
    # Both walk the same index in the same order, so the second list is a subsequence of the
    # first: pair them in step. An entry left unpaired means they didn't line up — never guess.
    for ((i = 0; i < ${#paths[@]}; i++)); do
        [ "$j" -lt "${#npath[@]}" ] && [ "${npath[j]}" = "${paths[i]}" ] || continue # absorbed
        [ "${rsha[i]}" = "${nsha[j]}" ] || return 1                                     # the move changed it too
        printf '%s %s\t%s\0' "${imode[i]}" "${isha[i]}" "${paths[i]}" # a staged deletion: mode 0
        j=$((j + 1))
    done
    [ "$j" -eq "${#npath[@]}" ]
}

# ── the shared index's heal, sealed to the box's own ref move ──────────────────────────────────
# After a box commit the host's .git/index still describes the old HEAD until it is healed, and a
# HOST commit in that gap recorded the box's files as deleted (libkrun, an IDE poller: 3 in 46,
# each within a second of the box's commit). So when this command's transaction moves the checked-
# out branch to N and the host's index is purely stale (exactly its sync point: nothing staged
# there), the healed index is built at `prepared` — N's tree, stat data kept — and moved INTO
# index.lock, taken exclusively: `committed` renames it over the index. A host commit needs that
# same lock, so it can't land between the branch move and the heal. Anything else (host staging to
# carry, a busy lock, an operation in progress) falls back to the post-command install below.
# Are two index files byte-identical? Answered from size + git's trailing checksum (a hash over the
# whole file) — 32 bytes read instead of two whole indexes, while a lock is held. An all-zero
# trailer (`index.skipHash`) can't answer, so then the files are compared outright.
fy_file_sig() {
    local sz tr
    sz=$(stat -c '%s' "$1" 2>/dev/null || stat -f '%z' "$1" 2>/dev/null) || return 1
    tr=$(tail -c 32 "$1" 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')
    case $tr in *[1-9a-f]*) printf '%s:%s' "$sz" "$tr" ;; *) return 1 ;; esac
}
fy_same_bytes() { # $1, $2 = index files
    local a b
    if a=$(fy_file_sig "$1") && b=$(fy_file_sig "$2"); then [ "$a" = "$b" ]; else cmp -s "$1" "$2"; fi
}

fy_tx_git() { # $1 index file, then git args — the real git, none of our overrides
    local f=$1
    shift
    env -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT GIT_INDEX_FILE="$f" "$real" "$@" >/dev/null 2>&1
}

# The healed index for $3 (N) in $4, from a copy of the shared index kept in $4.base: pure staleness
# → N's tree; staged host work the move didn't touch → carried (fy_carry, the rule both heals share);
# an overlap → status 1 (the post-command path reports it).
fy_tx_build() { # $1 git dir, $2 sync point, $3 N, $4 work file
    rm -f "$4.carry"
    cp "$1/index" "$4.base" 2>/dev/null && cp "$4.base" "$4" 2>/dev/null &&
        fy_carry "$2" "$3" "$4" >"$4.carry" &&
        fy_tx_git "$4" read-tree --reset "$3" &&
        { [ ! -s "$4.carry" ] || { GIT_INDEX_FILE="$4" pregit update-index -z --index-info <"$4.carry"; } >/dev/null 2>&1; }
}

fy_tx_prepare() { # stdin: the transaction's lines
    local gd sref scur old new ref target= op srec work tries=20
    [ -n "${FY_TX:-}" ] && [ "$(pwd -P)" = "${FY_ORIG_TOP:-}" ] || return 0
    gd=$FY_ORIG_GITDIR
    { read -r sref scur <"$FY_HEAD_STATE"; } 2>/dev/null || return 0
    while read -r old new ref; do
        case $ref in HEAD | "$sref") ;; *) continue ;; esac
        case $new in ref:*) continue ;; esac
        fy_zero "$new" || target=$new
    done
    [ -n "$target" ] || return 0
    for op in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do
        [ -e "$gd/$op" ] && return 0
    done
    fy_relookup "$gd/index.fy-head" # the host's heal tick rewrites it: a stale read says "none"
    srec=$(cat "$gd/index.fy-head" 2>/dev/null) || return 0
    work="$gd/.fy-tx.$$"
    fy_relookup "$gd/index"
    fy_tx_build "$gd" "$srec" "$target" "$work" || {
        rm -f "$work" "$work.base" "$work.carry"
        return 0
    }
    # A host git holds the lock for moments at a time: wait for it (~1 s) rather than fall back to
    # the post-command install, which a host commit can still beat.
    until (set -o noclobber && : >"$gd/index.lock") 2>/dev/null; do
        ((--tries)) || {
            rm -f "$work" "$work.base" "$work.carry"
            return 0
        }
        sleep 0.05
    done
    fy_relookup "$gd/index" # under the lock nothing replaces it: this read is the truth
    # Changed since the copy (a host add, an IDE's stat refresh): rebuild from it now, holding the
    # lock, rather than fall back.
    if { fy_same_bytes "$gd/index" "$work.base" || fy_tx_build "$gd" "$srec" "$target" "$work"; } &&
        mv -f "$work" "$gd/index.lock" 2>/dev/null; then
        printf 'held %s\n' "$target" >"$FY_TX"
    else
        rm -f "$gd/index.lock"
    fi
    rm -f "$work" "$work.base" "$work.carry"
}

fy_tx_finish() { # $1 = committed | aborted
    local st target gd=$FY_ORIG_GITDIR
    [ -n "${FY_TX:-}" ] && { read -r st target <"$FY_TX"; } 2>/dev/null && [ "$st" = held ] || return 0
    if [ "$1" = committed ] && mv -f "$gd/index.lock" "$gd/index" 2>/dev/null; then
        { printf '%s\n' "$target" >"$gd/index.fy-head.$$" && mv -f "$gd/index.fy-head.$$" "$gd/index.fy-head"; } 2>/dev/null
        printf 'done %s\n' "$target" >"$FY_TX"
    else
        rm -f "$gd/index.lock"
        printf 'released\n' >"$FY_TX"
    fi
}

# The repo has its own `$hook` to hand this to — never our own directory (that is "none": handing
# a hook to ourselves re-execs this script forever).
fy_repo_hook() { [ -n "$1" ] && ! [ "$1" -ef "$fy_hooks_dir" ] && [ -x "$1/$hook" ]; }

case $0 in
*/foldyard-git-hooks/*)
    hook=${0##*/} orig=$(fy_orig_hooks)
    fy_hook_path=$PATH
    [ -x "$fy_bin_dir/git" ] && fy_hook_path="$fy_bin_dir:$PATH"
    if [ "$hook" = reference-transaction ]; then
        input=$(cat; echo .)
        input=${input%.}
        if [ "${1:-}" = prepared ] && ! printf '%s' "$input" | fy_verify_transaction; then
            exit 1
        fi
        case ${1:-} in
        prepared) printf '%s' "$input" | fy_tx_prepare ;;
        committed | aborted) fy_tx_finish "$1" ;;
        esac
        # This command's own committed move of HEAD / the checked-out branch, for the post-command
        # attribution below (a repo hook's git never gets FY_MOVED: it is stripped for them).
        # The branch's move also advances the HEAD its next move is held to (a multi-commit
        # cherry-pick, a `rebase --continue` from a detached HEAD).
        if [ "${1:-}" = committed ] && [ -n "${FY_MOVED:-}" ] && [ "$(pwd -P)" = "${FY_ORIG_TOP:-}" ]; then
            { read -r sref _ <"$FY_HEAD_STATE"; } 2>/dev/null || sref=
            while read -r old new ref; do
                case $ref in HEAD | "$sref") printf '%s\n' "$new" >"$FY_MOVED" ;; esac
                [ "$ref" = "$sref" ] && ! fy_zero "$new" && printf '%s %s\n' "$sref" "$new" >"$FY_HEAD_STATE"
            done <<<"$input"
        fi
        fy_repo_hook "$orig" || exit 0
        printf '%s' "$input" | env -u FY_HEAL_TOKEN -u FY_HEAD_STATE -u FY_MOVED -u FY_TX PATH="$fy_hook_path" "$orig/$hook" "$@"
        exit
    fi
    fy_repo_hook "$orig" || exit 0
    exec env -u FY_HEAL_TOKEN -u FY_HEAD_STATE -u FY_MOVED -u FY_TX PATH="$fy_hook_path" "$orig/$hook" "$@"
    ;;
esac

[ -n "${FY_GIT_SHIM_OFF:-}" ] && exec "$real" "$@"

# Subcommands that never move a ref: they run without our hooks override (fy_arm_hooks).
FY_NO_REF_SUBS="rev-parse rev-list for-each-ref show-ref cat-file ls-tree merge-base name-rev var
log shortlog count-objects verify-pack hash-object ls-remote config status diff show grep ls-files
describe blame"
FY_NO_REF_SUBS=${FY_NO_REF_SUBS//$'\n'/ }

# …and without the one git hands down from an outer box command: inside a repo hook, git passes our
# `-c core.hooksPath` on (GIT_CONFIG_PARAMETERS), so lefthook's auto-sync asked `rev-parse
# --git-path hooks`, got OUR directory and wrote its hook scripts into it — through the symlinks
# over this shim when writable (the commit's own checks then silently skipped every file), "could
# not replace the hook: permission denied" on every commit when root-owned. Only our exact entry
# goes; a ref-moving git keeps it, so its moves still reach the check.
fy_hide_override() {
    local own="'core.hooksPath'='$fy_hooks_dir'" p a
    p=${GIT_CONFIG_PARAMETERS-}
    case $p in *"$own"*) ;; *) return 0 ;; esac
    while [ $# -gt 0 ]; do
        case $1 in
        -C | -c | --git-dir | --work-tree | --namespace | --super-prefix | --config-env | --attr-source) shift 2 || return 0 ;;
        -*) shift ;;
        *) break ;;
        esac
    done
    case " $FY_NO_REF_SUBS " in *" ${1:-} "*) ;; *) return 0 ;; esac
    p=${p//"$own"/}
    p=${p#"${p%%[! ]*}"} p=${p%"${p##*[! ]}"}
    if [ -n "$p" ]; then export GIT_CONFIG_PARAMETERS="$p"; else unset GIT_CONFIG_PARAMETERS; fi
}
fy_hide_override "$@"

# Respect an explicit GIT_INDEX_FILE — git's own temp-index protocols (stash, commit -a hooks)
# and deliberate callers. If it is OUR injection inherited through a subprocess (marker
# matches), fall through and re-resolve, so a child git running in a DIFFERENT repo never
# reuses the parent repo's index file.
if [ -n "${GIT_INDEX_FILE:-}" ] && [ "$GIT_INDEX_FILE" != "${FY_GIT_SHIM_INDEX:-}" ]; then
    # …inside our own command's hooks (FY_ORIG_GITDIR), refreshed first: a repo hook's git would
    # otherwise read a branch the host just moved as unborn (every file "staged").
    [ -n "${FY_ORIG_GITDIR:-}" ] && fy_refresh "$FY_ORIG_GITDIR"
    exec "$real" "$@"
fi

# Inside our own command's hooks (FY_ORIG_TOP), a git in the SAME repo — lefthook's two dozen calls
# per commit — needs only the refresh: the outer command has healed and armed (and the ref check
# reaches its nested moves through the override git hands down). The whole path per call made a
# lefthook commit ~2 s slower. A nearer .git (a nested repo, a submodule) or an explicit
# -C/--git-dir/--work-tree takes the whole path: it may be another repo, with its own index.
fy_in_outer_repo() {
    local a d
    [ -n "${FY_ORIG_TOP:-}" ] && [ -n "${FY_ORIG_GITDIR:-}" ] || return 1
    for a; do
        case $a in -C | --git-dir* | --work-tree* | --namespace*) return 1 ;; -*) ;; *) break ;; esac
    done
    d=$(pwd -P)
    case "$d/" in "$FY_ORIG_TOP"/*) ;; *) return 1 ;; esac
    while [ "$d" != "$FY_ORIG_TOP" ]; do
        [ -e "$d/.git" ] && return 1
        d=${d%/*}
    done
}
if fy_in_outer_repo "$@"; then
    fy_refresh "$FY_ORIG_GITDIR"
    exec "$real" "$@"
fi
unset GIT_INDEX_FILE

args=("$@")
# Collect the global options that affect repo discovery, up to the subcommand.
pre=() sub=
i=0
n=${#args[@]}
while ((i < n)); do
    a=${args[i]}
    case "$a" in
    -C | -c | --git-dir | --work-tree | --namespace | --super-prefix | --config-env | --attr-source)
        pre+=("$a")
        ((++i < n)) && pre+=("${args[i]}")
        ;;
    --git-dir=* | --work-tree=* | --namespace=* | --super-prefix=* | --config-env=* | --attr-source=* | --bare)
        pre+=("$a")
        ;;
    -*) ;; # other global flags (pager etc.) — discovery-neutral, keep out of rev-parse
    *)
        sub=$a
        break
        ;;
    esac
    ((i++))
done
subix=$i

case "$sub" in
# Repo-creating commands write the NEW repo's checkout through an inherited GIT_INDEX_FILE
# (verified: clone hijacks it, leaving the fresh clone looking broken) — never inject here.
# `worktree` is the same class, and worse: `add` checks the new tree out in a child git that
# inherits the variable, so the new worktree's index landed in THIS checkout's index-box —
# phantom `MM` here (a commit reverting whatever the two commits differ in; a later `switch`
# carrying those files over as "local edits"), and no index at all over there. `remove` runs
# its cleanliness check the same way. No `worktree` subcommand reads this checkout's index.
"" | clone | init | worktree) exec "$real" "${args[@]}" ;;
esac

# Subcommands that never READ the index, so a stale one cannot affect their output and healing
# them is pure cost. This matters because they are the high-frequency callers: foldyard's own
# probes are rev-parse / rev-list / for-each-ref, and the TUI runs them on a 1s timer, on the
# event loop — where the heal's latency stalled the entire UI.
#
# STRICTLY an allowlist, never a denylist: a denylist fails OPEN, so the day git grows a
# subcommand (or we forget one) an index-mutating command would silently skip healing and could
# commit a revert of the other side's work — the exact failure ADR-0021 exists to prevent.
# Anything touching the index (status, diff, add, commit, stash, checkout/switch/restore, reset,
# rm, mv, merge, rebase, cherry-pick, apply, am, clean, worktree, grep, ls-files, submodule) is
# absent on purpose. Add only after checking git's docs — when unsure, leave it out; the cost of
# a missing entry is latency, the cost of a wrong one is data loss.
#
# `describe` is absent for that reason: plain `describe` is index-free, but `--dirty`/`--broken`
# run a diff-index, so a stale index would append a phantom `-dirty` to the version string of
# whatever consumes it. Nothing in foldyard calls describe (it is off the hot path the list
# exists for), so the whole verb stays out rather than buying latency with a flag-sniffing
# special case.
FY_NO_INDEX_SUBS="rev-parse rev-list for-each-ref show-ref symbolic-ref cat-file ls-tree
merge-base name-rev var config remote fetch ls-remote log reflog shortlog branch tag
count-objects verify-pack hash-object"
# Fold the wrapped lines to single spaces — the lookup is a `case` on " $list " and a newline
# would leave the words either side of it unmatchable (a silent, subcommand-specific miss).
FY_NO_INDEX_SUBS=${FY_NO_INDEX_SUBS//$'\n'/ }

# Side files are best effort, and over the VM mount a create can fail where the name reads absent
# (a stale entry for a name the host just unlinked). `{ …; } 2>/dev/null`, never `>f 2>/dev/null`:
# redirections apply left to right, so a failed `>f` reports before the trailing one takes effect.
fy_record() { { printf '%s\n' "$1" >"$ix.head"; } 2>/dev/null || true; }

# Index IDENTITY — "is this the same index I last judged?", for the stale memo below.
# Deliberately NOT mtime: `git status` rewrites the index to refresh its stat cache, moving mtime
# while the entries are untouched (measured: size 281→281, mtime moved), so an mtime-keyed memo
# would invalidate on exactly the command it exists to speed up.
#
# Size alone is NOT enough, though it reads like it should be. An index entry is fixed-width plus
# the PATH, so a content-only change (restaging the same path) leaves the byte count identical;
# only the first `git add` after a write moves it, by invalidating cache-tree. Measured on a
# 3.6k-file checkout: three different staged contents of one file, all 564418 bytes. So a stale
# index that becomes HEALABLE without changing size stayed memoized as unfixable — phantom `MM`
# in status and `commit` blocked, until something unrelated moved the size (reproduced in
# test_git_shim.py::test_the_memo_is_keyed_on_index_CONTENT_not_just_size).
#
# The trailer fixes it for a 20-byte read rather than a re-hash: git ends every index with a
# checksum over the whole file. Take the last 32 bytes (covers sha256 repos too) — hex, because
# the value is binary. Size stays in the key as belt-and-braces for the two ways the trailer can
# go constant: `index.skipHash=true` (git ≥2.40) writes zeros there, and a box without `od`
# degrades to the empty string. Both then behave exactly as this did before.
fy_index_sig() {
    local sz tr
    sz=$(stat -c '%s' "$ix" 2>/dev/null || stat -f '%z' "$ix" 2>/dev/null) || sz=
    tr=$(tail -c 32 "$ix" 2>/dev/null | od -An -tx1 2>/dev/null | tr -d ' \n')
    printf '%s:%s' "${sz:-?}" "${tr:-?}"
}

# Does the index's tree exactly match a commit this checkout's HEAD has ACTUALLY BEEN AT? That's
# pure staleness with a lost / multi-hop sync point (e.g. this index's own commit already absorbed
# into a newer HEAD, or a pre-upgrade index-box with no .head yet) — never a state anyone
# deliberately staged.
#
# The PROVENANCE half of that question is load-bearing, and it is why this walks HEAD's reflog
# rather than `rev-list --all`. "This tree is SOME commit somewhere in the repo" is not evidence of
# staleness: deliberately staging content that reproduces an existing commit's tree is ordinary
# work (re-applying a colleague's patch, `git checkout <branch> -- .`, backing a WIP change out),
# and an unrelated feature branch's commit made every such index look disposable — so the moment
# the other side moved HEAD, the "nothing of the user's to lose" reset below threw that staging
# away. HEAD's reflog is precisely the set of states this index could have been SYNCED to, and
# since both sides share .git it records the host's checkouts, commits and resets too. Requiring
# the match to come from there keeps every heal these tests pin, and hands anything else to the
# stale-index refusal, which is recoverable (reset + re-stage) where a wrong reset is not.
#
# Compares TREE HASHES rather than running diff-index per candidate, and does NOT walk HEAD's
# ancestors — that part was a correctness fix, not a speedup: the common way this index goes stale
# is the OTHER side checking out a DIFFERENT BRANCH, whose commits are not ancestors of our HEAD,
# so the old ancestor walk was looking down the wrong line of history and could never match. It
# then declared "genuinely staged work" over a byte-exact snapshot of a real commit, permanently
# (see below: that verdict caches nothing, so every git call re-paid the full probe — measured at
# 3.1s per invocation on a 3.6k-file checkout over virtiofs, which starved the TUI's 1s refresh
# timer). A branch switch IS a HEAD move, so the reflog carries it.
#
# Cost is 2 git calls, and the reflog walk is UNBOUNDED (`-g HEAD` measured at 0.29s on a checkout
# with 842 reflog entries, against 0.38s for the old --all walk; the reflog's own gc expiry is the
# only cut-off). Two bounds that look tempting are not: `--max-count=200` (once carried as a speed
# guard) keeps the most RECENT entries, so an index snapshotting a state left alone for a couple of
# hundred HEAD moves is declared unfixable — commits blocked — for no gain; and `-g --all` widens
# provenance to every branch's reflog for 3.6s, because it re-reads every ref's log file over
# virtiofs. write-tree does WRITE tree objects into the ODB; they are unreferenced (gc'd like any
# other) and identical trees hash the same, so this is idempotent rather than growing. It fails on
# an unmerged index, which the mid-operation guard already excludes above.
fy_matches_a_past_head() { # [index file — default: this invocation's own]
    local t
    t=$(GIT_INDEX_FILE="${1:-$ix}" pregit write-tree 2>/dev/null) || return 1
    [ -n "$t" ] || return 1
    pregit rev-list -g HEAD --format='%T' 2>/dev/null | grep -qxF "$t"
}

# The box's own staged work carried onto the new HEAD when the move didn't touch it — the rule the
# shared index's heal (fy_carry, below) has always had: HEAD's tree plus exactly the staged paths
# (adds, modifications, staged deletions). Built on a copy and renamed in under git's own lock on
# index-box, so a concurrent box git never loses a write to it. Status 1 = a staged path the move
# also changed (never merged silently), or the lock was busy.
# Status 1 = an overlap (the verdict fy_heal remembers); 2 = not now (a busy lock, a failed git
# call) — tried again on the next call. The copy is taken under index-box.lock: taken after the
# build, another box git staging in between was overwritten by the older copy.
fy_carry_box() {
    local work="$ix.carry.$$" rc=2 crc=0
    (set -o noclobber && : >"$ix.lock") 2>/dev/null || return 2
    if cp "$ix" "$work" 2>/dev/null; then
        fy_carry "$rec" "$cur" "$work" >"$work.info" || crc=$?
        if ((crc == 1)); then
            rc=1
        elif ((crc == 0)) && GIT_INDEX_FILE="$work" pregit read-tree --reset "$cur" 2>/dev/null &&
            { [ ! -s "$work.info" ] || { GIT_INDEX_FILE="$work" pregit update-index -z --index-info <"$work.info"; } 2>/dev/null; } &&
            mv -f "$work" "$ix" 2>/dev/null; then
            rc=0
        fi
    fi
    rm -f "$ix.lock" "$work" "$work.info" 2>/dev/null
    ((rc == 0)) && fy_record "$cur"
    return "$rc"
}

# Self-heal a stale index-box before the command runs. Returns 1 (→ the commit guard) only for
# the one state it can't fix without guessing: the other side moved HEAD AND this index's staged
# work touches a path the move changed. Everything is conditional and non-destructive: staged
# state is only ever dropped when it provably equals a commit's tree (nothing of the user's to
# lose), and carried whole otherwise.
fy_heal() {
    [ -n "${FY_GIT_SHIM_NO_HEAL:-}" ] && return 0
    [ -z "$cur" ] && return 0 # unborn HEAD — nothing to be stale against
    rec=$(cat "$ix.head" 2>/dev/null) || rec=
    [ "$rec" = "$cur" ] && return 0
    # Never touch the index mid-operation — conflict stages / sequencer state live in it.
    local op
    for op in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do
        [ -e "$gitdir/$op" ] && return 0
    done
    # A stale sync point may predate a history rewrite — an unresolvable rec is no rec at all.
    [ -n "$rec" ] && { pregit rev-parse -q --verify "$rec^{commit}" >/dev/null 2>&1 || rec=; }
    # Side attribution: the last box-side git to move HEAD stamped it. If that stamp IS the
    # current HEAD, the move was ours (this or a sibling session) and the index state that came
    # with it is deliberate (a commit already matches; a soft reset deliberately doesn't).
    stamp=$(cat "$gitdir/fy-box-head" 2>/dev/null) || stamp=
    if [ -n "$stamp" ] && [ "$stamp" = "$cur" ]; then
        fy_record "$cur"
        return 0
    fi
    if pregit diff-index --cached --quiet "$cur" 2>/dev/null; then
        fy_record "$cur" # index already matches the new HEAD — just resync the marker
        return 0
    fi
    # The unfixable verdict is the EXPENSIVE one to reach and the one nothing else caches (the
    # branches above all record a sync point; this one deliberately must not, since there is no
    # reconciliation to claim). Uncached, every subsequent git call re-ran the whole probe for as
    # long as the state lasted. Memoize it on exactly its inputs — the two HEADs plus the index's
    # identity (fy_index_sig) — so a repeat call short-circuits to the message. Any staging, any
    # HEAD move, invalidates it; a wrong memo costs at most one command's delayed healing.
    # Convergence is one call behind, not immediate: `git status` may rewrite the index's cache-tree
    # extension once after each real change, which moves the identity and re-invalidates. So a
    # changed index costs two probes, then zero (verified — the rewrite is idempotent, so repeated
    # `status` on an unchanged index leaves the trailer byte-identical) — worth stating, because
    # "probes: 2, 2, 0, 0, 0" looks like a broken memo until you know why.
    local sig="" stale_sig=""
    sig="$rec $cur $(fy_index_sig)"
    stale_sig=$(cat "$ix.stale" 2>/dev/null) || stale_sig=
    if [ "$sig" != "$stale_sig" ]; then
        if { [ -n "$rec" ] && pregit diff-index --cached --quiet "$rec" 2>/dev/null; } ||
            fy_matches_a_past_head; then
            # Pure staleness — nothing genuinely staged: the new HEAD's tree, stat data kept for
            # every unchanged path (a one-way `read-tree --reset`: no worktree check, no -u). Never `git reset`, which also rewrites
            # the checked-out branch with the value it READ — stale over the VM mount, the rewind
            # the hook refuses, run here without it. On failure (lock contention) leave the marker
            # stale so the next invocation retries.
            rm -f "$ix.stale" 2>/dev/null || true
            pregit read-tree --reset "$cur" 2>/dev/null && fy_record "$cur"
            return 0
        fi
        local crc=1
        [ -n "$rec" ] && { fy_carry_box && crc=0 || crc=$?; }
        if ((crc == 0)); then
            rm -f "$ix.stale" 2>/dev/null || true
            return 0
        fi
        if ((crc == 2)); then # not now: nothing to remember, the next call tries again
            echo "foldyard git shim: index-box is stale — HEAD moved (${rec:0:12}… → ${cur:0:12}…)" \
                "and its staged work couldn't be carried forward just now (another git holds its" \
                "lock). Nothing was changed; run it again." >&2
            return 1
        fi
        if [ -z "$rec" ]; then
            # First sight of this index with real staged work and no sync point to diff against:
            # treat the staged state as intentional at the current HEAD.
            rm -f "$ix.stale" 2>/dev/null || true
            fy_record "$cur"
            return 0
        fi
        { printf '%s\n' "$sig" >"$ix.stale"; } 2>/dev/null || true
    fi
    echo "foldyard git shim: index-box is stale — HEAD moved (${rec:0:12}… → ${cur:0:12}…, from your" \
        "computer or another session) while it carries staged changes to a path that move changed," \
        "so it can't be carried forward." \
        "'git status' may show phantom staged diffs and commits are blocked meanwhile." \
        "Recover with: 'git checkout-index -a' (materialise anything staged-but-absent from the" \
        "worktree — a mixed reset would otherwise leave it only as an unreferenced blob), then" \
        "'git reset', then re-stage." >&2
    return 1
}

# ── the SHARED index's heal: the box builds it and installs it itself; the host is the fallback ──
# A box HEAD move leaves the host's .git/index describing the old HEAD (phantom staged diffs in
# every host-side git — and the host's next commit records the box's files as DELETED). Working
# out the healed index needs git, and git run in the checkout reads the checkout's config — which
# the box can write — so the HOST runs no git for this: the box builds the healed index here, from
# a COPY of the shared one, and installs it at once under git's own index.lock (fy_install_shared).
# Only when it can't (a host git holds the lock, or staged host work arrived meanwhile) does it
# offer the build as a new file next to the index, for the host (foldyard githeal.py) to install
# on its tick under the same checks. Waiting for that tick was the silent revert: an IDE's status
# poller rewrites the index every few seconds, the host dropped every offer built on a copy that
# was no longer byte-identical, and the box re-offered only on its next git call (measured: 4
# heals for 24 box commits, every host commit meanwhile deleting the box's files).
#
# Offers are always NEW names (index.fy-proposed.<head>.<base>.<ff|carry>, index.fy-record.<head>,
# index.fy-refused.<rec>.<head>.<base>), written in this kernel and renamed into place: a name the
# HOST replaces reads as missing from here for up to ~1 s over virtiofs (~5 s on podman machine's
# libkrun; measured, ADR-0021), so nothing here re-reads a file the host rewrites except
# index.fy-head — re-asked before each read (a stale "none" sent the sealed heal to the fallback). Decisions mirror the box's own heal above:
# already matching → record; pure staleness (the old sync point, or a past HEAD from the reflog) →
# the new HEAD's tree; staged host work disjoint from the move → carried forward; overlapping →
# refused, for the host to report.
fy_pending() { # anything for HEAD $1 already waiting for the host?
    compgen -G "$gitdir/index.fy-proposed.$1.*" >/dev/null ||
        [ -e "$gitdir/index.fy-record.$1" ] ||
        compgen -G "$gitdir/index.fy-refused.*.$1.*" >/dev/null
}

fy_offer_record() { { : >"$gitdir/index.fy-record.$1"; } 2>/dev/null || true; }

# Same index ENTRIES (path, mode, object id, stage) — the stat cache ignored: a status poller's
# rewrite changes only that, and must not make a heal wait for the host's tick.
fy_same_entries() { # $1, $2 = index files
    local a b
    a=$(set -o pipefail; GIT_INDEX_FILE="$1" pregit ls-files -s -z 2>/dev/null | cksum) || return 1
    b=$(set -o pipefail; GIT_INDEX_FILE="$2" pregit ls-files -s -z 2>/dev/null | cksum) || return 1
    [ "$a" = "$b" ]
}

# Install the built index $1 (for HEAD $2, built from the copy $3) as the shared index, under
# git's own index.lock — as githeal.py's _install does host-side. The lock file is created
# exclusively on the host's filesystem (and an exclusive create always re-asks for the name, so a
# stale entry here can't fake one), so while we hold it no git on either side can replace the
# index: a REFRESHED read now is the truth. HEAD is checked again after the rename (the lock guards
# the index, not refs), and the host's index put back if it moved. Status 1 = not installed.
fy_install_shared() {
    local lock="$gitdir/index.lock" keep rc=1
    (set -o noclobber && : >"$lock") 2>/dev/null || return 1 # a git holds it: the host's tick will
    keep="$1.kept"
    fy_relookup "$gitdir/index"
    fy_refresh "$gitdir"
    # The old index is kept by a hard link (one metadata op, not a copy over the mount) — enough to
    # put it back, since the rename below only moves the name.
    if [ "$(pregit rev-parse -q --verify HEAD 2>/dev/null)" = "$2" ] &&
        link "$gitdir/index" "$keep" 2>/dev/null &&
        { fy_same_bytes "$keep" "$3" || fy_same_entries "$keep" "$3"; } &&
        mv -f "$1" "$gitdir/index" 2>/dev/null; then
        rc=0
        fy_refresh "$gitdir"
        if [ "$(pregit rev-parse -q --verify HEAD 2>/dev/null)" != "$2" ]; then
            mv -f "$keep" "$gitdir/index" 2>/dev/null
            rc=1
        fi
    fi
    rm -f "$lock" "$keep" 2>/dev/null
    ((rc == 0)) && fy_mark_shared "$2"
    return "$rc"
}

fy_mark_shared() { # the shared index's sync point (githeal.py's SYNC_FILE), written as git would
    { printf '%s\n' "$1" >"$gitdir/index.fy-head.$$" && mv -f "$gitdir/index.fy-head.$$" "$gitdir/index.fy-head"; } 2>/dev/null ||
        rm -f "$gitdir/index.fy-head.$$" 2>/dev/null
}

fy_propose() { # $1 = the HEAD to heal the shared index to
    local head=$1 srec work op
    [ -n "${FY_GIT_SHIM_NO_HEAL:-}" ] || [ -z "$head" ] && return 0
    # A held index.lock is not in this list: a host git mid-write means "offer it for the host's
    # tick" (fy_install_shared finds the lock busy), not "wait for this box's next git call".
    for op in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG; do
        [ -e "$gitdir/$op" ] && return 0
    done
    fy_relookup "$gitdir/index.fy-head"
    srec=$(cat "$gitdir/index.fy-head" 2>/dev/null) || srec=
    [ "$srec" = "$head" ] && return 0
    # A record or refusal for this HEAD is the host's to act on; a leftover OFFER (an install that
    # found the lock busy) is rebuilt and installed now rather than left to the tick.
    { [ -e "$gitdir/index.fy-record.$head" ] || compgen -G "$gitdir/index.fy-refused.*.$head.*"; } >/dev/null && return 0
    fy_relookup "$gitdir/index"
    [ -f "$gitdir/index" ] || return 0 # absent: next call retries
    work=$(mktemp "$gitdir/.fy-propose.XXXXXX" 2>/dev/null) || return 0
    fy_propose_from "$head" "$srec" "$work"
    rm -f "$work" "$work.carry" "$work.base" 2>/dev/null || true # a build was renamed away; the rest goes
}

fy_propose_from() { # $1 head, $2 the shared index's sync point, $3 a temp file to build in
    local head=$1 srec=$2 work=$3 base kind
    cp "$gitdir/index" "$work" 2>/dev/null || return 0
    cp "$work" "$work.base" 2>/dev/null || return 0
    base=$(pregit hash-object --no-filters -- "$work" 2>/dev/null) || return 0
    [ -n "$srec" ] && { pregit rev-parse -q --verify "$srec^{commit}" >/dev/null 2>&1 || srec=; }
    if GIT_INDEX_FILE="$work" pregit diff-index --cached --quiet "$head" 2>/dev/null; then
        fy_offer_record "$head" # already matches the new HEAD — the host only moves its marker
        return 0
    fi
    if { [ -n "$srec" ] && GIT_INDEX_FILE="$work" pregit diff-index --cached --quiet "$srec" 2>/dev/null; } ||
        fy_matches_a_past_head "$work"; then
        kind=ff
    elif [ -n "$srec" ]; then
        local crc=0
        fy_carry "$srec" "$head" "$work" >"$work.carry" || crc=$?
        if ((crc == 1)); then
            { : >"$gitdir/index.fy-refused.$srec.$head.$base"; } 2>/dev/null || true
            return 0
        fi
        ((crc == 0)) || return 0 # git couldn't say: nothing offered, the next call proposes again
        kind=carry
    else
        fy_offer_record "$head" # staged work, no sync point to diff against — assume intentional
        return 0
    fi
    # read-tree leaves zero-stat entries: never racily clean, so the host's next status re-hashes
    # once (its kernel's stat data isn't ours to write anyway).
    GIT_INDEX_FILE="$work" pregit read-tree "$head" 2>/dev/null || return 0
    if [ -s "$work.carry" ]; then
        { GIT_INDEX_FILE="$work" pregit update-index -z --index-info <"$work.carry"; } 2>/dev/null || return 0
    fi
    if fy_install_shared "$work" "$head" "$work.base"; then
        rm -f "$gitdir/index.fy-proposed.$head".* 2>/dev/null
        return 0
    fi
    mv -f "$work" "$gitdir/index.fy-proposed.$head.$base.$kind" 2>/dev/null || true
}

# Before an index-touching command: offer the host what it's missing for the current HEAD. The box
# moved HEAD (its stamp) → propose (again, if the host dropped the last one); the host moved it →
# its own git set its index deliberately, so only the marker moves.
fy_shared_sync() {
    local srec stamp
    [ -n "${FY_GIT_SHIM_NO_HEAL:-}" ] || [ -z "$cur" ] && return 0
    fy_relookup "$gitdir/index.fy-head"
    srec=$(cat "$gitdir/index.fy-head" 2>/dev/null) || srec=
    [ "$srec" = "$cur" ] && return 0
    stamp=$(cat "$gitdir/fy-box-head" 2>/dev/null) || stamp=
    if [ "$stamp" = "$cur" ]; then
        fy_propose "$cur"
    elif [ -z "$srec" ]; then
        # No marker at all (a fresh worktree, a checkout never synced): record it now, not offer
        # it. The sealed heal needs one, and the tick's ~2 s left the agent's first commit unsealed
        # — a host commit in that gap recorded its files as deleted.
        fy_mark_shared "$cur"
    else
        fy_pending "$cur" || fy_offer_record "$cur"
    fi
}

# Bootstrap: a MISSING index file reads as empty — every tracked file would show as a staged
# deletion (the exact scare this shim exists to prevent). Seed from the shared index (keeps stat
# cache + staged state). Host git REPLACES that file, and over virtiofs a replaced name reads as
# missing from here for up to ~1 s on Lima, ~5 s on podman machine's libkrun (ADR-0021's
# visibility record) — so a copy that finds nothing is retried for ~6 s, and if the shared index
# never shows, HEAD's tree seeds it: clean, never empty. Only an unborn repo has nothing to copy
# (empty is then correct, so it doesn't wait) — and an unresolvable HEAD alone doesn't say unborn:
# a host commit replaces the branch ref in the same window as the index. The reflog is appended
# in place, never replaced, so one with entries says HEAD has had a commit.
fy_seed() {
    local tries=60 seed="$ix.seed.$$"
    while ((tries--)); do
        cp "$gitdir/index" "$seed" 2>/dev/null && {
            fy_seed_publish "$seed"
            return 0
        }
        pregit rev-parse -q --verify HEAD >/dev/null 2>&1 || [ -s "$gitdir/logs/HEAD" ] || return 0
        sleep 0.1
    done
    GIT_INDEX_FILE="$seed" pregit read-tree HEAD 2>/dev/null && fy_seed_publish "$seed"
    rm -f "$seed" 2>/dev/null || true
}

# Built under a temp name and renamed in only while index-box is still absent, under git's own
# lock on it: two first calls can both find none, and while one waits out the window the other may
# seed AND stage — git takes that same lock to write index-box, so neither can land between the
# check and the rename.
fy_seed_publish() { # $1 = the built seed
    local tries=40
    while ((tries--)); do
        if (set -o noclobber && : >"$ix.lock") 2>/dev/null; then
            [ -f "$ix" ] || mv -f "$1" "$ix" 2>/dev/null
            rm -f "$ix.lock"
            break
        fi
        [ -f "$ix" ] && break # a git command holds the lock on an index-box already there
        sleep 0.05
    done
    rm -f "$1" 2>/dev/null || true
}


# Subcommands that never move a ref: no override (their hooks, if any, are the repo's own).

# Point this command's hooks at ours (see "The under-lock check"), remembering the repo's own —
# resolved with the caller's `-c`s, and ours placed after them so it wins.
fy_hook_args=()
fy_arm_hooks() {
    local out
    case " $FY_NO_REF_SUBS " in *" $sub "*) return 0 ;; esac
    [ -e "$fy_hooks_dir/reference-transaction" ] || return 0 # not installed: refresh-only
    out=$(pregit rev-parse --path-format=absolute --show-toplevel --git-path hooks 2>/dev/null) ||
        return 0 # bare, or a git before 2.31
    FY_ORIG_TOP=${out%%$'\n'*} FY_ORIG_HOOKS=${out#*$'\n'}
    # Inside a hook of an outer box command, git hands our override down (GIT_CONFIG_PARAMETERS):
    # asked through it, "the repo's own hooks" is OUR directory — and delegating to it looped.
    if [ "$FY_ORIG_HOOKS" -ef "$fy_hooks_dir" ]; then
        FY_ORIG_HOOKS=$(env -u GIT_CONFIG_PARAMETERS -u GIT_CONFIG_COUNT \
            "$real" ${pre[@]+"${pre[@]}"} rev-parse --path-format=absolute --git-path hooks 2>/dev/null) ||
            FY_ORIG_HOOKS=
    fi
    FY_ORIG_GITDIR=$gitdir FY_ORIG_COMMON=$(fy_common "$gitdir")
    export FY_ORIG_TOP FY_ORIG_HOOKS FY_ORIG_GITDIR FY_ORIG_COMMON
    fy_hook_args=(-c "core.hooksPath=$fy_hooks_dir")
}

# Discovery itself reads HEAD: a host checkout/rebase/stash that just rewrote it can read as absent
# here, and git then says "not a git repository" (16 agent retries in one 30-s workflow run). This
# runs before any refresh, so on a failure: refresh `.git` and `.git/HEAD` up the tree from where
# git starts (the -C chain; a linked worktree's `.git` file names its own HEAD), and ask again.
fy_start_dir() { # where git starts looking: $PWD, then each -C in turn
    local d=$PWD k n
    for ((k = 0; k < ${#pre[@]}; k++)); do
        [ "${pre[k]}" = -C ] || continue
        n=${pre[k + 1]:-}
        case $n in /*) d=$n ;; "") ;; *) d=$d/$n ;; esac
    done
    printf '%s' "$d"
}
fy_rediscover() {
    local d gd
    d=$(fy_start_dir)
    while :; do
        fy_relookup "$d/.git"
        if [ -f "$d/.git" ]; then
            gd=$(sed -n 's/^gitdir: //p' "$d/.git" 2>/dev/null)
            case $gd in "") ;; /*) fy_relookup "$gd/HEAD" ;; *) fy_relookup "$d/$gd/HEAD" ;; esac
        else
            fy_relookup "$d/.git/HEAD"
        fi
        [ "$d" = / ] || [ -z "$d" ] && break
        d=$(dirname "$d")
    done
    pregit rev-parse --absolute-git-dir 2>/dev/null
}

# A git operation in progress (its state files) — and who started it. During a HOST `pull
# --rebase` HEAD is detached: a box commit landed on it and was dropped when the rebase finished
# (the workflow tests), and a box reset mid-rebase would wreck it. One kernel has that race too,
# but here the box can see the state and wait. The box marks an operation IT starts (fy-box-op), so
# an agent can finish its own; the mark goes once no operation state is left. A name the host
# just removed can still read as present over the mount: re-asked before it counts.
FY_OP_STATE="rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD REVERT_HEAD BISECT_LOG"
FY_OP_SUBS=" rebase merge pull cherry-pick revert am bisect "
FY_HEAD_SUBS=" commit reset checkout switch merge rebase cherry-pick revert am "
fy_op_in_progress() { # prints the first operation state present
    local op
    for op in $FY_OP_STATE; do
        [ -e "$gitdir/$op" ] || continue
        fy_relookup "$gitdir/$op"
        [ -e "$gitdir/$op" ] && { printf '%s' "$op"; return 0; }
    done
    return 1
}

cur= gitdir= ix= token= state= moved= tx=
# The other way discovery goes wrong: a NESTED repo whose `.git` reads absent for a moment makes git
# walk UP and find the outer repo — a box commit landed there (7 agent commits in the e2e
# fixture's own repo; the probe repo's directory had been replaced). A command that can write,
# started below the repo git found, re-asks each `.git` on the way up (one lookup per level, none
# at a repo's top); a nearer one wins. Prints that repo's git dir.
fy_nearer_repo() { # $1 = the git dir discovery found
    local d
    d=$(fy_start_dir)
    [ "$d/.git" -ef "$1" ] && return 1 # started at its top: the common case, free
    while [ -n "$d" ] && [ "$d" != / ]; do
        fy_relookup "$d/.git"
        if [ -e "$d/.git" ]; then # the first one up is the repo git should have found
            [ "$d/.git" -ef "$1" ] && return 1
            pregit rev-parse --absolute-git-dir 2>/dev/null
            return
        fi
        d=${d%/*}
    done
    return 1
}

if gitdir=$(pregit rev-parse --absolute-git-dir 2>/dev/null) || gitdir=$(fy_rediscover); then
    case " $FY_NO_REF_SUBS " in
    *" $sub "*) ;;
    *) nearer=$(fy_nearer_repo "$gitdir") && [ -n "$nearer" ] && gitdir=$nearer ;;
    esac
    fy_refresh "$gitdir"
    if op=$(fy_op_in_progress); then
        case $FY_HEAD_SUBS in *" $sub "*)
            if [ ! -e "$gitdir/fy-box-op" ]; then
                fy_said_leftover "refusing 'git $sub'" "$gitdir/$op" && exit 1
                echo "foldyard git shim: refusing 'git $sub' — your computer is in the middle of a" \
                    "git operation ($op), and a box $sub now would be lost or break it when it" \
                    "finishes (ADR-0021). Nothing was changed; run it again once it's done." >&2
                exit 1
            fi
            ;;
        esac
    else
        rm -f "$gitdir/fy-box-op" 2>/dev/null
        case $FY_OP_SUBS in *" $sub "*) { : >"$gitdir/fy-box-op"; } 2>/dev/null ;; esac
    fi
    ix="$gitdir/index-box"
    [ -f "$ix" ] || fy_seed
    export GIT_INDEX_FILE="$ix" FY_GIT_SHIM_INDEX="$ix"
    tries=3
    while :; do
        cur=$(pregit rev-parse -q --verify HEAD 2>/dev/null) || cur=
        heal_rc=0
        case " $FY_NO_INDEX_SUBS " in
        *" $sub "*) break ;; # index-irrelevant — skip the heal entirely (see the list's comment)
        esac
        fy_heal || heal_rc=$?
        fy_shared_sync
        # The heal takes long enough for the host to move HEAD meanwhile, and an index healed for
        # the old HEAD committed on the new one reverts the host's commit: look again, re-heal.
        fy_refresh "$gitdir"
        [ "$(pregit rev-parse -q --verify HEAD 2>/dev/null)" = "$cur" ] && break
        ((--tries)) && continue
        if [ "$sub" = commit ]; then
            echo "foldyard git shim: refusing 'git commit' — HEAD kept moving on your computer" \
                "while this index was being prepared (ADR-0021). Nothing was changed; run it" \
                "again once it settles." >&2
            exit 1
        fi
        break
    done
    # A branch that reads as UNBORN but has its own reflog isn't unborn: the host just replaced its
    # file and this kernel can't see it yet. Git would believe it too — a commit made a ROOT commit,
    # and the checks below agree with it (both sides saw "unborn"). An orphan branch has no reflog.
    if [ -z "$cur" ]; then
        case " $FY_HEAD_SUBS " in *" $sub "*)
            headref=$(cat "$gitdir/HEAD" 2>/dev/null) || headref=
            case $headref in "ref: refs/"*)
                if [ -s "$(fy_common "$gitdir")/logs/${headref#ref: }" ]; then
                    echo "foldyard git shim: refusing 'git $sub' — ${headref#ref: } reads as having no" \
                        "commits, but it has history: your computer just moved it (ADR-0021)." \
                        "Nothing was changed; run it again." >&2
                    exit 1
                fi
                ;;
            esac
            ;;
        esac
    fi
    if [ "$sub" = commit ] && [ "$heal_rc" -ne 0 ] && [ -z "${FY_GIT_SHIM_STALE_OK:-}" ]; then
        echo "foldyard git shim: refusing 'git commit' on the stale index above — it would" \
            "silently create a commit REVERTING the other side's work (ADR-0021). Fix:" \
            "'git checkout-index -a', then 'git reset', then re-stage and retry." \
            "Deliberate override: FY_GIT_SHIM_STALE_OK=1." >&2
        exit 1
    fi
    fy_arm_hooks
    if ((${#fy_hook_args[@]})); then
        headref=$(cat "$gitdir/HEAD" 2>/dev/null) || headref=
        case $headref in "ref: "*) headref=${headref#ref: } ;; *) headref=HEAD ;; esac
        state=$(mktemp "${TMPDIR:-/tmp}/fy-git-head.XXXXXX" 2>/dev/null) &&
            printf '%s %s\n' "$headref" "$cur" >"$state" && export FY_HEAD_STATE="$state"
        moved=$(mktemp "${TMPDIR:-/tmp}/fy-git-moved.XXXXXX" 2>/dev/null) && export FY_MOVED="$moved"
        tx=$(mktemp "${TMPDIR:-/tmp}/fy-git-tx.XXXXXX" 2>/dev/null) && export FY_TX="$tx"
        if [ "$sub" = commit ]; then
            token=$(mktemp "${TMPDIR:-/tmp}/fy-git-heal.XXXXXX" 2>/dev/null) &&
                printf '%s %s\n' "$headref" "$cur" >"$token" && export FY_HEAL_TOKEN="$token"
        fi
    fi
else
    exec "$real" "${args[@]}"
fi

"$real" "${args[@]:0:subix}" ${fy_hook_args[@]+"${fy_hook_args[@]}"} "${args[@]:subix}"
rc=$?
moved_to=$(cat ${moved:+"$moved"} /dev/null 2>/dev/null)
tx_state=$(cat ${tx:+"$tx"} /dev/null 2>/dev/null)
case $tx_state in "held "*) rm -f "$gitdir/index.lock" ;; esac # neither committed nor aborted ran
rm -f ${token:+"$token"} ${state:+"$state"} ${moved:+"$moved"} ${tx:+"$tx"}
# Post-command: if OUR command moved HEAD (commit, checkout, reset, rebase step…), stamp the
# box-side attribution AND resync this index's own marker — whatever index state the command
# left behind is deliberate at the new HEAD (a soft reset's kept staging included).
#
# "HEAD changed while it ran" is NOT "our command moved it": a refused commit, or a mere
# `git log`, that overlapped a host commit claimed the host's commit — index-box recorded as
# synced there while still holding the old tree, so every host file read as a staged deletion
# and the agent's next commit reverted them (measured, lefthook run on libkrun). Our own
# transaction says what it committed (FY_MOVED); a command that moves no ref claims nothing; only
# a box without the hooks installed falls back to "HEAD changed and the command succeeded".
fy_op_in_progress >/dev/null || rm -f "$gitdir/fy-box-op" 2>/dev/null # its operation is over
post=$(pregit rev-parse -q --verify HEAD 2>/dev/null) || post=
ours=
case " $FY_NO_REF_SUBS " in
*" $sub "*) ;;
*)
    if [ -n "$moved_to" ]; then
        [ "$moved_to" = "$post" ] && ours=1 # a host move right after ours is not ours
    elif [ -z "$moved" ] && [ "$rc" -eq 0 ]; then
        ours=1 # hooks not armed here (not installed / git < 2.31): the old rule, after success only
    fi
    ;;
esac
if [ -n "$ours" ] && [ -n "$post" ] && [ "$post" != "$cur" ]; then
    { printf '%s\n' "$post" >"$gitdir/fy-box-head"; } 2>/dev/null || true
    fy_record "$post"
    fy_propose "$post" # the shared index now describes the old HEAD (a no-op if our transaction healed it)
fi
exit "$rc"
