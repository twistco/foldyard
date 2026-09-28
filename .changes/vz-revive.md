---
section: Fixed
bump: patch
---

- **On macOS, starting a Lima VM no longer treats the running VM as an orphan.** Before a start
  foldyard reaps a Lima hostagent that outlived its VM, and it judged "outlived" by QEMU's pid
  file. The default macOS VM type (`vz`) runs the VM inside the hostagent and writes no such
  file, so a live VM could be waited on and signalled. That reap was also, by accident, what
  recovered a hung VM. Now a VM that Lima reports `Broken`, or that a graceful stop leaves
  running, is ended with Lima's own `limactl stop --force` before it starts again. The graceful
  stop gets a minute first, where Lima would wait over six.
