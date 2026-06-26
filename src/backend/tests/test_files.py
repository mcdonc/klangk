"""Tests for files: exec-based list, read, write, delete, rename, path validation."""

from unittest.mock import AsyncMock, patch

import pytest

from klangk_backend import files

CID = "test-container-123"
EXEC = "klangk_backend.files.podman.exec_container"
EXEC_STREAM = "klangk_backend.files.podman.exec_container_stream"


class TestValidatePath:
    def test_absolute_path(self):
        assert (
            files.validate_path("/home/work/foo.txt") == "/home/work/foo.txt"
        )

    def test_root(self):
        assert files.validate_path("/") == "/"

    def test_rejects_relative_path(self):
        with pytest.raises(ValueError, match="absolute"):
            files.validate_path("work/foo.txt")

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="Null byte"):
            files.validate_path("/home/\x00evil")

    def test_normalizes_dotdot(self):
        assert files.validate_path("/home/work/../foo") == "/home/foo"

    def test_normalizes_double_slash(self):
        assert files.validate_path("//home//work") == "/home/work"

    def test_normalizes_dot(self):
        assert files.validate_path("/home/./work") == "/home/work"

    def test_filename_too_long(self):
        long_name = "a" * 256
        with pytest.raises(ValueError, match="limit"):
            files.validate_path(f"/home/{long_name}")

    def test_255_byte_filename_ok(self):
        name = "a" * 255
        assert files.validate_path(f"/home/{name}") == f"/home/{name}"

    def test_dotdot_at_root_collapses_to_root(self):
        assert files.validate_path("/../../etc/passwd") == "/etc/passwd"

    def test_injection_semicolon_passes_validation(self):
        # Semicolons are harmless in argv-based exec, so validate_path
        # allows them — the container boundary is the sandbox.
        result = files.validate_path("/home/; rm -rf")
        assert result == "/home/; rm -rf"

    def test_injection_dollar_passes_validation(self):
        result = files.validate_path("/home/$(whoami)")
        assert result == "/home/$(whoami)"

    def test_injection_backtick_passes_validation(self):
        result = files.validate_path("/home/`id`")
        assert result == "/home/`id`"

    def test_injection_pipe_passes_validation(self):
        result = files.validate_path("/home/file | cat /etc/shadow")
        assert result == "/home/file | cat /etc/shadow"

    def test_path_starting_with_dash(self):
        # Paths starting with - are valid; --  separator in commands
        # prevents flag injection.
        result = files.validate_path("/-rf")
        assert result == "/-rf"


