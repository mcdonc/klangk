"""Tests for klangk.envexp — bash-style expansion of workspace env
values (#3526) — and its two choke points.

Unit matrix for the expander, the exec-path resolution on
:class:`klangk.podman.Podman` (with the per-container env cache), and
the start-path expansion in ``container.spec.build_env``.
"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from klangk.envexp import (
    EnvExpansionError,
    expand_env_map,
    expand_env_value,
    needs_expansion,
    validate_env_map,
)
from klangk.container.spec import build_env


# --- the expander: reference forms ---


def test_plain_and_braced_references():
    env = {"FOO": "bar", "A_B2": "x"}
    assert expand_env_value("$FOO", env) == "bar"
    assert expand_env_value("${FOO}", env) == "bar"
    assert expand_env_value("$A_B2", env) == "x"
    assert expand_env_value("pre${FOO}post", env) == "prebarpost"
    assert expand_env_value("$FOO$FOO", env) == "barbar"


def test_name_boundaries():
    # $FOOx is the variable FOOx (never FOO + "x").
    assert expand_env_value("$FOOx", {"FOO": "1"}) == ""
    assert expand_env_value("$FOO x", {"FOO": "1"}) == "1 x"


def test_unset_expands_empty_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert expand_env_value("$NOPE", {}) == ""
    assert "NOPE" in caplog.text


def test_defaults():
    env = {"EMPTY": "", "SET": "y"}
    assert expand_env_value("${EMPTY:-d}", env) == "d"
    assert expand_env_value("${EMPTY-d}", env) == ""  # set-but-empty
    assert expand_env_value("${MISSING:-d}", env) == "d"
    assert expand_env_value("${MISSING-d}", env) == "d"
    assert expand_env_value("${SET:-d}", env) == "y"


def test_alternates():
    env = {"EMPTY": "", "SET": "y"}
    assert expand_env_value("${SET:+alt}", env) == "alt"
    assert expand_env_value("${EMPTY:+alt}", env) == ""
    assert expand_env_value("${EMPTY+alt}", env) == "alt"  # set-but-empty
    assert expand_env_value("${MISSING+alt}", env) == ""


def test_required_hard_errors():
    env = {"EMPTY": "", "SET": "y"}
    assert expand_env_value("${SET:?msg}", env) == "y"
    with pytest.raises(EnvExpansionError, match="password required"):
        expand_env_value("${MISSING:?password required}", env)
    with pytest.raises(EnvExpansionError):
        expand_env_value("${EMPTY:?msg}", env)  # :? fires on empty
    # bare ? only fires on unset — a set-but-empty value passes.
    assert expand_env_value("${EMPTY?msg}", env) == ""


def test_nested_words_expand_once():
    env = {"B": "b"}
    assert expand_env_value("${A:-${B:-c}}", env) == "b"
    assert expand_env_value("${A:-${C:-c}}", env) == "c"


def test_no_rescan_of_results():
    # The value of FOO is the literal "$BAR"; it must not re-expand.
    assert expand_env_value("$FOO", {"FOO": "$BAR", "BAR": "no"}) == "$BAR"


# --- the expander: escapes and literals ---


def test_dollar_escapes():
    assert expand_env_value(r"\$FOO", {"FOO": "x"}) == "$FOO"
    assert expand_env_value("$$FOO", {"FOO": "x"}) == "$FOO"
    assert expand_env_value("a$$b", {}) == "a$b"
    assert expand_env_value(r"\$5", {}) == "$5"


def test_non_references_stay_literal():
    assert expand_env_value("$", {}) == "$"
    assert expand_env_value("$1 $- $#", {}) == "$1 $- $#"
    assert expand_env_value("$(whoami)", {}) == "$(whoami)"
    assert expand_env_value("plain", {}) == "plain"


# --- the expander: syntax errors ---


@pytest.mark.parametrize(
    "value",
    ["${FOO", "${}", "${1X}", "${FOO%}", "${FOO:}"],
)
def test_bad_syntax_raises(value):
    with pytest.raises(EnvExpansionError):
        expand_env_value(value, {})


def test_escaped_reference_stays_literal():
    # The escaped $ never starts a reference; the rest is plain text.
    assert expand_env_value(r"\${FOO}", {"FOO": "x"}) == "${FOO}"


def test_syntax_errors_mention_the_raw_value():
    with pytest.raises(EnvExpansionError, match="MISSING"):
        expand_env_value("x=${MISSING", {})


# --- map-level semantics ---


def test_map_earlier_entries_visible_later():
    base = {"PATH": "/usr/bin"}
    out = expand_env_map({"A": "$PATH:/x", "B": "$A/y", "C": "$B/z"}, base)
    assert out == {
        "A": "/usr/bin:/x",
        "B": "/usr/bin:/x/y",
        "C": "/usr/bin:/x/y/z",
    }


def test_map_without_base_env():
    assert expand_env_map({"A": "$B", "B": "x"}) == {"A": "", "B": "x"}


def test_needs_expansion_guard():
    assert needs_expansion(["$A", "b"]) is True
    assert needs_expansion(["b"]) is False
    assert needs_expansion([]) is False


# --- validation (settings-update gate) ---


def test_validate_rejects_syntax_only():
    with pytest.raises(EnvExpansionError, match="'VALUE'"):
        validate_env_map({"VALUE": "${FOO"})
    with pytest.raises(EnvExpansionError):
        validate_env_map({"X": "${}"})
    with pytest.raises(EnvExpansionError):
        validate_env_map({"X": "${A%}"})


def test_validate_accepts_runtime_dependent_forms():
    # Unset references and :? are runtime questions — syntax-valid.
    validate_env_map({"A": "$UNSET", "B": "${ALSO:?err}", "C": "${X:-$Y}"})


# --- exec-path choke point (Podman) ---


def _podman_with_inspect(container_info):
    """A real Podman with a counting fake for ``inspect_container``."""
    from klangk.podman import Podman

    p = Podman(app=None)

    async def fake_inspect(container_id):
        p.inspects += 1
        return container_info

    p.inspects = 0
    p.inspect_container = fake_inspect
    return p


def test_exec_path_expands_against_container_env():
    p = _podman_with_inspect({"Config": {"Env": ["PATH=/usr/bin", "FOO=bar"]}})
    resolved = asyncio.run(
        p._resolve_exec_extra_env("cid", {"A": "$FOO:$PATH", "B": "$A/x"})
    )
    assert resolved == {"A": "bar:/usr/bin", "B": "bar:/usr/bin/x"}
    # The env snapshot is cached: a second exec does not re-inspect.
    asyncio.run(p._resolve_exec_extra_env("cid", {"C": "$FOO"}))
    assert p.inspects == 1


def test_exec_path_skips_expansion_without_references():
    p = _podman_with_inspect(None)
    extra = {"LC_ALL": "C"}
    assert asyncio.run(p._resolve_exec_extra_env("cid", extra)) is extra
    assert p.inspects == 0


def test_exec_path_inspect_failure_degrades_to_empty():
    p = _podman_with_inspect(None)
    resolved = asyncio.run(
        p._resolve_exec_extra_env("cid", {"A": "${FOO:-d}"})
    )
    assert resolved == {"A": "d"}


def test_image_env_reads_podman_and_docker_shapes():
    from klangk.podman import Podman

    p = Podman(app=None)

    def fake_with(info):
        async def fake(args):
            return info

        return fake

    p._inspect_first = fake_with({"Env": ["A=1"]})
    assert asyncio.run(p.image_env("img")) == {"A": "1"}
    p._inspect_first = fake_with({"Config": {"Env": ["B=2"]}})
    assert asyncio.run(p.image_env("img")) == {"B": "2"}
    p._inspect_first = fake_with(None)
    assert asyncio.run(p.image_env("img")) == {}


# --- settings-update gate (request models) ---


def test_api_model_accepts_null_and_expands_later():
    from pydantic import ValidationError

    from klangk.api.workspaces import CreateWorkspaceRequest

    # Explicit null passes the gate untouched.
    req = CreateWorkspaceRequest(name="x", env=None)
    assert req.env is None
    # Runtime-dependent forms are syntax-valid at the gate.
    req = CreateWorkspaceRequest(name="x", env={"A": "${X:-$Y}"})
    assert req.env == {"A": "${X:-$Y}"}
    # Malformed references are a 422 at the gate, never a bricked start.
    with pytest.raises(ValidationError):
        CreateWorkspaceRequest(name="x", env={"A": "${OOPS"})


# --- start-path choke point (spec.build_env) ---


def _fake_app(feature_env=None):
    features = SimpleNamespace(container_env=lambda: dict(feature_env or {}))
    settings = SimpleNamespace(egress_port=8080, terminal_banner="", port=None)
    util = SimpleNamespace(derive_hosting_info=lambda a, b: ("h", "http", "/"))
    return SimpleNamespace(
        state=SimpleNamespace(features=features, settings=settings, util=util)
    )


def test_build_env_expands_extras_against_image_and_klangk_env():
    app = _fake_app(feature_env={"KLANGKWS_FEATURE": "on"})
    env_vars = build_env(
        app,
        "ws-id",
        [],
        "host",  # hosting_hostname
        "http",  # hosting_proto
        "/base",  # hosting_base_path
        "/home/klangk",
        {"MY_PATH": "$PATH:/opt/tool", "URL": "http://$KLANGKWS_WORKSPACE_ID"},
        None,  # ssl_dir
        base_env={"PATH": "/usr/bin"},
    )
    as_map = dict(item.split("=", 1) for item in env_vars)
    assert as_map["MY_PATH"] == "/usr/bin:/opt/tool"
    assert as_map["URL"] == "http://ws-id"
    # Feature env is context too, and plain values pass through.
    app2 = _fake_app(feature_env={"KLANGKWS_FEATURE": "on"})
    env2 = build_env(
        app2,
        "ws",
        [],
        "host",
        "http",
        "/b",
        "/home/klangk",
        {"F": "$KLANGKWS_FEATURE", "P": "plain"},
        None,
        base_env=None,
    )
    as_map2 = dict(item.split("=", 1) for item in env2)
    assert as_map2["F"] == "on"
    assert as_map2["P"] == "plain"
