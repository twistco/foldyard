#!/bin/bash
# Runs INSIDE the Lima guest as root (run.sh copies this in and invokes it).
# Model: default-deny egress for the `agent` user + an EXPLICIT CONNECT proxy as the only way
# out. The agent gets HTTP(S)_PROXY pointing at the proxy; anything that ignores it is dropped
# (fail-closed). No nftables REDIRECT / conntrack-NAT — robust across kernels.
set -euxo pipefail

# rootless container *networking* needs passt/slirp4netns (pull works without; run doesn't).
# root is unconstrained by the wall, so this apt works even on a re-run with the wall up.
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends passt slirp4netns >/dev/null 2>&1 || true
modprobe nf_log_syslog 2>/dev/null || true   # so the wall's `log` lands in dmesg (recourse)

# CRITICAL: no rootful podman socket may be reachable by the agent — it is VM-root (and thus a
# total wall bypass). The agent uses only its own ROOTLESS socket. wall.yaml's provisioning already
# MASKS podman.socket (don't trust image defaults — Ubuntu shipped it enabled); disable here too as
# belt-and-braces, so this kit is safe even if run on a base image whose provisioning skipped it.
systemctl disable --now podman.socket 2>/dev/null || true
rm -f /run/podman/podman.sock 2>/dev/null || true

# --- users -------------------------------------------------------------------------------
id proxy >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin proxy
id agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent
grep -q '^agent:' /etc/subuid || usermod --add-subuids 200000-265535 --add-subgids 200000-265535 agent
loginctl enable-linger agent || true

AGENT_UID=$(id -u agent)
PROXY_UID=$(id -u proxy)

# the agent uses the proxy by default (login + interactive shells); this is also what lets a
# real agent like Claude reach its allowlisted API. Tests override this per-check explicitly.
cat > /home/agent/.bashrc <<'EOF'
export HTTP_PROXY=http://127.0.0.1:8080  HTTPS_PROXY=http://127.0.0.1:8080
export http_proxy=http://127.0.0.1:8080  https_proxy=http://127.0.0.1:8080
export NO_PROXY=localhost,127.0.0.1      no_proxy=localhost,127.0.0.1
EOF
chown agent:agent /home/agent/.bashrc

# --- proxy + policy files ----------------------------------------------------------------
install -m 0755 /tmp/proxy.py /usr/local/bin/transproxy.py
install -d /etc/wall
cat > /etc/wall/allowlist <<'EOF'
# Hosts the agent may reach in `filtered` mode (exact host or .suffix match).
example.com
EOF
echo filtered > /etc/wall/mode          # filtered | passthrough  (runtime-toggleable)
: > /var/log/transproxy.log
chown proxy:proxy /var/log/transproxy.log

cat > /usr/local/bin/wall-mode <<'EOF'
#!/bin/bash
if [ -n "$1" ]; then echo "$1" > /etc/wall/mode; fi
echo "mode=$(cat /etc/wall/mode)"
EOF
chmod 0755 /usr/local/bin/wall-mode

# recourse: show what got blocked + how to fix it (the answer to "why did X mysteriously fail?")
cat > /usr/local/bin/wall-denied <<'EOF'
#!/bin/bash
echo "== proxy BLOCKs — cooperative clients hitting non-allowlisted hosts (have hostnames) =="
grep BLOCK /var/log/transproxy.log 2>/dev/null | tail -20 || echo "  (none)"
echo
echo "== wall REJECTs — non-cooperative direct egress (have dest IPs) =="
dmesg 2>/dev/null | grep -E "wall-deny" | tail -20 || echo "  (none — needs nf_log_syslog)"
echo
echo "Fix: add the host to /etc/wall/allowlist (then it works via the proxy), or run a"
echo "non-cooperative client with the proxy env / behind a transparent layer."
EOF
chmod 0755 /usr/local/bin/wall-denied

# --- the wall: default-deny the agent's egress; only lo (the proxy) + DNS allowed ----------
cat > /etc/wall/wall.nft <<EOF
flush ruleset
table inet wall {
  chain output {
    type filter hook output priority 0; policy accept;
    oifname "lo" accept                       # reaches the proxy at 127.0.0.1:8080 (+ DNS stub)
    ct state established,related accept
    meta skuid != $AGENT_UID accept           # only the agent is constrained
    udp dport 53 accept                        # DNS (proxy resolves upstreams; agent rarely needs it)
    tcp dport 53 accept
    # agent's direct egress: log it (recourse via \`wall-denied\`) and REJECT fast (not a silent
    # DROP/hang) so non-cooperative clients fail immediately with "connection refused".
    log prefix "wall-deny " counter
    meta l4proto tcp reject with tcp reset
    reject
  }
  chain forward {
    type filter hook forward priority 0; policy accept;
    ct state established,related accept
    ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } accept
    log prefix "wall-deny-fwd " counter        # bridged container egress to the internet
    reject
  }
}
table ip6 wall6 {
  chain output {
    type filter hook output priority 0; policy accept;
    meta skuid != $AGENT_UID accept
    oifname "lo" accept
    log prefix "wall-deny6 " counter           # force the agent onto the v4 proxy path
    reject
  }
}
EOF

# --- systemd units (persist across reboot) ----------------------------------------------
cat > /etc/systemd/system/transproxy.service <<'EOF'
[Unit]
Description=Explicit forcing proxy
After=network-online.target
Wants=network-online.target
[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/transproxy.py
User=proxy
Restart=always
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/wall.service <<'EOF'
[Unit]
Description=nftables egress forcing wall
After=transproxy.service
Requires=transproxy.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f /etc/wall/wall.nft
ExecReload=/usr/sbin/nft -f /etc/wall/wall.nft
[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable transproxy.service wall.service
systemctl restart transproxy.service   # pick up an updated proxy.py on re-runs
systemctl restart wall.service          # re-apply an updated wall.nft on re-runs

# Pre-pull the test image as the agent THROUGH the proxy (works on every run): flip to
# passthrough so the registry is reachable, pull with the proxy env, then back to filtered.
echo passthrough > /etc/wall/mode
sudo -iu agent env HTTPS_PROXY=http://127.0.0.1:8080 HTTP_PROXY=http://127.0.0.1:8080 \
  podman pull docker.io/library/alpine:latest \
  || echo "WARN: agent rootless image pre-pull failed — the optional container check (e) will skip"
echo filtered > /etc/wall/mode

echo "---- ruleset ----"; nft list ruleset | sed -n '1,40p'
echo "INSTALL OK (agent_uid=$AGENT_UID proxy_uid=$PROXY_UID mode=$(cat /etc/wall/mode))"
