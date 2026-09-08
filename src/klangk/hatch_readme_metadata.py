"""Hatchling metadata hook: long description from the repo-root README (#3349).

The wheel's long description (what renders on the PyPI project page) is the
monorepo root ``README.md`` — two levels above this package dir. Hatchling
refuses a static ``readme = "../../README.md"`` (paths must stay inside the
project dir), so ``pyproject.toml`` declares ``readme`` as ``dynamic`` and
this hook injects its content at metadata-parse time.

Source lookup, in order:

- **repo tree** — ``<repo>/README.md`` (the canonical file; what every build
  from a checkout sees, editable installs included).
- **sdist extraction** — ``<sdist>/README.md``, force-included as a real file
  by the sdist branch of ``hatch_build_package_data.py``. A wheel built from
  an extracted sdist (what ``pip install <sdist>.tar.gz`` does) has no
  surrounding repo, so the shipped copy is the only candidate.
"""

from __future__ import annotations

from pathlib import Path

from hatchling.metadata.plugin.interface import MetadataHookInterface

_README_NAME = "README.md"


class ReadmeMetadataHook(MetadataHookInterface):
    """Populate ``project.readme`` from the repo-root README (#3349)."""

    def _readme_source(self) -> Path:
        # ``self.root`` is the project dir (``src/klangk``); two levels up is
        # the repo root in a checkout. An sdist extraction ships the README
        # locally instead (see module docstring).
        repo_readme = Path(self.root).resolve().parent.parent / _README_NAME
        if repo_readme.is_file():
            return repo_readme
        return Path(self.root) / _README_NAME

    def update(self, metadata: dict) -> None:
        source = self._readme_source()
        metadata["readme"] = {
            "text": source.read_text(encoding="utf-8"),
            "content-type": "text/markdown",
        }
