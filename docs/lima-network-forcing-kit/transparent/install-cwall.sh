#!/bin/bash
# Runs INSIDE the Lima guest as root. Builds the CONTAINER-EGRESS wall: every container on the
# walled bridge has its :80/:443 transparently REDIRECTed (in PREROUTING — the reliable path) to
# an in-VM proxy, and all its other egress is default-denied. So the "agent" box AND any sibling
# box it spawns via the podman socket are on the same network → same wall → contained. Transparent
# (no proxy env in containers), so non-cooperative clients (Go/gRPC) are caught too.
set -euxo pipefail

DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  podman nftables python3 passt netavark aardvark-dns uidmap curl >/dev/null 2>&1 || true
modprobe nf_log_syslog 2>/dev/null || true

# stop the explicit-kit services if they're installed (avoid a conflicting ruleset)
systemctl disable --now wall.service transproxy.service 2>/dev/null || true

# WE own the firewall — tell netavark not to add its own nft rules (no fighting its FORWARD/NAT).
mkdir -p /etc/containers
cat > /etc/containers/containers.conf <<'EOF'
[network]
firewall_driver = "none"
EOF

# proxy user + policy files
id proxy >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin proxy
install -d /etc/wall
printf 'example.com\n' > /etc/wall/allowlist
echo filtered > /etc/wall/mode
: > /var/log/transproxy.log; chown proxy:proxy /var/log/transproxy.log
install -m 0755 /tmp/tproxy.py /usr/local/bin/tproxy.py

cat > /usr/local/bin/wall-mode <<'EOF'
#!/bin/bash
[ -n "$1" ] && echo "$1" > /etc/wall/mode
echo "mode=$(cat /etc/wall/mode)"
EOF
chmod 0755 /usr/local/bin/wall-mode
cat > /usr/local/bin/wall-denied <<'EOF'
#!/bin/bash
echo "== proxy BLOCKs (cooperative + transparent hits to non-allowlisted hosts) =="
grep BLOCK /var/log/transproxy.log 2>/dev/null | tail -20 || echo "  (none)"
echo "== wall REJECTs (container egress dropped — dest IPs) =="
dmesg 2>/dev/null | grep "cwall-deny" | tail -20 || echo "  (none — needs nf_log_syslog)"
EOF
chmod 0755 /usr/local/bin/wall-denied

# rootful podman socket so an 'agent' container can spawn siblings, exactly like Foldyard's box
systemctl enable --now podman.socket 2>/dev/null || true

# the walled bridge network
SUBNET=10.89.0.0/24
podman network exists wallnet || podman network create wallnet --subnet "$SUBNET"

sysctl -w net.ipv4.ip_forward=1 >/dev/null

# transparent proxy (host ns), binds 0.0.0.0:8080
cat > /etc/systemd/system/tproxy.service <<'EOF'
[Unit]
Description=Transparent forcing proxy (container-egress)
After=network-online.target
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/tproxy.py
User=proxy
Restart=always
[Install]
WantedBy=multi-user.target
EOF

# THE WALL — we provide redirect + default-deny FORWARD + masquerade for the bridge subnet.
# forward chain runs EARLY (priority -10) so its REJECT wins over anything else at the hook.
WAN=$(ip route show default | awk '{print $5; exit}')
cat > /etc/wall/cwall.nft <<EOF
flush ruleset
table ip cwall {
  chain prerouting {
    type nat hook prerouting priority dstnat; policy accept;
    ip saddr $SUBNET tcp dport { 80, 443 } redirect to :8080
  }
  chain postrouting {
    type nat hook postrouting priority srcnat; policy accept;
    ip saddr $SUBNET oifname "$WAN" masquerade
  }
}
table inet cwallf {
  chain forward {
    type filter hook forward priority -10; policy accept;
    ct state established,related accept
    ip saddr $SUBNET udp dport 53 accept
    ip saddr $SUBNET tcp dport 53 accept
    ip saddr $SUBNET log prefix "cwall-deny " counter
    ip saddr $SUBNET reject
  }
}
EOF
cat > /etc/systemd/system/cwall.service <<'EOF'
[Unit]
Description=container-egress forcing wall
After=tproxy.service
Requires=tproxy.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/wall/cwall.nft
ExecReload=/usr/sbin/nft -f /etc/wall/cwall.nft
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable tproxy.service cwall.service
systemctl restart tproxy.service
systemctl restart cwall.service

# pre-pull alpine (engine egress is host-ns, not on the walled subnet, so it's unaffected)
podman pull docker.io/library/alpine:latest || echo "WARN: alpine pre-pull failed"

echo "---- ruleset ----"; nft list ruleset | sed -n '1,40p'
echo "INSTALL OK (subnet=$SUBNET wan=$WAN mode=$(cat /etc/wall/mode))"
