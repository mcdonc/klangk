"""Tests for file_service: list, read, write, delete, rename, path traversal."""

import pytest

from backend import file_service, workspace_manager


@pytest.fixture
async def workspace_dir(workspace, user, temp_data_dir):
    """Create the workspace directory on disk and return (user_id, workspace_id, path)."""
    path = workspace_manager.get_workspace_host_path(user["id"], workspace["id"])
    path.mkdir(parents=True, exist_ok=True)
    return user["id"], workspace["id"], path


class TestResolvePathTraversal:
    async def test_normal_path(self, workspace_dir):
        uid, wid, path = workspace_dir
        resolved = file_service.resolve_path(uid, wid, "file.txt")
        assert str(resolved).startswith(str(path))

    async def test_traversal_rejected(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(ValueError, match="traversal"):
            file_service.resolve_path(uid, wid, "../../etc/passwd")

    async def test_dot_path(self, workspace_dir):
        uid, wid, path = workspace_dir
        resolved = file_service.resolve_path(uid, wid, ".")
        assert resolved == path


class TestWriteAndRead:
    async def test_write_and_read_file(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "hello.txt", b"hello world")
        content = file_service.read_file(uid, wid, "hello.txt")
        assert content == "hello world"

    async def test_write_nested_path(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "sub/dir/file.txt", b"nested")
        content = file_service.read_file(uid, wid, "sub/dir/file.txt")
        assert content == "nested"

    async def test_read_nonexistent(self, workspace_dir):
        uid, wid, _ = workspace_dir
        assert file_service.read_file(uid, wid, "nope.txt") is None

    async def test_read_large_file_returns_none(self, workspace_dir):
        uid, wid, path = workspace_dir
        big_file = path / "big.bin"
        big_file.write_bytes(b"x" * 1_100_000)
        assert file_service.read_file(uid, wid, "big.bin") is None

    async def test_read_directory_returns_none(self, workspace_dir):
        uid, wid, path = workspace_dir
        (path / "adir").mkdir()
        assert file_service.read_file(uid, wid, "adir") is None

    async def test_read_binary_with_replacement(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "bin.dat", b"\x80\x81\x82")
        content = file_service.read_file(uid, wid, "bin.dat")
        assert content is not None
        assert "\ufffd" in content  # replacement character

    async def test_read_unreadable_file_returns_none(self, workspace_dir):
        uid, wid, path = workspace_dir
        f = path / "noperm.txt"
        f.write_bytes(b"secret")
        f.chmod(0o000)
        try:
            assert file_service.read_file(uid, wid, "noperm.txt") is None
        finally:
            f.chmod(0o644)  # restore for cleanup


class TestListFiles:
    async def test_list_empty_dir(self, workspace_dir):
        uid, wid, _ = workspace_dir
        entries = file_service.list_files(uid, wid, ".")
        assert entries == []

    async def test_list_with_files(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "a.txt", b"aaa")
        file_service.write_file(uid, wid, "b.txt", b"bbb")
        entries = file_service.list_files(uid, wid, ".")
        names = [e["name"] for e in entries]
        assert "a.txt" in names
        assert "b.txt" in names

    async def test_list_shows_dirs(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "subdir/file.txt", b"x")
        entries = file_service.list_files(uid, wid, ".")
        dirs = [e for e in entries if e["is_dir"]]
        assert any(d["name"] == "subdir" for d in dirs)

    async def test_list_nonexistent_dir(self, workspace_dir):
        uid, wid, _ = workspace_dir
        entries = file_service.list_files(uid, wid, "no_such_dir")
        assert entries == []

    async def test_list_file_as_dir(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "afile.txt", b"data")
        entries = file_service.list_files(uid, wid, "afile.txt")
        assert entries == []

    async def test_list_entry_has_size(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "sized.txt", b"12345")
        entries = file_service.list_files(uid, wid, ".")
        entry = [e for e in entries if e["name"] == "sized.txt"][0]
        assert entry["size"] == 5
        assert entry["is_dir"] is False

    async def test_list_dir_entry_has_no_size(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "d/f.txt", b"x")
        entries = file_service.list_files(uid, wid, ".")
        entry = [e for e in entries if e["name"] == "d"][0]
        assert entry["size"] is None
        assert entry["is_dir"] is True

    async def test_list_entries_sorted(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "c.txt", b"c")
        file_service.write_file(uid, wid, "a.txt", b"a")
        file_service.write_file(uid, wid, "b.txt", b"b")
        entries = file_service.list_files(uid, wid, ".")
        names = [e["name"] for e in entries]
        assert names == sorted(names)

    async def test_list_subdirectory(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "sub/one.txt", b"1")
        file_service.write_file(uid, wid, "sub/two.txt", b"2")
        entries = file_service.list_files(uid, wid, "sub")
        names = [e["name"] for e in entries]
        assert "one.txt" in names
        assert "two.txt" in names


class TestDelete:
    async def test_delete_file(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "doomed.txt", b"bye")
        result = file_service.delete_path(uid, wid, "doomed.txt")
        assert "doomed.txt" in result
        assert file_service.read_file(uid, wid, "doomed.txt") is None

    async def test_delete_directory(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "dir/a.txt", b"a")
        file_service.write_file(uid, wid, "dir/b.txt", b"b")
        file_service.delete_path(uid, wid, "dir")
        entries = file_service.list_files(uid, wid, ".")
        names = [e["name"] for e in entries]
        assert "dir" not in names

    async def test_delete_nonexistent(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(FileNotFoundError):
            file_service.delete_path(uid, wid, "ghost.txt")


class TestRename:
    async def test_rename_file(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "old.txt", b"content")
        file_service.rename_path(uid, wid, "old.txt", "new.txt")
        assert file_service.read_file(uid, wid, "old.txt") is None
        assert file_service.read_file(uid, wid, "new.txt") == "content"

    async def test_rename_to_existing_fails(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "a.txt", b"a")
        file_service.write_file(uid, wid, "b.txt", b"b")
        with pytest.raises(FileExistsError):
            file_service.rename_path(uid, wid, "a.txt", "b.txt")

    async def test_rename_nonexistent_fails(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(FileNotFoundError):
            file_service.rename_path(uid, wid, "nope.txt", "new.txt")

    async def test_rename_into_subdirectory(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "file.txt", b"data")
        file_service.rename_path(uid, wid, "file.txt", "sub/file.txt")
        assert file_service.read_file(uid, wid, "sub/file.txt") == "data"

    async def test_rename_directory(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "olddir/f.txt", b"x")
        file_service.rename_path(uid, wid, "olddir", "newdir")
        assert file_service.read_file(uid, wid, "newdir/f.txt") == "x"
        entries = file_service.list_files(uid, wid, ".")
        names = [e["name"] for e in entries]
        assert "olddir" not in names
        assert "newdir" in names


class TestWriteFile:
    async def test_write_returns_relative_path(self, workspace_dir):
        uid, wid, _ = workspace_dir
        result = file_service.write_file(uid, wid, "out.txt", b"data")
        assert result == "out.txt"

    async def test_write_nested_returns_full_relative_path(self, workspace_dir):
        uid, wid, _ = workspace_dir
        result = file_service.write_file(uid, wid, "a/b/c.txt", b"deep")
        assert result == "a/b/c.txt"

    async def test_write_overwrites_existing(self, workspace_dir):
        uid, wid, _ = workspace_dir
        file_service.write_file(uid, wid, "f.txt", b"old")
        file_service.write_file(uid, wid, "f.txt", b"new")
        assert file_service.read_file(uid, wid, "f.txt") == "new"

    async def test_write_traversal_rejected(self, workspace_dir):
        uid, wid, _ = workspace_dir
        with pytest.raises(ValueError, match="traversal"):
            file_service.write_file(uid, wid, "../../evil.txt", b"bad")
