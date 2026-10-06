---
section: Changed
bump: minor
---

- **`[box].env` is your project's environment, and refuses the names foldyard sets in the box.**
  `fy box up` now stops before starting anything when `[box].env` sets a name foldyard owns: `FY_*`,
  `FOLDYARD_*`, the proxy and CA variables (`HTTPS_PROXY`, `NO_PROXY`, `SSL_CERT_FILE`, …), the
  engine socket (`CONTAINER_HOST`, `DOCKER_HOST`, `DOCKER_CONFIG`), the agents' homes and the
  shell basics such as `PATH`. Before, such a name silently replaced the proxy, CA or engine
  wiring, or was silently ignored, and the adoption diff showed it as one harmless-looking line.
  The message names the setting to use instead where there is one (`[box] clean_docker_config`,
  `[box] git_index_split`, `[proxy] no_proxy`, `fy allow add`). `TZ` and `LC_TIME` stay yours to
  set. An `[[inject]]` row's `box_env` already refused the same names.
