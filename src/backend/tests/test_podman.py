"""Tests for the podman CLI wrapper."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from klangk_backend import podman

EXEC = "klangk_backend.podman.asyncio.create_subprocess_exec"


def _procs(*results):
    """Build fake subprocess objects, one per (stdout, stderr, rc).

    ``_run`` reads output from the temp files it passes as stdout/stderr and
    awaits ``proc.wait()`` (not ``communicate()``), so each fake carries its
    canned bytes for ``_exec`` to write into those files.
    """
    out = []
    for stdout, stderr, rc in results:
        p = MagicMock()
        p.returncode = rc
        p._canned = (stdout.encode(), stderr.encode())
        p.wait = AsyncMock(return_value=rc)
        p.stdin = MagicMock()
        p.stdin.drain = AsyncMock()
        out.append(p)
    return out


def _exec(*results):
    """An AsyncMock standing in for create_subprocess_exec.

    Writes each fake's canned output into the temp files ``_run`` passes as
    ``stdout``/``stderr`` so the wrapper reads them back as real output.
    """
    procs = iter(_procs(*results))

    def side_effect(*args, **kwargs):
        p = next(procs)
        out_b, err_b = p._canned
        stdout_f, stderr_f = kwargs.get("stdout"), kwargs.get("stderr")
        if hasattr(stdout_f, "write"):
            stdout_f.write(out_b)
        if hasattr(stderr_f, "write"):
            stderr_f.write(err_b)
        return p

    return AsyncMock(side_effect=side_effect)


def _args(mock_exec, call_index=0):
    """The podman args (sans binary) from the Nth create_subprocess_exec."""
    return list(mock_exec.call_args_list[call_index].args[1:])


# --- _classify ---


class TestClassify:
    @pytest.mark.parametrize(
        "stderr,expected",
        [
            ("Error: no such container foo", 404),
            ("layer not found", 404),
            ("no container with name", 404),
            ("volume is being used by a container", 409),
            ("volume already in use", 409),
            ("Error: name is in use", 409),
            ("something else broke", 500),
        ],
    )
    def test_classify(self, stderr, expected):
        assert podman._classify(stderr) == expected


# --- _run ---


class TestRun:
    async def test_success(self):
        with patch(EXEC, _exec(("hello\n", "", 0))) as m:
            rc, out, err = await podman._run(["version"])
        assert (rc, out, err) == (0, "hello\n", "")
        assert m.call_args.args[0] == podman.PODMAN_BIN
        assert m.call_args.kwargs["stdin"] is None

    async def test_check_raises_with_classified_status(self):
        with patch(EXEC, _exec(("", "no such container x", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await podman._run(["start", "x"])
        assert exc.value.status == 404
        assert "no such container" in exc.value.message

    async def test_check_raises_empty_stderr_fallback(self):
        with patch(EXEC, _exec(("", "", 5))):
            with pytest.raises(podman.PodmanError) as exc:
                await podman._run(["boom"])
        assert exc.value.status == 500
        assert exc.value.message == "podman boom"

    async def test_no_check_returns_nonzero(self):
        with patch(EXEC, _exec(("", "bad", 2))):
            rc, out, err = await podman._run(["x"], check=False)
        assert rc == 2

    async def test_stdin_data_uses_pipe(self):
        proc = _procs(("", "", 0))[0]
        with patch(EXEC, AsyncMock(return_value=proc)) as m:
            await podman._run(["x"], stdin_data=b"payload")
        assert m.call_args.kwargs["stdin"] is not None
        proc.stdin.write.assert_called_once_with(b"payload")
        proc.stdin.close.assert_called_once()

    async def test_returncode_none_treated_as_zero(self):
        with patch(EXEC, _exec(("ok", "", None))):
            rc, _out, _err = await podman._run(["x"], check=False)
        assert rc == 0

    async def test_timeout_kills_process(self):
        """proc.wait() exceeding 120s triggers kill."""
        proc = _procs(("", "", 0))[0]
        proc.returncode = -9  # after kill

        def side_effect(*args, **kwargs):
            stdout_f = kwargs.get("stdout")
            stderr_f = kwargs.get("stderr")
            if hasattr(stdout_f, "write"):
                stdout_f.write(b"")
            if hasattr(stderr_f, "write"):
                stderr_f.write(b"")
            return proc

        with patch(EXEC, AsyncMock(side_effect=side_effect)):
            with patch(
                "klangk_backend.podman.asyncio.wait_for",
                side_effect=TimeoutError,
            ):
                rc, _out, _err = await podman._run(["rm", "x"], check=False)
        proc.kill.assert_called_once()
        assert rc == -9


# --- containers ---


class TestInspectContainer:
    async def test_missing_returns_none(self):
        with patch(EXEC, _exec(("", "no such container", 1))):
            assert await podman.inspect_container("c") is None

    async def test_found_returns_first(self):
        payload = json.dumps([{"State": {"Running": True}}])
        with patch(EXEC, _exec((payload, "", 0))):
            info = await podman.inspect_container("c")
        assert info["State"]["Running"] is True

    async def test_empty_list_returns_none(self):
        with patch(EXEC, _exec(("[]", "", 0))):
            assert await podman.inspect_container("c") is None


class TestCreateContainer:
    async def test_minimal(self):
        with patch(EXEC, _exec(("abc123\n", "", 0))) as m:
            cid = await podman.create_container("n", "img", replace=False)
        assert cid == "abc123"
        assert _args(m) == [
            "create",
            "--pull=never",
            "--name",
            "n",
            "img",
        ]

    async def test_all_flags(self):
        with patch(EXEC, _exec(("id\n", "", 0))) as m:
            await podman.create_container(
                "n",
                "img",
                labels={"a": "1"},
                binds=["/h:/c"],
                tmpfs={"/tmp": "rw,size=2g"},
                publish=[(9000, 8000)],
                add_hosts=["host.containers.internal:host-gateway"],
                dns=["8.8.8.8"],
                env=["K=V"],
                init=True,
                interactive=True,
                replace=True,
                userns="keep-id:uid=0,gid=0",
            )
        args = _args(m)
        assert "--replace" in args
        assert "--init" in args
        assert "-i" in args
        assert ["--userns", "keep-id:uid=0,gid=0"] == args[
            args.index("--userns") : args.index("--userns") + 2
        ]
        assert ["--label", "a=1"] == args[
            args.index("--label") : args.index("--label") + 2
        ]
        assert ["-v", "/h:/c"] == args[args.index("-v") : args.index("-v") + 2]
        assert ["--tmpfs", "/tmp:rw,size=2g"] == args[
            args.index("--tmpfs") : args.index("--tmpfs") + 2
        ]
        assert ["-p", "9000:8000"] == args[
            args.index("-p") : args.index("-p") + 2
        ]
        assert ["--add-host", "host.containers.internal:host-gateway"] == args[
            args.index("--add-host") : args.index("--add-host") + 2
        ]
        assert ["--dns", "8.8.8.8"] == args[
            args.index("--dns") : args.index("--dns") + 2
        ]
        assert ["-e", "K=V"] == args[args.index("-e") : args.index("-e") + 2]
        assert args[-1] == "img"

    async def test_pull_policy_override(self):
        with patch(EXEC, _exec(("id\n", "", 0))) as m:
            await podman.create_container(
                "n", "img", pull="missing", replace=False
            )
        args = _args(m)
        assert "--pull=missing" in args
        assert "--pull=never" not in args


class TestStartContainer:
    async def test_start(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await podman.start_container("cid")
        assert _args(m) == ["start", "cid"]


class TestExecContainer:
    async def test_basic(self):
        with patch(EXEC, _exec(("out", "", 0))) as m:
            rc, out, err = await podman.exec_container("cid", ["ls", "/"])
        assert _args(m) == ["exec", "cid", "ls", "/"]
        assert (rc, out, err) == (0, "out", "")

    async def test_with_user(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await podman.exec_container("cid", ["id"], user="root")
        assert _args(m) == ["exec", "-u", "root", "cid", "id"]

    async def test_nonzero_returned(self):
        with patch(EXEC, _exec(("", "fail", 1))):
            rc, _out, err = await podman.exec_container("cid", ["false"])
        assert rc == 1
        assert err == "fail"


class TestRemoveContainer:
    async def test_force_default(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await podman.remove_container("cid")
        assert _args(m) == ["rm", "-f", "cid"]

    async def test_no_force(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await podman.remove_container("cid", force=False)
        assert _args(m) == ["rm", "cid"]

    async def test_missing_is_ignored(self):
        with patch(EXEC, _exec(("", "no such container", 1))):
            await podman.remove_container("cid")  # no raise

    async def test_other_error_raises(self):
        with patch(EXEC, _exec(("", "in use", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await podman.remove_container("cid")
        assert exc.value.status == 409

    async def test_other_error_empty_stderr_fallback(self):
        with patch(EXEC, _exec(("", "", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await podman.remove_container("cid")
        assert exc.value.message == "podman rm"


class TestListContainers:
    async def test_parses_json(self):
        payload = json.dumps([{"Id": "a", "Labels": {"k": "v"}}])
        with patch(EXEC, _exec((payload, "", 0))) as m:
            result = await podman.list_containers("k=v")
        assert result[0]["Id"] == "a"
        assert _args(m) == [
            "ps",
            "-a",
            "--filter",
            "label=k=v",
            "--format",
            "json",
        ]

    async def test_empty_output(self):
        with patch(EXEC, _exec(("  \n", "", 0))):
            assert await podman.list_containers("k=v") == []


# --- volumes ---


class TestInspectVolume:
    async def test_missing(self):
        with patch(EXEC, _exec(("", "no such volume", 1))):
            assert await podman.inspect_volume("v") is None

    async def test_found(self):
        payload = json.dumps([{"Name": "v", "CreatedAt": "now"}])
        with patch(EXEC, _exec((payload, "", 0))):
            info = await podman.inspect_volume("v")
        assert info["Name"] == "v"

    async def test_empty_list(self):
        with patch(EXEC, _exec(("[]", "", 0))):
            assert await podman.inspect_volume("v") is None


class TestCreateVolume:
    async def test_with_labels(self):
        info = json.dumps([{"Name": "v", "CreatedAt": "t"}])
        with patch(EXEC, _exec(("v\n", "", 0), (info, "", 0))) as m:
            result = await podman.create_volume("v", {"a": "1"})
        assert result["Name"] == "v"
        assert ["--label", "a=1"] == _args(m, 0)[2:4]

    async def test_without_labels(self):
        info = json.dumps([{"Name": "v", "CreatedAt": "t"}])
        with patch(EXEC, _exec(("v\n", "", 0), (info, "", 0))) as m:
            await podman.create_volume("v")
        assert _args(m, 0) == ["volume", "create", "v"]


class TestListVolumes:
    async def test_parses(self):
        payload = json.dumps([{"Name": "v"}])
        with patch(EXEC, _exec((payload, "", 0))):
            assert (await podman.list_volumes("k=v"))[0]["Name"] == "v"

    async def test_empty(self):
        with patch(EXEC, _exec(("", "", 0))):
            assert await podman.list_volumes("k=v") == []


class TestRemoveVolume:
    async def test_success(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await podman.remove_volume("v")
        assert _args(m) == ["volume", "rm", "v"]

    async def test_not_found(self):
        with patch(EXEC, _exec(("", "no such volume", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await podman.remove_volume("v")
        assert exc.value.status == 404

    async def test_in_use(self):
        with patch(EXEC, _exec(("", "volume is being used", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await podman.remove_volume("v")
        assert exc.value.status == 409

    async def test_empty_stderr_fallback(self):
        with patch(EXEC, _exec(("", "", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await podman.remove_volume("v")
        assert exc.value.message == "podman volume rm"
