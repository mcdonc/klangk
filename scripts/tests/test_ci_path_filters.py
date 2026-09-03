"""CI path-filter contract tests (#3051).

Pins the ``pull_request`` path filters in the workflow files:

- The seven unit-suite workflows that carry the #2957 ``paths-ignore``
  block must ignore Markdown at every docs location: root ``*.md`` and
  ``docs/**/*.md`` (the GitHub filter cheat sheet documents
  ``docs/**/*.md`` as matching ``docs/README.md`` too — ``**/`` spans
  zero or more directories — while ``*.md`` is root-anchored because
  ``*`` does not cross ``/``). A nested docs-only PR still started the
  full unit suites before this (observed on #3047).
- No ``paths-ignore`` entry anywhere may match
  ``src/containers/workspace/agent-context.md``: it is ``COPY``ed into
  the workspace image, so an .md change there is a build-path change
  and must keep triggering the e2e suites.
- The e2e suites' ``paths:`` allowlists must keep matching that file.

Pure file-content contract — no network, no runners. If one of these
fails, a workflow edit drifted from the policy; fix the workflow, not
the test.

The glob model below implements exactly the constructs these filters
use — ``*``, full-segment ``**``, ``[...]`` classes, literals — and
raises loudly on anything else (negation, ``?``/``+`` quantifiers,
``**`` inside a segment, unclosed classes) so an unsupported future
entry fails the suite instead of being silently mis-evaluated.
"""

import re
from pathlib import Path

import pytest
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

# The docs-ignore entries each of the above must carry (#3051). The
# workflows also keep a belt-and-braces "docs/*.md"; only the entries
# the GitHub cheat sheet guarantees sufficient are required here.
REQUIRED_MD_IGNORES = {"*.md", "docs/**/*.md"}

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
        wf = yaml.safe_load(f)
    if not isinstance(wf, dict):
        raise AssertionError(f"{name}: not a workflow mapping")
    return wf


def pull_request_trigger(wf):
    """Return the ``pull_request`` trigger mapping, or None."""
    # YAML 1.1 parses the bare key `on` as boolean True.
    on = wf.get("on") or wf.get(True) or {}
    pr = on.get("pull_request")
    if pr is None:
        return None
    if not isinstance(pr, dict):
        raise AssertionError(f"pull_request trigger is not a mapping: {pr!r}")
    return pr


def char_class_regex(seg, i):
    """Translate a ``[...]`` class starting at *i*; return
    ``(regex, index of the closing ``]``)``."""
    end = seg.find("]", i)
    if end == -1:
        raise AssertionError(f"unclosed character class in {seg!r}")
    body = seg[i + 1 : end]
    if body.startswith(("!", "^")):
        raise AssertionError(f"negated class not modeled: {seg!r}")
    return "[" + body.replace("\\", "\\\\") + "]", end


def segment_to_regex(seg):
    """Translate one ``/``-free segment: ``*``, ``[...]``, literals."""
    out, i = "", 0
    while i < len(seg):
        c = seg[i]
        if c == "*":
            out += "[^/]*"
        elif c in "?+":
            raise AssertionError(f"unmodeled quantifier in segment {seg!r}")
        elif c == "[":
            cls, i = char_class_regex(seg, i)
            out += cls
        else:
            out += re.escape(c)
        i += 1
    return out


def doublestar_regex(idx, last):
    """Regex for a full-segment ``**`` at *idx* (``last`` = final)."""
    if last:
        return ".*"  # "docs/**" — everything under docs/
    if idx == 0:
        # "**/x" — zero or more leading dirs, bare "x" too.
        return "(?:[^/]*/)*"
    # Mid-path "**": at least one char per collapsed dir,
    # so "a/**/b" matches "a/b" but never "ab".
    return "(?:[^/]+/)*"


def glob_segment_regex(seg, idx, last, pattern):
    """Regex for one glob segment, trailing ``/`` included unless
    *last* (``**`` patterns carry their own separators)."""
    if seg == "**":
        return doublestar_regex(idx, last)
    if "**" in seg:
        raise AssertionError(f"mid-segment '**' not modeled: {pattern!r}")
    return segment_to_regex(seg) + ("" if last else "/")


def glob_to_regex(pattern):
    """Compile a GitHub path-filter glob to a regex over full paths.

    ``*`` matches zero or more non-``/`` characters; a full-segment
    ``**`` matches zero or more directory segments (per the cheat
    sheet, ``docs/**/*.md`` matches ``docs/README.md`` and
    ``docs/a/b.md``; leading ``**/x`` matches ``x`` itself);
    ``[...]`` is a character class; everything else is literal.
    """
    if pattern.startswith("!"):
        raise AssertionError(f"negated entry not modeled: {pattern!r}")
    segs = pattern.split("/")
    out = ""
    for idx, seg in enumerate(segs):
        out += glob_segment_regex(seg, idx, idx == len(segs) - 1, pattern)
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


def test_root_md_glob_is_root_anchored():
    # #2957: root "*.md" must not cross "/" into docs/.
    assert matches("*.md", ROOT_MD)
    assert not matches("*.md", DOCS_TOP_MD)
    assert not matches("*.md", DOCS_NESTED_MD)


def test_docs_doublestar_covers_the_tree_including_its_top():
    # "docs/**/*.md" must cover the whole docs tree INCLUDING its top
    # level (the GitHub cheat sheet lists docs/README.md as a match,
    # because "**/" spans zero directories); "docs/*.md" stays one
    # level deep.
    assert matches("docs/*.md", DOCS_TOP_MD)
    assert not matches("docs/*.md", DOCS_NESTED_MD)
    assert matches("docs/**/*.md", DOCS_TOP_MD)
    assert matches("docs/**/*.md", DOCS_NESTED_MD)


def test_docs_doublestar_never_matches_build_path_md():
    # Belt-and-braces docs/** must never swallow files outside docs/.
    assert not matches("docs/**/*.md", BUILD_PATH_MD)


def test_glob_model_fails_loud_on_unmodeled_constructs():
    # Negation re-includes files — mis-modeling it as a literal would
    # silently invert the contract this suite pins.
    for bad in ("!src/**", "release/v[0-9", "a**b", "**?.md", "x+"):
        with pytest.raises(AssertionError):
            glob_to_regex(bad)


def paths_ignore_offenders(path):
    """This workflow's paths-ignore entries that match BUILD_PATH_MD."""
    pr = pull_request_trigger(load_workflow(path))
    if not pr or "paths-ignore" not in pr:
        return []
    return [p for p in pr["paths-ignore"] if matches(p, BUILD_PATH_MD)]


def test_no_paths_ignore_entry_matches_build_path_md():
    for path in all_workflows():
        offenders = paths_ignore_offenders(path)
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
