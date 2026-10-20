#!/bin/bash
# Slice 0 of docs/ebpf-monitoring-spike.md, re-runnable on any host with Lima:
#
#   limactl create --tty=false --name fyspike template:podman --set '.mounts = []'
#   limactl start fyspike
#   docs/ebpf-spike-kit/run.sh fyspike
#
# Use a THROWAWAY instance, never a foldyard VM: the kit installs Tetragon with the guest's sudo,
# which foldyard's boot provisioning narrows to `shutdown` (so it would fail there, by design).
# Host side needs only limactl, curl, python3 and sha256sum or shasum.
set -euo pipefail
VM=${1:?usage: run.sh <lima-instance>}
TETRAGON=${TETRAGON_VERSION:-v1.7.1}
KIT=$(cd "$(dirname "$0")" && pwd)
OUT=${OUT:-$PWD/ebpf-spike-$VM-$(date -u +%Y%m%dT%H%M%SZ)}
mkdir -p "$OUT"
vm() { limactl shell --workdir / "$VM" "$@"; }
sha() { if command -v sha256sum >/dev/null; then sha256sum "$@"; else shasum -a 256 "$@"; fi; }

case "$(vm uname -m)" in x86_64) ARCH=amd64 ;; aarch64) ARCH=arm64 ;; *) echo "unknown arch" >&2; exit 1 ;; esac
TAR=tetragon-$TETRAGON-$ARCH.tar.gz
URL=https://github.com/cilium/tetragon/releases/download/$TETRAGON/$TAR

echo "▶ guest kernel"
limactl copy "$KIT/guest-probe.sh" "$VM:/tmp/guest-probe.sh"
vm sudo bash /tmp/guest-probe.sh | tee "$OUT/guest-probe.txt"

echo "▶ fetch $TAR on the host, verify, deliver over ssh, re-verify as root in the guest"
[ -f "$OUT/$TAR" ] || curl -fsSL -o "$OUT/$TAR" "$URL"
want=$(curl -fsSL "$URL.sha256sum" | awk '{print $1}')
echo "$want  $OUT/$TAR" | sha -c -
time limactl copy "$OUT/$TAR" "$VM:/tmp/$TAR"
limactl copy "$KIT/policy.yaml" "$VM:/tmp/fy-spike-policy.yaml"
vm sudo bash -s <<EOF
set -euo pipefail
install -d -m 0700 /root/fy-stage
cp /tmp/$TAR /root/fy-stage/$TAR   # copy into root-owned space FIRST, then verify the copy
echo "$want  /root/fy-stage/$TAR" | sha256sum -c -
tar -C /root/fy-stage -xzf /root/fy-stage/$TAR
cp -Rf /root/fy-stage/tetragon-$TETRAGON-$ARCH/usr/local/* /usr/local/
cp -f /usr/local/lib/tetragon/systemd/tetragon.service /usr/lib/systemd/system/
install -d /etc/tetragon/tetragon.conf.d /etc/tetragon/tetragon.tp.d
cp -n -r /usr/local/lib/tetragon/tetragon.conf.d /etc/tetragon/ 2>/dev/null || true
echo false >/etc/tetragon/tetragon.conf.d/export-file-compress
install -m 0644 /tmp/fy-spike-policy.yaml /etc/tetragon/tetragon.tp.d/fy-spike.yaml
/usr/local/bin/tetra probe 2>&1 | tr '\n' ' '; echo
systemctl daemon-reload
systemctl enable tetragon
systemctl restart tetragon
EOF
echo "▶ waiting for Tetragon to load the policy"
t0=$(date +%s)
until vm sudo tetra tracingpolicy list 2>/dev/null | grep -q 'fy-spike *enabled'; do
    sleep 5
    if [ $(($(date +%s) - t0)) -gt 600 ]; then vm sudo journalctl -u tetragon --no-pager | tail -20; exit 1; fi
done
echo "  ready in $(($(date +%s) - t0)) s"
vm sudo tetra tracingpolicy list | tee "$OUT/policies.txt"

echo "▶ workload (as the Lima user)"
limactl copy "$KIT/guest-workload.sh" "$VM:/tmp/guest-workload.sh"
vm bash -l /tmp/guest-workload.sh | tee "$OUT/workload.out"
vm sudo bash -c 'ps -o rss=,time=,etime= -C tetragon; cp /var/log/tetragon/tetragon.log /tmp/tt.json; chmod 0644 /tmp/tt.json' \
    | tee "$OUT/tetragon-ps.txt"
limactl copy "$VM:/tmp/tt.json" "$OUT/tetragon.json"
python3 "$KIT/analyse.py" "$OUT/tetragon.json" "$OUT/workload.out" | tee "$OUT/analysis.txt"
echo "▶ results in $OUT"
