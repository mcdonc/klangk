"""Tests for client-side mount spec validation."""

from klangkc.mount import validate_mount_spec


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
