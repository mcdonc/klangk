"""Tests for klangk sandbox config loading and path resolution."""

from unittest.mock import patch

import pytest
import yaml

from klangk.cli.sandbox import (
    SandboxConfig,
    build_all_mounts,
    build_copy_pairs,
    expand_container_path,
    expand_host_path,
    load_sandbox_config,
    parse_copy_spec,
    resolve_setup_command,
    validate_copy_specs,
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

    def test_copy_spec_no_colon_rejected_at_load(self, sandbox_root):
        _write_config(sandbox_root, {"copy": ["notes.txt"]})
        with pytest.raises(ValueError, match="Invalid copy spec"):
            load_sandbox_config(sandbox_root)

    def test_copy_spec_extra_segment_rejected_at_load(self, sandbox_root):
        """A mount-style :ro suffix must not be silently dropped (#3119)."""
        _write_config(sandbox_root, {"copy": ["notes.txt:~/notes.txt:ro"]})
        with pytest.raises(ValueError, match="Invalid copy spec"):
            load_sandbox_config(sandbox_root)

    def test_copy_spec_empty_source_rejected_at_load(self, sandbox_root):
        """An empty source resolves to the sandbox root dir (#3119)."""
        _write_config(sandbox_root, {"copy": [":~/notes.txt"]})
        with pytest.raises(ValueError, match="Invalid copy spec"):
            load_sandbox_config(sandbox_root)

    def test_copy_spec_non_string_rejected_at_load(self, sandbox_root):
        """A YAML int/null entry must fail as a config error, not a
        traceback (fresh-eyes review of #3119)."""
        _write_config(sandbox_root, {"copy": [42]})
        with pytest.raises(ValueError, match="Invalid copy spec"):
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


class TestParseCopySpec:
    def test_simple(self):
        assert parse_copy_spec("a.txt:b.txt") == ("a.txt", "b.txt")

    def test_no_colon_raises(self):
        with pytest.raises(ValueError, match="Invalid copy spec"):
            parse_copy_spec("no-colon")

    def test_extra_segment_raises(self):
        with pytest.raises(ValueError, match="Invalid copy spec"):
            parse_copy_spec("a.txt:b.txt:ro")

    def test_empty_source_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            parse_copy_spec(":dest")

    def test_empty_dest_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            parse_copy_spec("src:")

    def test_both_empty_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            parse_copy_spec(":")


class TestValidateCopySpecs:
    def test_non_string_raises(self):
        with pytest.raises(ValueError, match="must be a string"):
            validate_copy_specs([42])

    def test_valid_specs_pass(self):
        validate_copy_specs(["a:b", "~/.gitconfig:~/.gitconfig"])


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

    def test_extra_segment_raises(self, sandbox_root):
        """Direct construction bypasses load-time validation (#3119)."""
        config = SandboxConfig(copy=["notes.txt:~/notes.txt:ro"])
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


class TestCopySandboxFiles:
    """copy_sandbox_files derives container parents POSIX-style (#3117).

    The destination is a container (POSIX) path; the host ``Path``
    flavor must never touch it — on a Windows CLI host
    ``WindowsPath('/home/admin/sub dir').parent`` yields
    backslash-flavored parents and drive-letter semantics.
    """

    async def test_parent_is_posix_with_spaces(self, tmp_path):
        from klangk.cli import sandboxcmd

        src = tmp_path / "file.txt"
        src.write_text("data")
        config = SandboxConfig(copy=[f"{src}:~/sub dir/file.txt"])
        commands = []

        async def fake_exec_on_ws(ws, cmd, **kwargs):
            commands.append(cmd)
            return 0

        with patch.object(sandboxcmd, "exec_on_ws", fake_exec_on_ws):
            await sandboxcmd.copy_sandbox_files(
                None, config, tmp_path, "admin"
            )

        assert len(commands) == 1
        sh_cmd = commands[0][2]
        # Parent derived POSIX-style and both paths quoted (#3093).
        assert "mkdir -p '/home/admin/sub dir'" in sh_cmd
        assert "cat > '/home/admin/sub dir/file.txt'" in sh_cmd

    async def test_parent_of_root_level_dest(self, tmp_path):
        from klangk.cli import sandboxcmd

        src = tmp_path / "file.txt"
        src.write_text("data")
        config = SandboxConfig(copy=[f"{src}:/opt/file.txt"])
        commands = []

        async def fake_exec_on_ws(ws, cmd, **kwargs):
            commands.append(cmd)
            return 0

        with patch.object(sandboxcmd, "exec_on_ws", fake_exec_on_ws):
            await sandboxcmd.copy_sandbox_files(
                None, config, tmp_path, "admin"
            )

        sh_cmd = commands[0][2]
        assert "mkdir -p /opt && cat > /opt/file.txt" in sh_cmd

    async def test_parent_of_relative_dest_is_dot(self, tmp_path):
        """A bare relative dest passes through build_copy_pairs
        unexpanded (expand_container_path gets no mount_at there), so
        its parent must be ``.`` — an empty parent makes mkdir fail
        and the && chain silently skip the copy (#3117 review)."""
        from klangk.cli import sandboxcmd

        src = tmp_path / "file.txt"
        src.write_text("data")
        config = SandboxConfig(copy=[f"{src}:file.txt"])
        commands = []

        async def fake_exec_on_ws(ws, cmd, **kwargs):
            commands.append(cmd)
            return 0

        with patch.object(sandboxcmd, "exec_on_ws", fake_exec_on_ws):
            await sandboxcmd.copy_sandbox_files(
                None, config, tmp_path, "admin"
            )

        sh_cmd = commands[0][2]
        assert "mkdir -p . && cat > file.txt" in sh_cmd