class TestListFiles:
    async def test_list_files(self):
        find_output = (
            "a.txt\tf\t100\t1700000000.0\t1700000001.0\n"
            "subdir\td\t4096\t1700000002.0\t1700000003.0\n"
        )
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ) as mock:
            entries = await files.list_files(CID, "/home/work")

        mock.assert_called_once()
        assert mock.call_args.kwargs["user"] == "klangk"
        assert len(entries) == 2
        assert entries[0]["name"] == "a.txt"
        assert entries[0]["path"] == "/home/work/a.txt"
        assert entries[0]["is_dir"] is False
        assert entries[0]["size"] == 100
        assert entries[1]["name"] == "subdir"
        assert entries[1]["path"] == "/home/work/subdir"
        assert entries[1]["is_dir"] is True
        assert entries[1]["size"] is None

    async def test_list_root(self):
        find_output = "home\td\t4096\t1700000000.0\t1700000000.0\n"
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/")

        assert entries[0]["path"] == "/home"

    async def test_list_empty_dir(self):
        with patch(EXEC, new_callable=AsyncMock, return_value=(0, "", "")):
            entries = await files.list_files(CID, "/home/work")

        assert entries == []

    async def test_list_nonexistent_dir(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(1, "", "No such file")
        ):
            entries = await files.list_files(CID, "/no/such/dir")

        assert entries == []

    async def test_list_sorted(self):
        find_output = (
            "c.txt\tf\t1\t0.0\t0.0\n"
            "a.txt\tf\t1\t0.0\t0.0\n"
            "b.txt\tf\t1\t0.0\t0.0\n"
        )
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/home")

        assert [e["name"] for e in entries] == ["a.txt", "b.txt", "c.txt"]

    async def test_list_rejects_relative_path(self):
        with pytest.raises(ValueError, match="absolute"):
            await files.list_files(CID, "work")

    async def test_list_symlink_to_dir(self):
        """Symlinks to directories show as is_dir=True (find -printf %Y)."""
        find_output = "link\td\t4096\t0.0\t0.0\n"
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/home")
        assert entries[0]["is_dir"] is True

    async def test_list_symlink_to_file(self):
        """Symlinks to files show as is_dir=False (find -printf %Y returns f)."""
        find_output = "link\tf\t100\t0.0\t0.0\n"
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/home")
        assert entries[0]["is_dir"] is False

    async def test_list_broken_symlink(self):
        """Broken symlinks (find -printf %Y returns N) show as is_dir=False."""
        find_output = "broken\tN\t0\t0.0\t0.0\n"
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/home")
        assert entries[0]["is_dir"] is False

    async def test_list_skips_malformed_lines(self):
        find_output = "good.txt\tf\t10\t0.0\t0.0\nbadline\n"
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/home")
        assert len(entries) == 1
        assert entries[0]["name"] == "good.txt"

    async def test_list_handles_bad_size(self):
        find_output = "f.txt\tf\tnotanumber\t0.0\t0.0\n"
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/home")
        assert entries[0]["size"] is None

    async def test_list_handles_bad_mtime(self):
        find_output = "f.txt\tf\t10\tbad\t0.0\n"
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/home")
        assert entries[0]["mtime"] == 0.0

    async def test_list_handles_bad_ctime(self):
        find_output = "f.txt\tf\t10\t0.0\tbad\n"
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, find_output, "")
        ):
            entries = await files.list_files(CID, "/home")
        assert entries[0]["ctime"] == 0.0

    async def test_list_default_path_is_root(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, "", "")
        ) as mock:
            await files.list_files(CID)

        cmd = mock.call_args[0][1]
        assert cmd[1] == "/"


class TestStatPath:
    async def test_stat_file(self):
        with patch(
            EXEC,
            new_callable=AsyncMock,
            return_value=(0, "regular file\t12345", ""),
        ):
            info = await files.stat_path(CID, "/home/work/f.txt")

        assert info == {"is_dir": False, "size": 12345}

    async def test_stat_directory(self):
        with patch(
            EXEC,
            new_callable=AsyncMock,
            return_value=(0, "directory\t4096", ""),
        ):
            info = await files.stat_path(CID, "/home/work")

        assert info == {"is_dir": True, "size": 4096}

    async def test_stat_malformed_output(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, "garbage", "")
        ):
            info = await files.stat_path(CID, "/home")
        assert info is None

    async def test_stat_bad_size(self):
        with patch(
            EXEC,
            new_callable=AsyncMock,
            return_value=(0, "regular file\tnotanumber", ""),
        ):
            info = await files.stat_path(CID, "/f.txt")
        assert info == {"is_dir": False, "size": 0}

    async def test_stat_nonexistent(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(1, "", "No such file")
        ):
            info = await files.stat_path(CID, "/nope")

        assert info is None


