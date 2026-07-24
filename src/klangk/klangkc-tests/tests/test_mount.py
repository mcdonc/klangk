"""Tests for client-side mount spec validation."""

from klangk.cli.mount import validate_env_entry, validate_mount_spec


class TestValidateMountSpec:
    def test_valid_source_dest(self):
        assert validate_mount_spec("/host/path:/container/path") is None

    def test_valid_with_options(self):
        assert validate_mount_spec("/host:/container:ro") is None
        assert validate_mount_spec("/host:/container:ro,z") is None

    def test_valid_named_volume(self):
        assert validate_mount_spec("myvolume:/data") is None

    def test_too_few_parts(self):
        result = validate_mount_spec("nocolon")
        assert result is not None
        assert "expected source:dest" in result

    def test_too_many_parts(self):
        result = validate_mount_spec("a:b:c:d")
        assert result is not None
        assert "expected source:dest" in result

    def test_empty_source(self):
        result = validate_mount_spec(":/container")
        assert result is not None
        assert "source is empty" in result

    def test_relative_dest(self):
        result = validate_mount_spec("/host:relative")
        assert result is not None
        assert "must be absolute" in result

    def test_unknown_option(self):
        result = validate_mount_spec("/host:/container:bogus")
        assert result is not None
        assert "unknown option" in result


class TestValidateEnvEntry:
    def test_valid_key_value(self):
        assert validate_env_entry("FOO=bar") is None

    def test_valid_empty_value(self):
        assert validate_env_entry("EMPTY=") is None

    def test_value_may_contain_equals(self):
        # only the first '=' splits key from value
        assert validate_env_entry("PATH=/usr:/bin") is None

    def test_missing_equals(self):
        result = validate_env_entry("NOEQUALS")
        assert result is not None
        assert "KEY=VALUE" in result

    def test_empty_key(self):
        result = validate_env_entry("=val")
        assert result is not None
        assert "key cannot be empty" in result
