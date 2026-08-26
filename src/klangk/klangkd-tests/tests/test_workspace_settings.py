"""Unit tests for the per-workspace ``settings`` resolution layer (#864).

Covers :mod:`klangk.workspace_settings` — the validation gate and the
precedence resolver (workspace override > deploy default > none). Pure
function tests (no DB / app_state).
"""

import pytest

from klangk import workspace_settings as ws


# --- validate_settings (full-replace semantics) ---


def test_validate_settings_none_returns_none():
    assert ws.validate_settings(None) is None


def test_validate_settings_empty_returns_none():
    assert ws.validate_settings({}) is None


def test_validate_settings_all_null_returns_none():
    # Full-replace bag where every key is null = empty bag = None.
    assert ws.validate_settings({"idle_timeout": None}) is None


def test_validate_settings_coerces_numeric_strings():
    out = ws.validate_settings(
        {"idle_timeout": "300", "pids_limit": "512", "cpu_limit": "1.5"}
    )
    assert out == {"idle_timeout": 300, "pids_limit": 512, "cpu_limit": 1.5}


def test_validate_settings_nix_boolean():
    # Truthy forms coerce to True.
    for v in (True, "true", "True", 1, "1", "yes", "on"):
        assert ws.validate_settings({"nix": v}) == {"nix": True}
    # Falsy forms (incl. empty string) coerce to False.
    for v in (False, "false", 0, "0", "no", "off", ""):
        assert ws.validate_settings({"nix": v}) == {"nix": False}
    # Garbage is rejected.
    import pytest

    with pytest.raises(ValueError, match="settings.nix="):
        ws.validate_settings({"nix": "maybe"})


def test_validate_settings_allow_sudo_boolean():
    # #2017: the per-workspace sudo knob uses the same bool coercion as
    # nix (truthy / falsy forms, garbage rejected).
    for v in (True, "true", 1, "yes"):
        assert ws.validate_settings({"allow_sudo": v}) == {"allow_sudo": True}
    for v in (False, "false", 0, "no", ""):
        assert ws.validate_settings({"allow_sudo": v}) == {"allow_sudo": False}
    with pytest.raises(ValueError, match="settings.allow_sudo="):
        ws.validate_settings({"allow_sudo": "maybe"})


def test_parse_allow_sudo():
    # #2017: the deploy-wide setting string parses with the same truthy
    # forms the container registry has always honored.
    for v in ("1", "true", "True", "YES", " yes "):
        assert ws.parse_allow_sudo(v) is True
    for v in ("", "0", "false", "no", None):
        assert ws.parse_allow_sudo(v) is False


def test_resolve_allow_sudo_ceiling():
    # #2017: deploy allow_sudo is a ceiling. Unset workspace value
    # follows the deploy posture; an explicit workspace value may only
    # further restrict (False), never enable sudo above the deploy.
    assert ws.resolve_allow_sudo({"settings": {}}, True) is True
    assert ws.resolve_allow_sudo({"settings": {}}, False) is False
    assert ws.resolve_allow_sudo({"settings": None}, True) is True
    assert ws.resolve_allow_sudo(None, True) is True
    # Lock-down wins even on a sudo-enabled deploy.
    assert (
        ws.resolve_allow_sudo({"settings": {"allow_sudo": False}}, True)
        is False
    )
    # A workspace "true" can never raise sudo past a forbidding deploy.
    assert (
        ws.resolve_allow_sudo({"settings": {"allow_sudo": True}}, False)
        is False
    )
    assert (
        ws.resolve_allow_sudo({"settings": {"allow_sudo": True}}, True) is True
    )


def test_validate_settings_preserves_memory_string():
    out = ws.validate_settings({"memory_limit": "2g"})
    assert out == {"memory_limit": "2g"}


def test_validate_settings_drops_null_keeps_others():
    out = ws.validate_settings(
        {"idle_timeout": 300, "cpu_limit": None, "pids_limit": 512}
    )
    assert out == {"idle_timeout": 300, "pids_limit": 512}


def test_validate_settings_rejects_unknown_key():
    with pytest.raises(ValueError, match="Unknown setting 'nonsense'"):
        ws.validate_settings({"nonsense": 1})


def test_validate_settings_lists_known_keys_on_error():
    with pytest.raises(ValueError) as exc:
        ws.validate_settings({"bogus": 1})
    msg = str(exc.value)
    assert "idle_timeout" in msg
    assert "cpu_limit" in msg


def test_validate_settings_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a JSON object"):
        ws.validate_settings(["idle_timeout", 300])  # type: ignore[arg-type]


