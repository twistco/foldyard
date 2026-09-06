"""The ``vscode`` plugin — VS Code attach support in the dev box, gated on a ``[vscode]`` table.

VS Code attaches over the container-engine API (``docker/podman exec`` via the Remote-Containers
extension — no sshd), so the only box-side need is a persisted ``~/.vscode-server`` volume so the
uploaded VS Code Server survives box recreation / ``fy nuke``. The Mac-side launcher is
``fy code`` (see ``vscode.py``'s ``code()``), which this table also gates.

Without ``[vscode]`` no server volume is mounted (shell-only box). The volume TARGET resolves
from the image HOME via ``FY_BOX_HOME`` that ``box._up`` injects. Stdlib only.
"""

from __future__ import annotations

from .. import config
from . import Plugin


class VscodePlugin(Plugin):
    name = "vscode"

    def box_args(self, env: dict) -> list[str]:
        if not config.vscode_enabled():
            return []
        box_home = env.get("FY_BOX_HOME") or "/home/vscode"
        return ["-v", f"devbox_vscode_server:{box_home}/.vscode-server"]
