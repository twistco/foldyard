"""The host-side daemons listen on LOOPBACK only.

The egress proxy (no client authentication — it injects the project's credentials into requests
bound for its injector hosts) and the GCP token minter used to bind 0.0.0.0: every interface on
the operator's computer, the LAN included. The VM never needed that — Lima's user-mode network
delivers the guest's 192.168.5.2 to the host's 127.0.0.1 (seen live: the hostagent connects from
127.0.0.1), as gvproxy does for host.containers.internal — so all 0.0.0.0 added was the network."""

from __future__ import annotations

import importlib.util

from foldyard import config
from foldyard.plugins import Registry, gcp, proxy

_FULL_TOML = {"proxy": {}, "plugins": {"gcp-metadata": {"project": "acme"}}}


def test_the_egress_proxy_listens_on_loopback_only():
    cfg = config.Config(repo_root=config.repo_root(), worktree="", toml=_FULL_TOML)
    with config.using(cfg):
        cmd = Registry([proxy.ProxyPlugin()], config=cfg).desired_daemons({})["egress-proxy"]["cmd"]
    assert cmd[cmd.index("--listen-host") + 1] == "127.0.0.1"
    assert "0.0.0.0" not in cmd


def test_the_gcp_minter_listens_on_loopback_only(monkeypatch):
    monkeypatch.setenv("GCP_SA_ALLOWLIST", "sa@acme.iam.gserviceaccount.com")
    spec = importlib.util.spec_from_file_location("_fy_minter", gcp.METADATA_DIR / "minter.py")
    assert spec and spec.loader
    minter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(minter)

    bound: list[tuple[str, int]] = []

    class _Server:
        def __init__(self, address, _handler):
            bound.append(address)

        def serve_forever(self):
            return None

    monkeypatch.setattr(minter, "ThreadingHTTPServer", _Server)
    minter.main()
    assert bound and bound[0][0] == "127.0.0.1"
