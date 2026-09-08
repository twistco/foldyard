"""The throwaway base image the live box e2es spin their sibling box from — ONE image, once.

Three e2e files (`test_proxy_box_e2e`, `test_capture_box_e2e`, `test_gcp_metadata_box_e2e`) start a
real container to play the dev box. All they need from it is the contract they exercise: an HTTPS
client and a system CA store the mounted MITM CA can be installed into.

It is DEBIAN, matching the packaged box (`assets/box/Dockerfile`, `debian:trixie-slim` since
ADR-0014's 2026-08-27 amendment). The rig used to run a Fedora base (`quay.io/podman/stable`,
picked back when the packaged box was Fedora too), which meant every box-side line here had to
work on two distros while the product shipped one — and the e2e's whole claim is that it proves
the REAL box wiring. `buildpack-deps:<suite>-curl` is the Debian official image that already
carries curl + ca-certificates, so no box-side install (and therefore no egress) is needed to get
there. The nested-virt docs keep `quay.io/podman/stable`: that rig genuinely runs podman inside
the container, which is the job that image is for.

Pinned in one place so CI's pre-pull list and the tests can't drift apart.
"""

from __future__ import annotations

BOX_IMAGE = "docker.io/library/buildpack-deps:trixie-curl"
