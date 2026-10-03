#!/bin/bash
# Slice 0, step 1: what the Lima podman-template guest kernel offers. Run as root in the guest.
set -u
echo "== kernel"; uname -r; cat /etc/os-release | grep PRETTY
echo "== lsm (active)"; cat /sys/kernel/security/lsm; echo
echo "== cmdline"; cat /proc/cmdline
echo "== btf"; ls -la /sys/kernel/btf/vmlinux
echo "== cgroup"; stat -fc %T /sys/fs/cgroup; cat /sys/fs/cgroup/cgroup.controllers
echo "== config"
cfg=/boot/config-$(uname -r)
grep -E "^(# )?CONFIG_(KPROBES|KPROBE_EVENTS|UPROBE_EVENTS|BPF_LSM|BPF_KPROBE_OVERRIDE|FPROBE|FUNCTION_ERROR_INJECTION|DEBUG_INFO_BTF|BPF_JIT|BPF_SYSCALL|CGROUP_BPF|DYNAMIC_FTRACE_WITH_DIRECT_CALLS|IMA|SECURITY_SELINUX)[= ]" "$cfg"
echo "== unprivileged_bpf_disabled"; cat /proc/sys/kernel/unprivileged_bpf_disabled
echo "== selinux"; getenforce 2>/dev/null
echo "== podman"; podman --version
