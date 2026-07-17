"""Tests for klangk sandbox config loading and path resolution."""

import pytest
import yaml

from klangk.cli.sandbox import (
    SandboxConfig,
    build_all_mounts,
    build_copy_pairs,
    expand_container_path,
    expand_host_path,
    load_sandbox_config,
    resolve_setup_command,
)


@pytest.fixture
def sandbox_root(tmp_path):
    """Create a minimal sandbox root with .klangk-sandbox.yaml."""
    return tmp_path


def _write_config(sandbox_root, config):
    config_path = sandbox_root / ".klangk-sandbox.yaml"
    config_path.write_text(yaml.dump(config))


class TestLoadSandboxConfig:
    def test_minimal(self, sandbox_root):
        _write_config(sandbox_root, {})
        config = load_sandbox_config(sandbox_root)
        assert config.image is None
        assert config.mount_at == "~/work"
        assert config.setup is None
        assert config.copy == []
        assert config.mounts == []
        assert config.volumes == []

    def test_full(self, sandbox_root):
        _write_config(
            sandbox_root,
            {
                "workspace": {"image": "my-image"},
                "sandbox": {
                    "mount-at": "~/project",
                    "setup": "setup.sh",
                },
                "copy": ["~/.gitconfig:~/.gitconfig"],
                "mounts": ["/data:~/data:ro"],
                "volumes": ["cache:/cache"],
            },
        )
        config = load_sandbox_config(sandbox_root)
        assert config.image == "my-image"
        assert config.mount_at == "~/project"
        assert config.setup == "setup.sh"
        assert config.copy == ["~/.gitconfig:~/.gitconfig"]
        assert config.mounts == ["/data:~/data:ro"]
        assert config.volumes == ["cache:/cache"]

    def test_setup_timeout_default(self, sandbox_root):
        _write_config(sandbox_root, {})
        config = load_sandbox_config(sandbox_root)
        assert config.setup_timeout == 300

    def test_setup_timeout_custom(self, sandbox_root):
        _write_config(
            sandbox_root,
            {"sandbox": {"setup-timeout": 60}},
        )
        config = load_sandbox_config(sandbox_root)
        assert config.setup_timeout == 60

    def test_setup_timeout_snake_case_fallback(self, sandbox_root):
        _write_config(
            sandbox_root,
            {"sandbox": {"setup_timeout": 120}},
        )
        config = load_sandbox_config(sandbox_root)
        assert config.setup_timeout == 120

    def test_setup_timeout_invalid_raises(self, sandbox_root):
        _write_config(
            sandbox_root,
            {"sandbox": {"setup-timeout": "not-a-number"}},
        )
        with pytest.raises(
            ValueError, match="setup-timeout must be an integer"
        ):
            load_sandbox_config(sandbox_root)

    def test_snake_case_mount_at_fallback(self, sandbox_root):
        """Legacy mount_at key still works for backwards compat."""
        _write_config(
            sandbox_root,
            {"sandbox": {"mount_at": "~/legacy"}},
        )
        config = load_sandbox_config(sandbox_root)
        assert config.mount_at == "~/legacy"

    def test_missing_config_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No sandbox config"):
            load_sandbox_config(tmp_path)

    def test_invalid_yaml_raises(self, sandbox_root):
        config_path = sandbox_root / ".klangk-sandbox.yaml"
        config_path.write_text("not a mapping")
        with pytest.raises(ValueError, match="Invalid sandbox config"):
            load_sandbox_config(sandbox_root)


class TestExpandHostPath:
    def test_absolute(self, tmp_path):
        assert expand_host_path("/data/files", tmp_path) == "/data/files"

    def test_tilde(self, tmp_path):
        import os

        result = expand_host_path("~/.gitconfig", tmp_path)
        assert result == os.path.expanduser("~/.gitconfig")

    def test_relative(self, tmp_path):
        result = expand_host_path("../sibling", tmp_path)
        expected = str((tmp_path / "../sibling").resolve())
        assert result == expected


class TestExpandContainerPath:
    def test_tilde(self):
        assert expand_container_path("~/work", "admin") == "/home/admin/work"

    def test_tilde_alone(self):
        assert expand_container_path("~", "admin") == "/home/admin"

    def test_absolute(self):
        assert expand_container_path("/nix", "admin") == "/nix"

    def test_no_tilde(self):
        assert expand_container_path("relative", "admin") == "relative"

    def test_relative_with_mount_at(self):
        assert (
            expand_container_path(
                "subdir", "admin", mount_at="/home/admin/project"
            )
            == "/home/admin/project/subdir"
        )


class TestBuildAllMounts:
    def test_implicit_sandbox_root(self, sandbox_root):
        config = SandboxConfig()
        mounts = build_all_mounts(config, sandbox_root, "admin")
        assert mounts[0] == f"{sandbox_root.resolve()}:/home/admin/work"

    def test_explicit_mounts_expanded(self, sandbox_root):
        config = SandboxConfig(mounts=["~/.ssh:~/.ssh:ro"])
        mounts = build_all_mounts(config, sandbox_root, "admin")
        import os

        expected_src = os.path.expanduser("~/.ssh")
        assert f"{expected_src}:/home/admin/.ssh:ro" in mounts

    def test_volumes_source_not_expanded(self, sandbox_root):
        config = SandboxConfig(volumes=["nix-store:/nix"])
        mounts = build_all_mounts(config, sandbox_root, "admin")
        assert "nix-store:/nix" in mounts

    def test_custom_mount_at(self, sandbox_root):
        config = SandboxConfig(mount_at="~/myproject")
        mounts = build_all_mounts(config, sandbox_root, "admin")
        assert mounts[0] == f"{sandbox_root.resolve()}:/home/admin/myproject"

    def test_relative_dest_resolved_to_mount_at(self, sandbox_root):
        config = SandboxConfig(mount_at="~/project", mounts=["/data:subdir"])
        mounts = build_all_mounts(config, sandbox_root, "admin")
        assert "/data:/home/admin/project/subdir" in mounts


class TestBuildCopyPairs:
    def test_basic(self, sandbox_root):
        config = SandboxConfig(copy=["~/.gitconfig:~/.gitconfig"])
        pairs = build_copy_pairs(config, sandbox_root, "admin")
        import os

        assert len(pairs) == 1
        assert pairs[0][0] == os.path.expanduser("~/.gitconfig")
        assert pairs[0][1] == "/home/admin/.gitconfig"

    def test_invalid_spec_raises(self, sandbox_root):
        config = SandboxConfig(copy=["no-colon"])
        with pytest.raises(ValueError, match="Invalid copy spec"):
            build_copy_pairs(config, sandbox_root, "admin")


class TestExpandSpec:
    def test_invalid_mount_spec_raises(self, sandbox_root):
        config = SandboxConfig(mounts=["nocolon"])
        with pytest.raises(ValueError, match="Invalid mount spec"):
            build_all_mounts(config, sandbox_root, "admin")


class TestResolveSetupCommand:
    def test_none(self):
        config = SandboxConfig()
        assert resolve_setup_command(config, "admin") is None

    def test_relative(self):
        config = SandboxConfig(setup="setup.sh")
        result = resolve_setup_command(config, "admin")
        assert result == "/home/admin/work/setup.sh"

    def test_absolute(self):
        config = SandboxConfig(setup="/opt/setup.sh")
        result = resolve_setup_command(config, "admin")
        assert result == "/opt/setup.sh"

    def test_custom_mount_at(self):
        config = SandboxConfig(mount_at="~/project", setup="setup.sh")
        result = resolve_setup_command(config, "admin")
        assert result == "/home/admin/project/setup.sh"
