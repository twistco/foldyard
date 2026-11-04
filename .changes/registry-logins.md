---
section: Security
bump: minor
---

- **foldyard's clients on your computer no longer send your registry logins to the VM.** A `podman`
  client asks the VM's API service to pull and build, and sends its logins along: a build sent every
  login in your `auth.json`, whatever it needed, and the service kept them in a temp file a box could
  read while the build ran. So any box could read your registry logins during any `fy up`, `fy build`
  or `fy box build` run from your computer (the docker CLI, if you set it as the engine, did the
  same from `~/.docker/config.json`). Consider rotating them if a box you didn't trust ran
  during a build. foldyard's clients now get none, so images pull anonymously. **A project that pulls
  a private image must now opt in:** export `FY_REGISTRY_AUTH_FILE` at a file holding one narrow
  login, and name it in `[project] compose_env` (`fy docs stack`, Private images). A failed build or start
  says so if you have logins on your computer.