class TestReadFile:
    async def test_read_file(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "regular file\t100", ""),  # stat
                (0, "hello world", ""),  # cat
            ]
            content = await files.read_file(CID, "/home/work/hello.txt")

        assert content == "hello world"
        # cat should use -- separator
        cat_cmd = mock.call_args_list[1][0][1]
        assert "--" in cat_cmd

    async def test_read_nonexistent(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(1, "", "No such file")
        ):
            content = await files.read_file(CID, "/nope.txt")

        assert content is None

    async def test_read_too_large(self):
        with patch(
            EXEC,
            new_callable=AsyncMock,
            return_value=(0, "regular file\t2000000", ""),
        ):
            content = await files.read_file(CID, "/big.bin")

        assert content is None

    async def test_read_directory_returns_none(self):
        with patch(
            EXEC,
            new_callable=AsyncMock,
            return_value=(0, "directory\t4096", ""),
        ):
            content = await files.read_file(CID, "/home/work")

        assert content is None

    async def test_read_cat_fails(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "regular file\t100", ""),  # stat ok
                (1, "", "Permission denied"),  # cat fails
            ]
            content = await files.read_file(CID, "/home/noperm.txt")

        assert content is None


class TestStreamFile:
    async def test_stream_file(self):
        async def fake_stream(*a, **kw):
            yield b"chunk1"
            yield b"chunk2"

        with patch(EXEC_STREAM, side_effect=fake_stream) as mock:
            chunks = []
            async for chunk in files.stream_file(CID, "/home/work/image.png"):
                chunks.append(chunk)

        assert chunks == [b"chunk1", b"chunk2"]
        mock.assert_called_once()
        cmd = mock.call_args[0][1]
        assert cmd[0] == "cat"
        assert "--" in cmd
        assert mock.call_args.kwargs["user"] == "klangk"

    async def test_stream_file_rejects_relative(self):
        with pytest.raises(ValueError, match="absolute"):
            async for _ in files.stream_file(CID, "relative.txt"):
                pass  # pragma: no cover


class TestStreamDirTar:
    async def test_stream_dir_tar(self):
        async def fake_stream(*a, **kw):
            yield b"\x1f\x8b"
            yield b"tardata"

        with patch(EXEC_STREAM, side_effect=fake_stream) as mock:
            chunks = []
            async for chunk in files.stream_dir_tar(CID, "/home/work/mydir"):
                chunks.append(chunk)

        assert chunks == [b"\x1f\x8b", b"tardata"]
        cmd = mock.call_args[0][1]
        assert "tar" in cmd
        assert mock.call_args.kwargs["user"] == "klangk"

    async def test_stream_dir_tar_rejects_relative(self):
        with pytest.raises(ValueError, match="absolute"):
            async for _ in files.stream_dir_tar(CID, "nope"):
                pass  # pragma: no cover


class TestDeletePath:
    async def test_delete_file(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "", ""),  # test -e
                (0, "", ""),  # rm -rf
            ]
            result = await files.delete_path(CID, "/home/work/doomed.txt")

        assert result == "/home/work/doomed.txt"
        rm_cmd = mock.call_args_list[1][0][1]
        assert "--" in rm_cmd
        assert mock.call_args_list[1].kwargs["user"] == "klangk"

    async def test_delete_nonexistent(self):
        with patch(EXEC, new_callable=AsyncMock, return_value=(1, "", "")):
            with pytest.raises(FileNotFoundError):
                await files.delete_path(CID, "/nope.txt")

    async def test_delete_rm_fails(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "", ""),  # test -e ok
                (1, "", "Permission denied"),  # rm fails
            ]
            with pytest.raises(OSError, match="Permission denied"):
                await files.delete_path(CID, "/usr/bin/important")


class TestRenamePath:
    async def test_rename(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "", ""),  # test -e old
                (1, "", ""),  # test -e new (doesn't exist — good)
                (0, "", ""),  # mkdir -p
                (0, "", ""),  # mv
            ]
            result = await files.rename_path(
                CID, "/home/work/old.txt", "/home/work/new.txt"
            )

        assert result == "/home/work/new.txt"
        mv_cmd = mock.call_args_list[3][0][1]
        assert "--" in mv_cmd

    async def test_rename_source_missing(self):
        with patch(EXEC, new_callable=AsyncMock, return_value=(1, "", "")):
            with pytest.raises(FileNotFoundError):
                await files.rename_path(CID, "/nope.txt", "/new.txt")

    async def test_rename_dest_exists(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "", ""),  # test -e old — exists
                (0, "", ""),  # test -e new — also exists
            ]
            with pytest.raises(FileExistsError):
                await files.rename_path(CID, "/old.txt", "/existing.txt")

    async def test_rename_mv_fails(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "", ""),  # test -e old
                (1, "", ""),  # test -e new (doesn't exist)
                (0, "", ""),  # mkdir -p
                (1, "", "Cross-device link"),  # mv fails
            ]
            with pytest.raises(OSError, match="Cross-device"):
                await files.rename_path(CID, "/old.txt", "/mnt/new.txt")


