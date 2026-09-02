"""CI path-filter contract tests (#3051).

Pins the ``pull_request`` path filters in the workflow files:

- The seven unit-suite workflows that carry the #2957 ``paths-ignore``
  block must ignore Markdown files at every docs location (root,
  ``docs/*.md``, ``docs/**/*.md``). GitHub filter patterns are matched
  against the full path relative to the repo root and ``*`` does not
  cross ``/`` — so ``"*.md"`` alone is root-only and a nested
  docs-only PR still started the full unit suites (observed on #3047).
- No ``paths-ignore`` entry anywhere may match
  ``src/containers/workspace/agent-context.md``: it is ``COPY``ed into
  the workspace image, so an .md change there is a build-path change
  and must keep triggering the e2e suites.
- The e2e suites' ``paths:`` allowlists must keep matching that file.

Pure file-content contract — no network, no runners. If one of these
fails, a workflow edit drifted from the policy; fix the workflow, not
the test.
"""

import re
from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent.parent / ".github" / "workflows"

# Workflows carrying the #2957/#3051 docs-ignore block on pull_request.
MD_IGNORE_WORKFLOWS = [
    "backend-tests.yml",
    "codeql.yml",
    "frontend-tests.yml",
    "fuzz-check.yml",
    "macos-smoke.yml",
    "python-deps-audit.yml",
    "sidecar-tests.yml",
]

# The docs-ignore entries each of the above must carry (#3051).
REQUIRED_MD_IGNORES = {"*.md", "docs/*.md", "docs/**/*.md"}

# agent-context.md is COPYed into the workspace image
# (src/containers/workspace/Dockerfile) — a build-path file despite
# the .md extension. It must never be paths-ignored, and the e2e
# allowlists must keep matching it.
BUILD_PATH_MD = "src/containers/workspace/agent-context.md"

E2E_WORKFLOWS = [
    "backend-e2e-tests.yml",
    "cli-e2e-tests.yml",
    "frontend-e2e-tests.yml",
]

# Representative .md paths (real files in the tree) used to pin glob
# semantics: root-only, one level under docs/, nested under docs/.
ROOT_MD = "README.md"
DOCS_TOP_MD = "docs/index.md"
DOCS_NESTED_MD = "docs/features/sandbox.md"


def load_workflow(name):
    with open(WORKFLOWS_DIR / name) as f:
        return yaml.safe_load(f)


def pull_request_trigger(wf):
    """Return the ``pull_request`` trigger mapping, or None."""
    # YAML 1.1 parses the bare key `on` as boolean True.
    on = wf.get("on") or wf.get(True) or {}
    pr = on.get("pull_request")
    return pr if isinstance(pr, dict) else None


def glob_to_regex(pattern):
    """Compile a GitHub path-filter glob to a regex over full paths.

    ``*`` matches zero or more non-``/`` characters, ``**`` crosses
    directory separators, ``?`` is one non-``/`` character, ``[...]``
    is a character class, everything else is literal.
    """
    out, i = "", 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                out += ".*"
                i += 2
            else:
                out += "[^/]*"
                i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        elif c == "[":
            end = pattern.find("]", i)
            out += pattern[i : end + 1].replace("\\", "\\\\")
            i = end + 1
        else:
            out += re.escape(c)
            i += 1
    return re.compile("^" + out + "$")


def matches(pattern, path):
    return glob_to_regex(pattern).match(path) is not None


def all_workflows():
    return sorted(WORKFLOWS_DIR.glob("*.yml"))


def test_unit_workflows_ignore_md_at_every_docs_depth():
    for name in MD_IGNORE_WORKFLOWS:
        pr = pull_request_trigger(load_workflow(name))
        assert pr is not None, f"{name}: no pull_request trigger"
        missing = REQUIRED_MD_IGNORES - set(pr.get("paths-ignore", []))
        assert not missing, f"{name}: paths-ignore missing {sorted(missing)}"


def test_docs_ignore_entries_have_the_intended_semantics():
    # "docs/**" in the entries must never be a catch-all that also
    # swallows files outside docs/.
    assert matches("*.md", ROOT_MD)
    assert not matches("*.md", DOCS_TOP_MD)
    assert not matches("*.md", DOCS_NESTED_MD)
    assert matches("docs/*.md", DOCS_TOP_MD)
    assert not matches("docs/*.md", DOCS_NESTED_MD)
    assert matches("docs/**/*.md", DOCS_NESTED_MD)


def test_no_paths_ignore_entry_matches_build_path_md():
    for path in all_workflows():
        pr = pull_request_trigger(load_workflow(path))
        if not pr or "paths-ignore" not in pr:
            continue
        offenders = [p for p in pr["paths-ignore"] if matches(p, BUILD_PATH_MD)]
        assert not offenders, (
            f"{path.name}: paths-ignore {offenders} would skip CI for "
            f"{BUILD_PATH_MD} — it is COPYed into the workspace image "
            "(#3051)"
        )


def test_e2e_paths_allowlists_still_match_build_path_md():
    for name in E2E_WORKFLOWS:
        pr = pull_request_trigger(load_workflow(name))
        assert pr is not None, f"{name}: no pull_request trigger"
        allowlist = pr.get("paths", [])
        assert any(matches(p, BUILD_PATH_MD) for p in allowlist), (
            f"{name}: no paths entry matches {BUILD_PATH_MD} — an "
            "agent-context.md-only change must still start the e2e suite "
            "(#3051)"
        )