def test_validate_settings_rejects_non_string_key():
    with pytest.raises(ValueError, match="keys must be strings"):
        ws.validate_settings({1: 300})  # type: ignore[dict-item]


@pytest.mark.parametrize(
    "key,value",
    [
        ("pids_limit", 0),
        ("pids_limit", -5),
        ("bridge_timeout", -1),
        ("bridge_timeout", 0),
    ],
)
def test_validate_settings_rejects_non_positive(key, value):
    # pids_limit and bridge_timeout require a strictly positive int — 0 is
    # not meaningful for either (a 0 pids limit would fork-bomb the workspace
    # instantly; a 0 bridge timeout is nonsense), unlike idle_timeout.
    with pytest.raises(ValueError, match="must be a positive"):
        ws.validate_settings({key: value})


def test_validate_settings_rejects_negative_idle_timeout():
    # idle_timeout: 0 means "never idle out" (the idle reaper guards with
    # `timeout > 0`), so only negatives are rejected — as a non-negative,
    # not a positive, int.
    with pytest.raises(ValueError, match="must be a non-negative"):
        ws.validate_settings({"idle_timeout": -10})


def test_validate_settings_accepts_zero_idle_timeout():
    # 0 = pin the workspace alive forever (never idle out), the per-workspace
    # equivalent of the auto_start boot path pinning a service alive.
    assert ws.validate_settings({"idle_timeout": 0}) == {"idle_timeout": 0}
    # Also coerced from a numeric string.
    assert ws.validate_settings({"idle_timeout": "0"}) == {"idle_timeout": 0}


def test_validate_settings_rejects_non_integer_timeout():
    with pytest.raises(ValueError, match="must be an integer"):
        ws.validate_settings({"idle_timeout": 1.5})


def test_validate_settings_rejects_non_numeric_string_timeout():
    with pytest.raises(ValueError, match="must be an integer"):
        ws.validate_settings({"idle_timeout": "soon"})


def test_validate_settings_rejects_bool_timeout():
    # bool is a subclass of int; the gate rejects it explicitly.
    with pytest.raises(ValueError, match="not a boolean"):
        ws.validate_settings({"idle_timeout": True})


def test_validate_settings_rejects_zero_cpu():
    with pytest.raises(ValueError, match="must be a positive number"):
        ws.validate_settings({"cpu_limit": 0})


def test_validate_settings_rejects_non_numeric_cpu():
    with pytest.raises(ValueError, match="must be a number"):
        ws.validate_settings({"cpu_limit": "fast"})


def test_validate_settings_rejects_bad_memory_unit():
    with pytest.raises(ValueError, match="not a valid memory size"):
        ws.validate_settings({"memory_limit": "2gib"})


def test_validate_settings_rejects_zero_memory():
    with pytest.raises(ValueError, match="not a valid memory size"):
        ws.validate_settings({"memory_limit": "0"})


@pytest.mark.parametrize("val", ["2g", "2G", "512m", "512mb", "1024", "1.5g"])
def test_validate_settings_accepts_memory_forms(val):
    assert ws.validate_settings({"memory_limit": val}) == {"memory_limit": val}


def test_validate_settings_accepts_int_bytes_memory():
    # Bare-bytes int is coerced to a string for podman.
    assert ws.validate_settings({"memory_limit": 524288000}) == {
        "memory_limit": "524288000"
    }


# #2378: tmp_size reuses the memory-size coercer (same podman grammar).
@pytest.mark.parametrize("val", ["2g", "512m", "1.5g", "1024"])
def test_validate_settings_accepts_tmp_size_forms(val):
    assert ws.validate_settings({"tmp_size": val}) == {"tmp_size": val}


def test_validate_settings_rejects_bad_tmp_size_unit():
    with pytest.raises(ValueError, match="not a valid memory size"):
        ws.validate_settings({"tmp_size": "2gib"})


def test_validate_settings_rejects_zero_tmp_size():
    with pytest.raises(ValueError, match="not a valid memory size"):
        ws.validate_settings({"tmp_size": "0"})


# --- validate_settings_patch (merge semantics) ---


def test_patch_rejects_none():
    with pytest.raises(ValueError, match="must not be empty"):
        ws.validate_settings_patch(None)


def test_patch_rejects_empty():
    with pytest.raises(ValueError, match="must not be empty"):
        ws.validate_settings_patch({})


def test_patch_preserves_null_as_deletion_marker():
    out = ws.validate_settings_patch({"cpu_limit": None})
    assert out == {"cpu_limit": None}


