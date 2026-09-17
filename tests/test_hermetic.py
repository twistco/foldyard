"""conftest's hermetic-subprocess guards: the PATH scrub and the execute-time refusal.

Pins the division of labour described at the guards themselves: a host tool named bare is simply
NOT FOUND (the CI outcome — the code under test takes its "not installed" branch), while one
reached by absolute path or through a caller's own ``env["PATH"]`` is refused by name, with an
exception no ``except Exception`` in the code under test can turn into a handled error."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from conftest import SPAWNABLE, HostToolSpawned

# Present on macOS and Linux alike, not on the allowlist: the stand-in for a host tool.
HOST_TOOL = "/bin/ls"


def test_scrub_hides_host_tools_but_keeps_the_allowlist():
    for tool in (
        "podman",
        "limactl",
        "gcloud",
        "gh",
        "terminal-notifier",
        "osascript",
        "ls",
        "env",
    ):
        assert shutil.which(tool) is None, tool
    for tool in (*SPAWNABLE, "python", "python3"):
        assert shutil.which(tool), tool
    assert len(os.environ["PATH"].split(os.pathsep)) == 1


def test_a_bare_host_tool_is_not_found_rather_than_refused():
    # The "not installed" branch is a legitimate outcome, not a leak — it is what CI sees.
    with pytest.raises(FileNotFoundError):
        subprocess.run(["osascript", "-e", "beep"], capture_output=True)


def test_an_absolute_path_to_a_host_tool_is_refused_by_name():
    with pytest.raises(HostToolSpawned, match=f"would execute {HOST_TOOL!r}"):
        subprocess.run([HOST_TOOL], capture_output=True)


def test_a_callers_own_path_cannot_smuggle_one_in():
    with pytest.raises(HostToolSpawned, match="would execute"):
        subprocess.Popen(["ls"], env={"PATH": "/bin:/usr/bin"})


def test_except_exception_cannot_swallow_the_refusal():
    # devmode.run_stream catches Exception and reports rc 127 — a leak reading as a handled
    # error is exactly how the 2026-08/09 escapes passed green.
    with pytest.raises(HostToolSpawned):
        try:
            subprocess.run([HOST_TOOL])
        except Exception:  # the point under test
            pytest.fail("the guard's exception was swallowed as a handled error")


def test_an_allowlisted_tool_is_fine_wherever_it_lives():
    # git_shim runs the real git by absolute path; the box PATH tests hand bash a PATH of their
    # own. The allowlist is about WHICH tool, not where it is.
    assert subprocess.run([shutil.which("true") or "true"], capture_output=True).returncode == 0
    assert subprocess.run(["true"], env={"PATH": "/usr/bin:/bin"}).returncode == 0


@pytest.mark.spawns(HOST_TOOL)
def test_the_marker_permits_a_named_binary():
    assert subprocess.run([HOST_TOOL, "/"], capture_output=True).returncode == 0


def test_an_executable_override_cannot_smuggle_one_in():
    # `executable=` is what actually runs; argv[0] is then only the name the child sees. Both
    # spellings.
    with pytest.raises(HostToolSpawned, match=f"would execute {HOST_TOOL!r}"):
        subprocess.run(["git", "/"], executable=HOST_TOOL, capture_output=True)
    with pytest.raises(HostToolSpawned, match=f"would execute {HOST_TOOL!r}"):
        subprocess.Popen(["git", "/"], 0, HOST_TOOL, stdout=subprocess.DEVNULL)


def test_shell_true_is_refused_outright():
    # The guard resolves argv[0]; a shell string is a whole program, and only its first word
    # would be seen — `true; /bin/ls` reads as `true`. Nothing under test spawns a shell this
    # way, so the form itself is refused rather than parsed.
    with pytest.raises(HostToolSpawned, match="shell=True"):
        subprocess.run(f"true; {HOST_TOOL} /", shell=True, capture_output=True)


def test_a_shell_wrapper_cannot_smuggle_a_program_in(tmp_path):
    # `bash -c …` / `sh script` / bare stdin carry a whole program past argv[0] — the shell=True
    # gap by another door. The shell itself is allowlisted (for `-n`), so the refusal names the
    # program form, not the binary.
    with pytest.raises(HostToolSpawned, match="would run a program under"):
        subprocess.run(["bash", "-c", f"{HOST_TOOL} /"], capture_output=True)
    with pytest.raises(HostToolSpawned, match="would run a program under"):
        subprocess.run(["bash", "-lc", f"{HOST_TOOL} /"], capture_output=True)
    script = tmp_path / "run.sh"
    script.write_text(f"{HOST_TOOL} /\n")
    with pytest.raises(HostToolSpawned, match="would run a program under"):
        subprocess.run(["sh", str(script)], capture_output=True)
    with pytest.raises(HostToolSpawned, match="would run a program under"):
        subprocess.run(["sh"], input=f"{HOST_TOOL} /", text=True, capture_output=True)
    # `executable=` is what runs; naming git as argv[0] doesn't change what the shell is handed.
    with pytest.raises(HostToolSpawned, match="would run a program under"):
        subprocess.run(["git", "-c", f"{HOST_TOOL} /"], executable="bash", capture_output=True)


def test_a_syntax_check_executes_nothing_and_is_fine():
    # The guestlog/machine tests `bash -n` their rendered scripts — parsing, not running: the
    # script names a host tool and nothing is spawned.
    res = subprocess.run(["bash", "-n"], input=f"{HOST_TOOL} /\n", text=True, capture_output=True)
    assert res.returncode == 0, res.stderr
    res = subprocess.run(["bash", "-o", "noexec", "-c", f"{HOST_TOOL} /"], capture_output=True)
    assert res.returncode == 0, res.stderr
    assert subprocess.run(["bash", "--version"], capture_output=True).returncode == 0


@pytest.mark.spawns("bash")
def test_the_marker_permits_a_program_under_a_named_shell():
    # The box PATH tests run the REAL prepend snippet under bash; naming the shell says so.
    res = subprocess.run(["bash", "-c", "echo ok"], capture_output=True, text=True)
    assert res.stdout == "ok\n"
    # …and it names THAT shell, not shells in general.
    with pytest.raises(HostToolSpawned, match="would run a program under"):
        subprocess.run(["sh", "-c", "echo ok"], capture_output=True)