class TestWriteFile:
    async def test_write(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, "", "")
        ) as mock:
            result = await files.write_file(CID, "/home/work/out.txt", b"data")

        assert result == "/home/work/out.txt"
        assert mock.call_args.kwargs["user"] == "klangk"
        assert mock.call_args.kwargs["stdin_data"] == b"data"
        # Path passed as $1 positional arg, not in the sh -c string
        cmd = mock.call_args[0][1]
        assert cmd[0] == "sh"
        assert cmd[1] == "-c"
        assert "/home/work/out.txt" not in cmd[2]  # not in the script
        assert cmd[-1] == "/home/work/out.txt"  # passed as positional arg

    async def test_write_fails(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(1, "", "Read-only")
        ):
            with pytest.raises(OSError, match="Read-only"):
                await files.write_file(CID, "/usr/bin/evil", b"bad")

    async def test_write_rejects_relative_path(self):
        with pytest.raises(ValueError, match="absolute"):
            await files.write_file(CID, "relative.txt", b"data")

    async def test_write_rejects_null_byte(self):
        with pytest.raises(ValueError, match="Null byte"):
            await files.write_file(CID, "/home/\x00evil", b"data")


class TestExecUser:
    """All operations must run as the klangk user."""

    async def test_list_runs_as_klangk(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, "", "")
        ) as mock:
            await files.list_files(CID, "/")
        assert mock.call_args.kwargs["user"] == "klangk"

    async def test_read_runs_as_klangk(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "regular file\t10", ""),
                (0, "content", ""),
            ]
            await files.read_file(CID, "/f.txt")
        for call in mock.call_args_list:
            assert call.kwargs["user"] == "klangk"

    async def test_delete_runs_as_klangk(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [(0, "", ""), (0, "", "")]
            await files.delete_path(CID, "/f.txt")
        for call in mock.call_args_list:
            assert call.kwargs["user"] == "klangk"

    async def test_rename_runs_as_klangk(self):
        with patch(EXEC, new_callable=AsyncMock) as mock:
            mock.side_effect = [
                (0, "", ""),
                (1, "", ""),
                (0, "", ""),
                (0, "", ""),
            ]
            await files.rename_path(CID, "/a.txt", "/b.txt")
        for call in mock.call_args_list:
            assert call.kwargs["user"] == "klangk"

    async def test_write_runs_as_klangk(self):
        with patch(
            EXEC, new_callable=AsyncMock, return_value=(0, "", "")
        ) as mock:
            await files.write_file(CID, "/f.txt", b"x")
        assert mock.call_args.kwargs["user"] == "klangk"

    async def test_stream_file_runs_as_klangk(self):
        async def fake_stream(*a, **kw):
            yield b"x"

        with patch(EXEC_STREAM, side_effect=fake_stream) as mock:
            async for _ in files.stream_file(CID, "/f.bin"):
                pass
        assert mock.call_args.kwargs["user"] == "klangk"

    async def test_stream_dir_tar_runs_as_klangk(self):
        async def fake_stream(*a, **kw):
            yield b"x"

        with patch(EXEC_STREAM, side_effect=fake_stream) as mock:
            async for _ in files.stream_dir_tar(CID, "/dir"):
                pass
        assert mock.call_args.kwargs["user"] == "klangk"
