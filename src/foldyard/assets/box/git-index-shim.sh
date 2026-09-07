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

[ -n "${FY_GIT_SHIM_OFF:-}" ] && exec "$real" "$@"

# Respect an explicit GIT_INDEX_FILE — git's own temp-index protocols (stash, commit -a hooks)
# and deliberate callers. If it is OUR injection inherited through a subprocess (marker
# matches), fall through and re-resolve, so a child git running in a DIFFERENT repo never
# reuses the parent repo's index file.
if [ -n "${GIT_INDEX_FILE:-}" ] && [ "$GIT_INDEX_FILE" != "${FY_GIT_SHIM_INDEX:-}" ]; then
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

case "$sub" in
# Repo-creating commands write the NEW repo's checkout through an inherited GIT_INDEX_FILE
# (verified: clone hijacks it, leaving the fresh clone looking broken) — never inject here.
"" | clone | init) exec "$real" "${args[@]}" ;;
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

# Run the real git with the caller's own discovery flags, so heal probes resolve the same repo
# (and, once exported, the same injected index) as the command itself.
pregit() { "$real" ${pre[@]+"${pre[@]}"} "$@"; }

fy_record() { printf '%s\n' "$1" >"$ix.head" 2>/dev/null || true; }

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
fy_matches_a_past_head() {
    local t
    t=$(pregit write-tree 2>/dev/null) || return 1
    [ -n "$t" ] || return 1
    pregit rev-list -g HEAD --format='%T' 2>/dev/null | grep -qxF "$t"
}

# Self-heal a stale index-box before the command runs. Returns 1 (→ the commit guard) only for
# the one state it can't fix without guessing: the other side moved HEAD AND this index carries
# genuinely staged work. Everything is conditional and non-destructive: staged state is only
# ever reset when it provably equals a commit's tree (nothing of the user's to lose).
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
            # Pure staleness — nothing genuinely staged. A mixed reset both fast-forwards the
            # index and refreshes its stat cache; on failure (bare repo, lock contention) leave
            # the marker stale so the next invocation retries.
            rm -f "$ix.stale" 2>/dev/null || true
            pregit reset -q 2>/dev/null && fy_record "$cur"
            return 0
        fi
        if [ -z "$rec" ]; then
            # First sight of this index with real staged work and no sync point to diff against:
            # treat the staged state as intentional at the current HEAD.
            rm -f "$ix.stale" 2>/dev/null || true
            fy_record "$cur"
            return 0
        fi
        printf '%s\n' "$sig" >"$ix.stale" 2>/dev/null || true
    fi
    echo "foldyard git shim: index-box is stale — HEAD moved (${rec:0:12}… → ${cur:0:12}…, by the" \
        "host or another session) while it carries staged changes, so it can't be fast-forwarded." \
        "'git status' may show phantom staged diffs and commits are blocked meanwhile." \
        "Recover with: 'git checkout-index -a' (materialise anything staged-but-absent from the" \
        "worktree — a mixed reset would otherwise leave it only as an unreferenced blob), then" \
        "'git reset', then re-stage." >&2
    return 1
}

cur= gitdir= ix=
if gitdir=$(pregit rev-parse --absolute-git-dir 2>/dev/null); then
    ix="$gitdir/index-box"
    # Bootstrap: a MISSING index file reads as empty — every tracked file would show as a
    # staged deletion (the exact scare this shim exists to prevent). Seed from the shared
    # index (keeps stat cache + staged state); a fresh/unborn repo has none to copy — empty
    # is then correct.
    [ -f "$ix" ] || { [ -f "$gitdir/index" ] && cp "$gitdir/index" "$ix" 2>/dev/null; } || true
    export GIT_INDEX_FILE="$ix" FY_GIT_SHIM_INDEX="$ix"
    cur=$(pregit rev-parse -q --verify HEAD 2>/dev/null) || cur=
    heal_rc=0
    case " $FY_NO_INDEX_SUBS " in
    *" $sub "*) ;; # index-irrelevant — skip the heal entirely (see the list's comment)
    *) fy_heal || heal_rc=$? ;;
    esac
    if [ "$sub" = commit ] && [ "$heal_rc" -ne 0 ] && [ -z "${FY_GIT_SHIM_STALE_OK:-}" ]; then
        echo "foldyard git shim: refusing 'git commit' on the stale index above — it would" \
            "silently create a commit REVERTING the other side's work (ADR-0021). Fix:" \
            "'git checkout-index -a', then 'git reset', then re-stage and retry." \
            "Deliberate override: FY_GIT_SHIM_STALE_OK=1." >&2
        exit 1
    fi
else
    exec "$real" "${args[@]}"
fi

"$real" "${args[@]}"
rc=$?
# Post-command: if OUR command moved HEAD (commit, checkout, reset, rebase step…), stamp the
# box-side attribution AND resync this index's own marker — whatever index state the command
# left behind is deliberate at the new HEAD (a soft reset's kept staging included).
post=$(pregit rev-parse -q --verify HEAD 2>/dev/null) || post=
if [ -n "$post" ] && [ "$post" != "$cur" ]; then
    printf '%s\n' "$post" >"$gitdir/fy-box-head" 2>/dev/null || true
    fy_record "$post"
fi
exit "$rc"
