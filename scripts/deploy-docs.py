#!/usr/bin/env python3
"""Deploy versioned documentation to the gh-pages branch with mike (#687).

Zensical has no native versioning support yet; the official bridge is
squidfunk's fork of mike (installed from GitHub in CI — it is not
published on PyPI). This script drives that fork's Python API:

1. sync the local gh-pages branch from origin (creating it on the first
   deploy — without this, deploying would clobber every earlier version),
2. build the docs with ``zensical build``, with ``MIKE_DOCS_VERSION``
   exported so zensical prefixes ``site_url`` with the version,
3. commit ``site/`` into a versioned subdirectory of gh-pages, refresh
   the ``latest`` alias as HTML redirect pages (GitHub Pages does not
   serve symlinks), and rewrite ``versions.json``,
4. write a root ``index.html`` redirecting to the ``latest`` alias.

The version comes from the ``MIKE_DOCS_VERSION`` environment variable
(set by the docs workflow from the pushed tag). Run from the repository
root; the caller pushes gh-pages afterwards.
"""

from __future__ import annotations

import os
import sys

from mike import commands, git_utils, utils

GH_PAGES_BRANCH = "gh-pages"
LATEST_ALIAS = "latest"


def sync_gh_pages() -> None:
    """Create or fast-forward the local gh-pages branch from origin."""
    git_utils.update_from_upstream("origin", GH_PAGES_BRANCH)


def deploy_version(version: str) -> None:
    """Build the site and commit it as a versioned gh-pages deployment."""
    cfg = utils.load_config()
    with commands.deploy(
        cfg,
        version,
        aliases=[LATEST_ALIAS],
        update_aliases=True,
        alias_type=commands.AliasType.redirect,
    ):
        utils.build(None, version)


def set_default_redirect() -> None:
    """Point the gh-pages root index.html at the latest alias."""
    commands.set_default(LATEST_ALIAS)


def main() -> int:
    version = os.environ.get("MIKE_DOCS_VERSION", "")
    if not version:
        print(
            "MIKE_DOCS_VERSION is not set — pass the docs version to "
            "deploy (e.g. 1.2.3)",
            file=sys.stderr,
        )
        return 1

    sync_gh_pages()
    try:
        deploy_version(version)
        set_default_redirect()
    except git_utils.GitEmptyCommit:
        # Re-deploying unchanged docs makes mike roll back the empty
        # commit; that is a successful no-op, not an error.
        print("docs unchanged since the last deploy — nothing to commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