def test_patch_coerces_values():
    out = ws.validate_settings_patch(
        {"idle_timeout": "300", "cpu_limit": None}
    )
    assert out == {"idle_timeout": 300, "cpu_limit": None}


def test_patch_rejects_unknown_key():
    with pytest.raises(ValueError, match="Unknown setting 'nope'"):
        ws.validate_settings_patch({"nope": 1})


def test_patch_rejects_bad_value():
    with pytest.raises(ValueError, match="must be a non-negative"):
        ws.validate_settings_patch({"idle_timeout": -1})


def test_patch_rejects_non_dict():
    with pytest.raises(ValueError, match="must be a JSON object"):
        ws.validate_settings_patch("idle_timeout=300")  # type: ignore[arg-type]


# --- resolve + typed resolvers ---


def test_resolve_workspace_override_wins():
    ws_dict = {"settings": {"idle_timeout": 300}}
    assert ws.resolve(ws_dict, "idle_timeout", 60) == 300


def test_resolve_falls_back_to_deploy_default():
    ws_dict = {"settings": {"idle_timeout": 300}}
    assert ws.resolve(ws_dict, "bridge_timeout", 30) == 30


def test_resolve_returns_none_when_neither_set():
    ws_dict = {"settings": {"idle_timeout": 300}}
    assert ws.resolve(ws_dict, "cpu_limit", None) is None


def test_resolve_handles_missing_settings_bag():
    assert ws.resolve({}, "idle_timeout", 60) == 60
    assert ws.resolve({"settings": None}, "idle_timeout", 60) == 60


def test_resolve_handles_none_workspace():
    assert ws.resolve(None, "idle_timeout", 60) == 60


def test_resolve_does_not_merge_override_and_default():
    # Override replaces the default entirely (no merge), like allowed_domains.
    ws_dict = {"settings": {"allowed": ["x"]}}
    assert ws.resolve(ws_dict, "allowed", ["y"]) == ["x"]


def test_typed_resolvers_bind_keys():
    ws_dict = {"settings": {"cpu_limit": 2.0}}
    assert ws.resolve_bridge_timeout(ws_dict, 30.0) == 30.0
    assert ws.resolve_cpu_limit(ws_dict, 1.0) == 2.0
    assert ws.resolve_memory_limit(ws_dict, "1g") == "1g"
    assert ws.resolve_pids_limit(ws_dict, 100) == 100
    assert ws.resolve_tmp_size({"settings": {"tmp_size": "4g"}}, "2g") == "4g"
    assert ws.resolve_tmp_size({"settings": None}, "2g") == "2g"
    assert ws.resolve_tmp_size({"settings": {}}, None) is None


def test_known_settings_has_all_documented_keys():
    assert ws.KNOWN_SETTINGS == frozenset(
        {
            "idle_timeout",
            "bridge_timeout",
            "cpu_limit",
            "memory_limit",
            "pids_limit",
            "tmp_size",
            "nix",
            "allow_sudo",
        }
    )


# --- coercer edge cases (full branch coverage) ---


def test_coerce_int_accepts_integer_valued_float():
    # A float that is a whole number is accepted (300.0 -> 300).
    assert ws.validate_settings({"idle_timeout": 300.0}) == {
        "idle_timeout": 300
    }


def test_coerce_int_rejects_empty_string():
    with pytest.raises(ValueError, match="must not be empty"):
        ws.validate_settings({"idle_timeout": "   "})


def test_coerce_int_rejects_wrong_type():
    # A list (or any non-int/float/str) is rejected.
    with pytest.raises(ValueError, match="must be an integer"):
        ws.validate_settings({"idle_timeout": [300]})  # type: ignore[list-item]


def test_coerce_float_rejects_bool():
    with pytest.raises(ValueError, match="not a boolean"):
        ws.validate_settings({"cpu_limit": True})


def test_coerce_float_rejects_empty_string():
    with pytest.raises(ValueError, match="must not be empty"):
        ws.validate_settings({"cpu_limit": ""})


def test_coerce_float_rejects_wrong_type():
    with pytest.raises(ValueError, match="must be a number"):
        ws.validate_settings({"cpu_limit": [1.5]})  # type: ignore[list-item]


def test_coerce_memory_rejects_wrong_type():
    with pytest.raises(ValueError, match="must be a size string"):
        ws.validate_settings({"memory_limit": ["2g"]})  # type: ignore[list-item]


def test_patch_rejects_non_string_key():
    with pytest.raises(ValueError, match="keys must be strings"):
        ws.validate_settings_patch({1: 300})  # type: ignore[dict-item]
