---
section: Fixed
bump: patch
---

- **A VM restarted after a crash no longer hangs waiting for ssh.** `fy up` restarts a VM that
  says it is running but serves nothing, and that restart kept the ssh port it last recorded.
  When the port was stale and taken, the start waited forever. The port is now re-pinned while
  the VM is stopped. Where Podman Desktop is followed, `fy doctor` warns when the VM's ssh
  port is off its port range.
