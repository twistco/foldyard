#!/usr/bin/env bash
# Runs as root in the L1, from `just nested-up` (tests/tools/nested.py). Safe to repeat.
#
# The GIC traps take effect at the next boot; nested.py restarts the L1 when the running kernel
# lacks them. docs/nested-virt.md says why each piece is here.
set -euo pipefail
: "${LIMA_VERSION:?}"

tester=fytest
packages=(qemu-system-aarch64-core qemu-img edk2-aarch64 git just uv jq nftables python3 curl tar)
if ! rpm -q "${packages[@]}" >/dev/null 2>&1; then
  dnf_log="$(mktemp)"
  dnf install -y -q "${packages[@]}" >"$dnf_log" 2>&1 || {
    tail -n 20 "$dnf_log"
    exit 1
  }
  rm -f "$dnf_log"
fi

if ! /usr/local/bin/limactl --version 2>/dev/null | grep -qxF "limactl version $LIMA_VERSION"; then
  url="https://github.com/lima-vm/lima/releases/download/v${LIMA_VERSION}/lima-${LIMA_VERSION}-Linux-aarch64.tar.gz"
  curl -fsSL "$url" | tar xz -C /usr/local
fi

# The tier's user: no sudo, so what it runs can't remove the wall below. `--with-sudo` grants it
# for one run, and an interrupted run can leave that behind.
#
# Its subordinate ids are named: Lima's own user holds the whole default range in this image, and a
# plain useradd then fails ("Can't get unique subordinate UID range").
if ! id "$tester" >/dev/null 2>&1; then
  useradd -m -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0 "$tester"
  usermod --add-subuids 1100000000-1100065535 --add-subgids 1100000000-1100065535 "$tester"
fi
rm -f /etc/sudoers.d/90-fy-nested-test
loginctl enable-linger "$tester"
uid="$(id -u "$tester")"
for _ in $(seq 1 30); do
  test -S "/run/user/$uid/bus" && break
  sleep 1
done
test -S "/run/user/$uid/bus"

grubby --update-kernel=ALL \
  --args="kvm-arm.vgic_v3_group0_trap=1 kvm-arm.vgic_v3_group1_trap=1 kvm-arm.vgic_v3_common_trap=1"

# Nothing in the L1 reaches your computer, or any other private address: DNS only, and DNS first,
# since the resolver is on the gateway. The public internet stays open, which the tier needs. No
# user is exempt. Replies to what your computer started (`limactl shell`) pass as established.
cat >/etc/fy-nested-wall.nft <<'NFT'
table inet fy_nested_wall {}
delete table inet fy_nested_wall
table inet fy_nested_wall {
  chain output {
    type filter hook output priority 0; policy accept;
    oifname "lo" accept
    ct state established,related accept
    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 } udp dport 53 accept
    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 } tcp dport 53 accept
    ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16 } drop
  }
}
NFT
cat >/etc/systemd/system/fy-nested-wall.service <<'UNIT'
[Unit]
Description=Keep what runs in this VM away from the computer it runs on
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/fy-nested-wall.nft

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q fy-nested-wall.service
systemctl restart fy-nested-wall.service
