"""guestfiles.py — the machine VM lets go of shared files nothing is using.

Your computer's side of a shared folder (Apple's virtiofs server under ``vz``) keeps a file open
for every file the VM's kernel still remembers, and that kernel forgets one only under memory
pressure, which a roomy VM never has. So the count climbs with every install, test run and scan on
the mount and stays there: on 2026-11-23 four projects' VMs held 474k of one Mac's 491,520 open
files between them, 95% of the largest one's from three worktrees' ``node_modules`` and ``.venv``.

The kernel's own answer is the FUSE module's ``inval_wq`` (both guest kernels we run have it, 6.19
and 7.3; off unless set): every that-many seconds it drops the lookups that have expired, and with
the last of them the file. What is in use stays — mounts over a shared dir, open and unlinked
files, locks, a running program, inotify watches, a working directory. Measured 2026-11-23 over
40k files: 40k held → 35 within 10 s at a period of 5, and 50k → 77 within 75 s at 60. The cost is
the next read of a dropped file starting cold, so a short period is felt: ``git status`` on 10k
files every 2 s ran slow 6 times in 23 at 5 s, once in 59 at 60 s (ADR-0021's 2026-11-23
amendment). It caps what lingers, not a burst inside one period.

A module parameter only root may write, so it rides the boot script (``machine.py``: root in the
guest is boot-time only), under its provisioning id. Housekeeping, not a posture: a kernel without
the setting, or one that refuses it, is a line in the boot log and never a failed boot.
"""

from __future__ import annotations

KNOB = "/sys/module/fuse/parameters/inval_wq"
DEFAULT_SECONDS = 60
MIN_SECONDS = 5  # the kernel refuses a shorter period (FUSE_DENTRY_INVAL_FREQ_MIN)…
MAX_SECONDS = 65535  # …and a longer one (USHRT_MAX)


def release_snippet(seconds: int) -> str:
    """The setting as bash, run as root by the boot script. ``seconds`` 0 is off: nothing is
    written, and the kernel boots with it off."""
    if not seconds:
        return ": # [machine] release_files is off"
    return f"""if [ ! -e {KNOB} ]; then
    echo "shared files: this kernel has no inval_wq, so it lets go of them under pressure only"
elif echo {seconds:d} >{KNOB}; then
    echo "shared files: released after {seconds:d} s or so unused"
else
    echo "shared files: the kernel refused inval_wq={seconds:d}"
fi"""
