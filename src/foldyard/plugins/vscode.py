"""The ``vscode`` plugin — VS Code attach support in the dev box, gated on a ``[vscode]`` table.

VS Code attaches over the container-engine API (``docker/podman exec`` via the Remote-Containers
extension — no sshd), so the only box-side need is a persisted ``~/.vscode-server`` volume so the
uploaded VS Code Server survives box recreation / ``fy nuke``. The host-side launcher is
``fy code`` (see ``vscode.py``'s ``code()``), which this table also gates.

Without ``[vscode]`` no server volume is mounted (shell-only box). The volume TARGET resolves
from the image HOME via ``FY_BOX_HOME`` that ``box._up`` injects. Stdlib only.

Under an ENFORCING wall the attach also needs egress the box does not otherwise have: the server
inside the box installs ``[vscode] extensions`` itself (gallery query on
``marketplace.visualstudio.com``, VSIX downloads from ``<publisher>.gallery.vsassets.io`` — the
CDN twin ``gallerycdn`` is the fallback the gallery hands out) and
fetches its own server build (``update.code.visualstudio.com`` → ``main.vscode-cdn.net``). Refused,
the install fails SILENTLY — the extensions dir simply stays empty (seen on a consumer: every
listed extension "not found"). So they join ``egress_recommend`` like the agents' installer hosts,
offered per host at the launch verbs. VS Code's telemetry and experiment hosts
(``mobile.events.data.microsoft.com``, ``default.exp-tas.com``) are deliberately NOT offered.
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

    def egress_recommend(self) -> list[dict]:
        """What a walled box needs for `fy code` to install `[vscode] extensions` and fetch the
        server build — after one consented yes per host, instead of an empty extensions dir."""
        if not config.vscode_enabled():
            return []
        return [
            {"host": "marketplace.visualstudio.com", "why": "VS Code attach — extension gallery"},
            {"host": "*.gallery.vsassets.io", "why": "VS Code attach — extension (VSIX) downloads"},
            {
                "host": "*.gallerycdn.vsassets.io",
                "why": "VS Code attach — extension downloads (CDN)",
            },
            {"host": "update.code.visualstudio.com", "why": "VS Code attach — server build lookup"},
            {"host": "main.vscode-cdn.net", "why": "VS Code attach — server build download"},
        ]
