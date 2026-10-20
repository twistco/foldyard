"""MkDocs hooks for the docs site (mkdocs.yml).

The pages under docs/ are written for GitHub and for `fy docs`, so their relative links point at
whatever the repo has: DEVELOPMENT.md beside docs/, a workflow file, a directory like `./adrs/`,
a script in a kit. The site holds only docs/ minus `exclude_docs`, so each such link would be a
dead link — and a failed `--strict` build. Rather than change pages that render correctly where
they are read today, this rewrites links at build time:

- a link that leaves the site (outside docs/, or into an excluded path) → the file on GitHub;
- a link to a directory with an index page (README.md) → that page;
- an absolute GitHub URL (`raw/`, `blob/` or `tree/main/docs/…`) for something the site has →
  the site's own copy. The shipped pages link that way to what the wheel doesn't carry (the
  diagrams, the contributor pages); on the site that would send a reader to GitHub for a page
  that is right here, and show whatever main has rather than what the site was built with — or
  nothing, for a diagram that isn't on main yet;
- everything else is left alone for MkDocs to validate.

Fenced code is left as written. Heading ids follow GitHub's algorithm for the same reason: the pages
link `#degraded--the-switch-is-on-…` (an em dash dropped, both spaces kept) and
`#pluginsname` (from `` `[plugins.<name>]` ``), as GitHub derives them.
"""

from __future__ import annotations

import html
import logging
import posixpath
import re
from pathlib import Path

log = logging.getLogger("mkdocs.hooks.docs_site")

REPO = "https://github.com/twistco/foldyard"
BRANCH = "main"

# In the repo, deliberately not on the site yet (mkdocs.yml's exclude_docs keeps them out). A link
# into one is a warning — a failed --strict build — rather than a quiet GitHub link, so publishing
# them stays a decision: drop the entry here and in exclude_docs, and link them.
UNPUBLISHED = ("talks/",)

# [text](target), and `![alt](target)`, for which the same rewrite is right (an image outside the
# site is served from GitHub raw). The text may span lines and hold bracketed spans one or two
# deep — alt texts name TOML tables (`[machine] firewall`, `[[inject]]`).
_TEXT = r"(?:[^\[\]]|\[\[?[^\[\]]*\]\]?)*"
_LINK = re.compile(rf"(!?\[{_TEXT}\]\()([^)\s]+)(\))")
_OWN_DOCS = re.compile(rf"^{re.escape(REPO)}/(?:raw|blob|tree)/{BRANCH}/docs/([^#?]*?)/?(#.*)?$")


def _is_relative(target: str) -> bool:
    return not (
        target.startswith(("#", "/", "mailto:")) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)
    )


def _on_site(files, src_uri: str) -> bool:
    """A file MkDocs will publish. Excluded files stay in the collection (with a URL that 404s),
    so presence alone isn't enough."""
    f = files.get_file_from_path(src_uri)
    return f is not None and f.inclusion.is_included()


def _rewrite(
    target: str, page_src: str, docs_root: Path, repo_root: Path, files, image: bool
) -> str:
    page_dir = posixpath.dirname(page_src)
    path, sep, frag = target.partition("#")
    if not path:
        return target
    in_docs = posixpath.normpath(posixpath.join(page_dir, path))  # relative to docs/
    repo_rel = posixpath.normpath(posixpath.join("docs", in_docs))  # relative to the repo
    if repo_rel.startswith("../"):
        return target  # outside the repo: not ours to fix

    inside_docs = not in_docs.startswith("../")
    if inside_docs:
        if (in_docs + "/").startswith(UNPUBLISHED):
            log.warning("%s links %s, which isn't published on the site yet", page_src, target)
            return target
        if _on_site(files, in_docs):
            return target  # a page or asset on the site
        index = posixpath.join(in_docs, "README.md")
        if _on_site(files, index) and (docs_root / in_docs).is_dir():
            # The directory's index page, relative to the linking page.
            rel = posixpath.relpath(index, page_dir or ".")
            return rel + sep + frag

    on_disk = repo_root / repo_rel
    if not on_disk.exists():
        return target  # a genuinely broken link: let --strict report it
    if image:
        return f"{REPO}/raw/{BRANCH}/{repo_rel}"
    kind = "tree" if on_disk.is_dir() else "blob"
    return f"{REPO}/{kind}/{BRANCH}/{repo_rel}" + sep + frag


def github_slug(value: str, separator: str = "-") -> str:
    """A heading's id as GitHub makes it: lowercase, drop everything but word characters, spaces
    and hyphens, and make each space a hyphen. toc hands over the heading as plain text already
    (`[plugins.<name>]`, not markup), so nothing that looks like a tag may be stripped — GitHub
    keeps that `name`."""
    text = html.unescape(value).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", separator)


def on_config(config):
    config["mdx_configs"].setdefault("toc", {})["slugify"] = github_slug
    return config


def on_page_markdown(markdown, page, config, files):
    docs_root = Path(config["docs_dir"])
    repo_root = docs_root.parent

    def fix(m: re.Match) -> str:
        own = _OWN_DOCS.match(m.group(2))
        if own:
            path, frag = own.group(1), own.group(2) or ""
            if not _on_site(files, path):
                path = posixpath.join(path, "README.md")  # a directory: its index page
            if _on_site(files, path):
                rel = posixpath.relpath(path, posixpath.dirname(page.file.src_uri) or ".")
                return m.group(1) + rel + frag + m.group(3)
        if not _is_relative(m.group(2)):
            return m.group(0)
        image = m.group(1).startswith("!")
        new = _rewrite(m.group(2), page.file.src_uri, docs_root, repo_root, files, image)
        return m.group(1) + new + m.group(3)

    # Split on fences and rewrite only the prose between them (even indices).
    parts = re.split(r"(^(?:```|~~~).*?^(?:```|~~~)[^\n]*$)", markdown, flags=re.M | re.S)
    for i in range(0, len(parts), 2):
        parts[i] = _LINK.sub(fix, parts[i])
    return "".join(parts)
