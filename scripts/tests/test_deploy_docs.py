"""Contract tests for the versioned-docs deployment (#687).

``scripts/deploy-docs.py`` drives squidfunk's mike fork (Zensical's
official versioning bridge — native versioning does not exist yet, see
https://zensical.org/docs/setup/versioning/) to publish each release's
docs as a versioned subdirectory of the gh-pages branch. These grep-style
tests pin the wiring — in the spirit of ``test_fmtk_harness.py`` — so a
future edit that silently drops a piece (the redirect alias type, the
gh-pages sync that keeps older versions alive, the workflow's full
checkout) is loud.
"""

from __future__ import annotations

import stat
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "deploy-docs.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "docs.yml"
_ZENSICAL_TOML = _REPO_ROOT / "zensical.toml"


def test_zensical_config_declares_version_selector():
    """The theme must render the mike-driven version selector."""
    toml = _ZENSICAL_TOML.read_text()
    assert "[project.extra.version]" in toml, (
        "zensical.toml no longer configures project.extra.version — the "
        "version selector will not render (see #687)"
    )
    assert 'provider = "mike"' in toml, (
        'project.extra.version must set provider = "mike"'
    )
    assert 'default = "latest"' in toml, (
        "project.extra.version must declare which alias is the default "
        "version (the root redirect and version-warning target it)"
    )


def test_script_exists_and_is_executable():
    assert _SCRIPT.is_file(), f"{_SCRIPT} is missing"
    mode = _SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "scripts/deploy-docs.py must be executable"
    first = _SCRIPT.read_text().splitlines()[0]
    assert first.startswith("#!"), "scripts/deploy-docs.py needs a shebang"


def test_script_uses_mike_python_api_with_redirect_aliases():
    """The deploy must keep every prior version and alias safely."""
    script = _SCRIPT.read_text()
    assert "AliasType.redirect" in script, (
        "deploy must use redirect alias pages — GitHub Pages does not serve symlinks"
    )
    assert "AliasType.symlink" not in script, "symlink aliases break on GitHub Pages"
    assert "aliases=[LATEST_ALIAS]" in script, (
        "deploy must refresh the latest alias alongside the version"
    )
    assert "update_aliases=True" in script, (
        "deploy without update_aliases leaves the latest alias stale"
    )
    assert "update_from_upstream" in script, (
        "deploy must sync the local gh-pages branch from origin first — "
        "otherwise every deploy clobbers all earlier versions"
    )
    assert "GitEmptyCommit" in script, (
        "re-deploying unchanged docs raises GitEmptyCommit; it must be "
        "treated as a no-op, not a failure"
    )


def test_script_versions_the_build_and_root_redirect():
    script = _SCRIPT.read_text()
    assert "MIKE_DOCS_VERSION" in script, (
        "deploy must read the version from MIKE_DOCS_VERSION (set by the "
        "docs workflow from the tag)"
    )
    assert "utils.build" in script, (
        "the zensical build must run inside mike's deploy context so "
        "site_url gets the version prefix"
    )
    assert "set_default" in script, (
        "deploy must write a root index.html redirect — without it the "
        "site root 404s under gh-pages serving"
    )


def test_workflow_deploys_via_gh_pages_branch():
    """The workflow pushes a versioned gh-pages branch, not artifacts."""
    workflow = _WORKFLOW.read_text()
    assert "fetch-depth: 0" in workflow, (
        "checkout must fetch all refs so mike sees origin/gh-pages"
    )
    assert "contents: write" in workflow, "pushing gh-pages needs contents: write"
    assert "pages: write" not in workflow, (
        "the pages: write permission belongs to the removed actions/deploy-pages flow"
    )
    assert "id-token: write" not in workflow, (
        "the id-token: write permission belongs to the removed "
        "actions/deploy-pages flow"
    )
    assert "squidfunk/mike.git" in workflow, (
        "CI must install squidfunk's mike fork — the PyPI mike builds via "
        "mkdocs and cannot parse zensical.toml"
    )
    assert "deploy-pages" not in workflow, (
        "docs now deploy from the gh-pages branch, not a workflow artifact"
    )
    assert "upload-pages-artifact" not in workflow, (
        "docs now deploy from the gh-pages branch, not a workflow artifact"
    )
    assert "git push origin gh-pages" in workflow, (
        "the workflow must push the gh-pages branch mike committed to"
    )
    assert "MIKE_DOCS_VERSION:" in workflow, (
        "the workflow must pass the extracted version to deploy-docs.py"
    )
