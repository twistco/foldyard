#!/bin/bash
# Make a Claude Code cloud container a foldyard HOST: QEMU + Lima + a non-root operator with fy on
# PATH, and two wrappers that run commands as that operator with a clean environment. Run as root,
# once per container (idempotent). See SKILL.md beside this for why each step exists.
#
#   .claude/skills/cloud-host/setup.sh [operator] [foldyard checkout]
#
# Afterwards:  /opt/fy-cloud/<operator>/run 'cd ~/<project> && fy box up'
#              /opt/fy-cloud/<operator>/box <machine> 'fy verify'
#
# Root writes only under /opt/fy-cloud (root-owned) and a fresh mktemp dir. Everything in the
# operator's home is written AS the operator: on a rerun, root following a symlink the operator
# planted there would let it overwrite or take ownership of any file.
set -euo pipefail
OP=${1:-fyhost}
SRC=${2:-$(cd "$(dirname "$0")/../../.." && pwd)}
LIMA_VERSION=${LIMA_VERSION:-2.2.0} # the release CI pins
CA_SRC=${CA_SRC:-/root/.ccr/ca-bundle.crt}

[ "$(id -u)" = 0 ] || { echo "run as root (it installs packages and creates the operator)" >&2; exit 1; }

echo "▶ packages (QEMU, OVMF, qemu-img, the podman CLI, ssh — Lima's hostagent needs ssh)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    ovmf qemu-system-x86 qemu-utils podman uidmap slirp4netns jq openssh-client git curl >/dev/null

echo "▶ operator '$OP' (Lima refuses to run as root)"
id "$OP" >/dev/null 2>&1 || useradd -m -s /bin/bash "$OP"
H=$(getent passwd "$OP" | cut -d: -f6)
D=/opt/fy-cloud/$OP
install -d -m 0755 /opt/fy-cloud "$D"

# The session's egress proxy re-signs ALL TLS — even "direct" connections — with its own CA. Copy
# the bundle where the operator can read it; never into the system store.
if [ -f "$CA_SRC" ]; then
    install -m 0644 "$CA_SRC" "$D/ca.crt"
else
    install -m 0644 /etc/ssl/certs/ca-certificates.crt "$D/ca.crt"
fi

# The operator's whole environment. No *_PROXY: Lima copies those into the guest
# (propagateProxyEnv), where they override the wall's proxy and the VM firewall refuses the pulls.
cat >"$D/env" <<EOF
export HOME=$H USER=$OP LOGNAME=$OP SHELL=/bin/bash LANG=C.UTF-8 TERM=dumb
export PATH=$H/.local/bin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export SSL_CERT_FILE=$D/ca.crt REQUESTS_CA_BUNDLE=$D/ca.crt CURL_CA_BUNDLE=$D/ca.crt
export GIT_SSL_CAINFO=$D/ca.crt NODE_EXTRA_CA_CERTS=$D/ca.crt
export XDG_RUNTIME_DIR=/tmp/$OP-run
mkdir -p "\$XDG_RUNTIME_DIR" && chmod 700 "\$XDG_RUNTIME_DIR"
EOF
# run: a command as the operator, from an EMPTY environment. `su` without `-` keeps the caller's —
# tokens, cloud keys, the session proxy, a BASH_ENV its shell would source — so root's is dropped
# before `su`, not after: the operator's shell never sees it, nor does the supervisor it starts.
cat >"$D/run" <<EOF
#!/bin/bash
[ "\$(id -un)" = "$OP" ] || exec env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \\
    su "$OP" -s /bin/bash -c "$D/run \$(printf '%q ' "\$@")"
exec env -i bash -c '. $D/env; cd ~; eval "\$1"' _ "\$*"
EOF
# box: a command inside the running dev box, non-interactively (`fy box shell` needs a TTY). The
# box starts in the checkout; its container is `<[project] prefix>-devbox` (FY_BOX overrides).
cat >"$D/box" <<EOF
#!/bin/bash
VM=\${1:?usage: box <machine> <command…>}; shift
exec $D/run "limactl shell --workdir / \$VM podman exec -i \${FY_BOX:-\$VM-devbox} bash -lc \$(printf %q "\$*")"
EOF
chmod 0644 "$D/env"; chmod 0755 "$D/run" "$D/box"

# mitmdump (foldyard's egress proxy) verifies upstream with certifi, so without this every
# decrypted host answers the box 502. Its default confdir is the one foldyard launches it with.
"$D/run" "mkdir -p ~/.local/bin ~/.mitmproxy && printf 'ssl_verify_upstream_trusted_ca: %s\n' $D/ca.crt >~/.mitmproxy/config.yaml"

# Prove the bundle, don't assume it: the system fallback above lacks the session's CA wherever the
# egress is intercepted, and then mitmdump answers 502 and every operator fetch fails to verify.
# Python with cafile= trusts that file ALONE, as mitmdump's option does; curl would not tell (it also
# reads its compiled-in /etc/ssl/certs).
echo "▶ TLS as the operator, verified with $D/ca.crt alone"
if ! "$D/run" "python3 -I -c 'import ssl, sys, urllib.request as u; u.urlopen(\"https://pypi.org/simple/\", timeout=30, context=ssl.create_default_context(cafile=sys.argv[1]))' $D/ca.crt"; then
    echo "✗ the operator's TLS does not verify with $D/ca.crt ($( [ -f "$CA_SRC" ] && echo "from $CA_SRC" || echo "the system bundle: $CA_SRC is absent")). Point CA_SRC at this session's CA bundle and run again." >&2
    exit 1
fi

echo "▶ Lima $LIMA_VERSION → $H/.local"
if ! [ -x "$H/.local/bin/limactl" ] || ! "$D/run" "limactl --version" | grep -q "$LIMA_VERSION"; then
    url=https://github.com/lima-vm/lima/releases/download/v$LIMA_VERSION/lima-$LIMA_VERSION-Linux-x86_64.tar.gz
    "$D/run" "curl -fsSL $url | tar -C ~/.local -xzf -"
fi

echo "▶ uv + foldyard (editable, from a clone of $SRC)"
if ! [ -x "$H/.local/bin/uv" ]; then
    if command -v uv >/dev/null; then
        install -m 0755 "$(command -v uv)" "$D/uv" # root's may sit where the operator can't read
        "$D/run" "install -m 0755 $D/uv ~/.local/bin/uv"; rm -f "$D/uv"
    else
        "$D/run" "curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
fi
# Root clones (the operator may not be able to read $SRC) into a dir only root can reach, hands
# the clone over, and the operator moves it home. --no-local: a local clone HARD-LINKS the
# source's objects, and the chown would then hand them, in the source checkout too, to the operator.
if ! [ -d "$H/foldyard/.git" ]; then
    T=$(mktemp -d)
    git clone --no-local -q "$SRC" "$T/foldyard"
    chown -R "$OP:$OP" "$T"
    "$D/run" "mv -T $T/foldyard ~/foldyard" # -T: an empty ~/foldyard becomes the clone, not its parent
    rm -rf "$T"
fi
"$D/run" "git config --global --add safe.directory '*'; cd ~/foldyard && uv tool install -q --force --editable ."

echo "▶ fy prereq (expect ✗ KVM and ⚠ systemd --user here — see SKILL.md)"
"$D/run" "fy prereq" || true
echo "✓ operator '$OP' ready. Run as it with: $D/run '<command>'"
