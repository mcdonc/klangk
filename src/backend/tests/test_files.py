"""Tests for files: list, read, write, delete, rename, path traversal."""

import pytest

from klangk_backend import files, workspaces as ws_mod


@pytest.fixture
async def workspace_dir(workspace, user, temp_data_dir):
    """Create the home directory on disk and return (user_id, workspace_id, path)."""
    path = ws_mod.get_home_host_path(user["id"], workspace["id"])
    path.mkdir(parents=True, exist_ok=True)
    return user["id"], workspace["id"], path


class TestResolvePathTraversal:
    async def test_normal_path(self, workspace_dir):
        uid, wid, path = workspace_dir
        resolved = files.resolve_path(uid, wid, "file.txt")
        assert str(resolved).startswith(str(path))

    async def test_traversal_rejected(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(ValueError, match="traversal"):
            files.resolve_path(uid, wid, "../../etc/passwd")

    async def test_dot_path(self, workspace_dir):
        uid, wid, path = workspace_dir
        resolved = files.resolve_path(uid, wid, ".")
        assert resolved == path


class TestWriteAndRead:
    async def test_write_and_read_file(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "hello.txt", b"hello world")
        content = files.read_file(uid, wid, "hello.txt")
        assert content == "hello world"

    async def test_write_nested_path(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "sub/dir/file.txt", b"nested")
        content = files.read_file(uid, wid, "sub/dir/file.txt")
        assert content == "nested"

    async def test_read_nonexistent(self, workspace_dir):
        uid, wid, _ = workspace_dir
        assert files.read_file(uid, wid, "nope.txt") is None

    async def test_read_large_file_returns_none(self, workspace_dir):
        uid, wid, path = workspace_dir
        big_file = path / "big.bin"
        big_file.write_bytes(b"x" * 1_100_000)
        assert files.read_file(uid, wid, "big.bin") is None

    async def test_read_directory_returns_none(self, workspace_dir):
        uid, wid, path = workspace_dir
        (path / "adir").mkdir()
        assert files.read_file(uid, wid, "adir") is None

    async def test_read_binary_with_replacement(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "bin.dat", b"\x80\x81\x82")
        content = files.read_file(uid, wid, "bin.dat")
        assert content is not None
        assert "\ufffd" in content  # replacement character

    async def test_read_unreadable_file_returns_none(self, workspace_dir):
        uid, wid, path = workspace_dir
        f = path / "noperm.txt"
        f.write_bytes(b"secret")
        f.chmod(0o000)
        try:
            assert files.read_file(uid, wid, "noperm.txt") is None
        finally:
            f.chmod(0o644)  # restore for cleanup


class TestListFiles:
    async def test_list_empty_dir(self, workspace_dir):
        uid, wid, _ = workspace_dir
        entries = files.list_files(uid, wid, ".")
        assert entries == []

    async def test_list_with_files(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "a.txt", b"aaa")
        files.write_file(uid, wid, "b.txt", b"bbb")
        entries = files.list_files(uid, wid, ".")
        names = [e["name"] for e in entries]
        assert "a.txt" in names
        assert "b.txt" in names

    async def test_list_shows_dirs(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "subdir/file.txt", b"x")
        entries = files.list_files(uid, wid, ".")
        dirs = [e for e in entries if e["is_dir"]]
        assert any(d["name"] == "subdir" for d in dirs)

    async def test_list_nonexistent_dir(self, workspace_dir):
        uid, wid, _ = workspace_dir
        entries = files.list_files(uid, wid, "no_such_dir")
        assert entries == []

    async def test_list_file_as_dir(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "afile.txt", b"data")
        entries = files.list_files(uid, wid, "afile.txt")
        assert entries == []

    async def test_list_entry_has_size(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "sized.txt", b"12345")
        entries = files.list_files(uid, wid, ".")
        entry = [e for e in entries if e["name"] == "sized.txt"][0]
        assert entry["size"] == 5
        assert entry["is_dir"] is False

    async def test_list_dir_entry_has_no_size(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "d/f.txt", b"x")
        entries = files.list_files(uid, wid, ".")
        entry = [e for e in entries if e["name"] == "d"][0]
        assert entry["size"] is None
        assert entry["is_dir"] is True

    async def test_list_entries_sorted(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "c.txt", b"c")
        files.write_file(uid, wid, "a.txt", b"a")
        files.write_file(uid, wid, "b.txt", b"b")
        entries = files.list_files(uid, wid, ".")
        names = [e["name"] for e in entries]
        assert names == sorted(names)

    async def test_list_subdirectory(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "sub/one.txt", b"1")
        files.write_file(uid, wid, "sub/two.txt", b"2")
        entries = files.list_files(uid, wid, "sub")
        names = [e["name"] for e in entries]
        assert "one.txt" in names
        assert "two.txt" in names


class TestDelete:
    async def test_delete_file(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "doomed.txt", b"bye")
        result = files.delete_path(uid, wid, "doomed.txt")
        assert "doomed.txt" in result
        assert files.read_file(uid, wid, "doomed.txt") is None

    async def test_delete_directory(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "dir/a.txt", b"a")
        files.write_file(uid, wid, "dir/b.txt", b"b")
        files.delete_path(uid, wid, "dir")
        entries = files.list_files(uid, wid, ".")
        names = [e["name"] for e in entries]
        assert "dir" not in names

    async def test_delete_nonexistent(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(FileNotFoundError):
            files.delete_path(uid, wid, "ghost.txt")


class TestRename:
    async def test_rename_file(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "old.txt", b"content")
        files.rename_path(uid, wid, "old.txt", "new.txt")
        assert files.read_file(uid, wid, "old.txt") is None
        assert files.read_file(uid, wid, "new.txt") == "content"

    async def test_rename_to_existing_fails(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "a.txt", b"a")
        files.write_file(uid, wid, "b.txt", b"b")
        with pytest.raises(FileExistsError):
            files.rename_path(uid, wid, "a.txt", "b.txt")

    async def test_rename_nonexistent_fails(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(FileNotFoundError):
            files.rename_path(uid, wid, "nope.txt", "new.txt")

    async def test_rename_into_subdirectory(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "file.txt", b"data")
        files.rename_path(uid, wid, "file.txt", "sub/file.txt")
        assert files.read_file(uid, wid, "sub/file.txt") == "data"

    async def test_rename_directory(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "olddir/f.txt", b"x")
        files.rename_path(uid, wid, "olddir", "newdir")
        assert files.read_file(uid, wid, "newdir/f.txt") == "x"
        entries = files.list_files(uid, wid, ".")
        names = [e["name"] for e in entries]
        assert "olddir" not in names
        assert "newdir" in names


class TestWriteFile:
    async def test_write_returns_relative_path(self, workspace_dir):
        uid, wid, _ = workspace_dir
        result = files.write_file(uid, wid, "out.txt", b"data")
        assert result == "out.txt"

    async def test_write_nested_returns_full_relative_path(
        self, workspace_dir
    ):
        uid, wid, _ = workspace_dir
        result = files.write_file(uid, wid, "a/b/c.txt", b"deep")
        assert result == "a/b/c.txt"

    async def test_write_overwrites_existing(self, workspace_dir):
        uid, wid, _ = workspace_dir
        files.write_file(uid, wid, "f.txt", b"old")
        files.write_file(uid, wid, "f.txt", b"new")
        assert files.read_file(uid, wid, "f.txt") == "new"

    async def test_write_traversal_rejected(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(ValueError, match="traversal"):
            files.write_file(uid, wid, "../../evil.txt", b"bad")


class TestWriteFilePath:
    async def test_returns_path_and_creates_parents(self, workspace_dir):
        uid, wid, base = workspace_dir
        dest = files.write_file_path(uid, wid, "sub/new.txt")
        assert dest.parent.exists()
        assert str(dest).startswith(str(base))

    async def test_traversal_rejected(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(ValueError, match="traversal"):
            files.write_file_path(uid, wid, "../../evil.txt")
