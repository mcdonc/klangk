"""Tests for the podman CLI wrapper."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import types

from klangk import podman
from _helpers import make_settings

# Instance whose methods the tests exercise (#1468: the ~20 free
# functions became Podman methods; classify/PodmanError stay module-level).
_p = podman.Podman(
    types.SimpleNamespace(
        state=types.SimpleNamespace(settings=make_settings({}))
    )
)

EXEC = "klangk.podman.asyncio.create_subprocess_exec"


def _procs(*results):
    """Build fake subprocess objects, one per (stdout, stderr, rc).

    ``run`` reads output from the temp files it passes as stdout/stderr and
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

    Writes each fake's canned output into the temp files ``run`` passes as
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


# --- classify ---


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
        assert podman.classify(stderr) == expected


# --- run ---


class TestRun:
    async def test_success(self):
        with patch(EXEC, _exec(("hello\n", "", 0))) as m:
            rc, out, err = await _p.run(["version"])
        assert (rc, out, err) == (0, "hello\n", "")
        assert m.call_args.args[0] == "podman"
        assert m.call_args.kwargs["stdin"] is None

    async def test_check_raises_with_classified_status(self):
        with patch(EXEC, _exec(("", "no such container x", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.run(["start", "x"])
        assert exc.value.status == 404
        assert "no such container" in exc.value.message

    async def test_check_raises_empty_stderr_fallback(self):
        with patch(EXEC, _exec(("", "", 5))):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.run(["boom"])
        assert exc.value.status == 500
        assert exc.value.message == "podman boom"

    async def test_no_check_returns_nonzero(self):
        with patch(EXEC, _exec(("", "bad", 2))):
            rc, out, err = await _p.run(["x"], check=False)
        assert rc == 2

    async def test_stdin_data_uses_pipe(self):
        proc = _procs(("", "", 0))[0]
        with patch(EXEC, AsyncMock(return_value=proc)) as m:
            await _p.run(["x"], stdin_data=b"payload")
        assert m.call_args.kwargs["stdin"] is not None
        proc.stdin.write.assert_called_once_with(b"payload")
        proc.stdin.close.assert_called_once()

    async def test_returncode_none_treated_as_zero(self):
        with patch(EXEC, _exec(("ok", "", None))):
            rc, _out, _err = await _p.run(["x"], check=False)
        assert rc == 0

    async def test_timeout_kills_process(self):
        """A hanging podman process is killed after the timeout."""
        proc = _procs(("", "", 0))[0]
        killed = False

        async def slow_wait():
            if not killed:
                await asyncio.sleep(999)

        def do_kill():
            nonlocal killed
            killed = True

        proc.wait = AsyncMock(side_effect=slow_wait)
        proc.kill = MagicMock(side_effect=do_kill)

        with patch(EXEC, AsyncMock(return_value=proc)):
            rc, _out, err = await _p.run(
                ["rm", "-f", "cid"], check=False, timeout=0.1
            )
        assert rc == -1
        proc.kill.assert_called_once()
        assert "timed out" in err

    async def test_timeout_with_check_raises(self):
        """Timeout + check=True raises PodmanError."""
        proc = _procs(("", "", 0))[0]
        killed = False

        async def slow_wait():
            if not killed:
                await asyncio.sleep(999)

        def do_kill():
            nonlocal killed
            killed = True

        proc.wait = AsyncMock(side_effect=slow_wait)
        proc.kill = MagicMock(side_effect=do_kill)

        with patch(EXEC, AsyncMock(return_value=proc)):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.run(["rm", "-f", "cid"], timeout=0.1)
        assert exc.value.status == 500
        assert "timed out" in exc.value.message


# --- containers ---


class TestInspectContainer:
    async def test_missing_returns_none(self):
        with patch(EXEC, _exec(("", "no such container", 1))):
            assert await _p.inspect_container("c") is None

    async def test_found_returns_first(self):
        payload = json.dumps([{"State": {"Running": True}}])
        with patch(EXEC, _exec((payload, "", 0))):
            info = await _p.inspect_container("c")
        assert info["State"]["Running"] is True

    async def test_empty_list_returns_none(self):
        with patch(EXEC, _exec(("[]", "", 0))):
            assert await _p.inspect_container("c") is None


class TestCreateContainer:
    async def test_minimal(self):
        with patch(EXEC, _exec(("abc123\n", "", 0))) as m:
            cid = await _p.create_container("n", "img", replace=False)
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
            await _p.create_container(
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
            await _p.create_container(
                "n", "img", pull="missing", replace=False
            )
        args = _args(m)
        assert "--pull=missing" in args
        assert "--pull=never" not in args

    async def test_annotations_emitted(self):
        with patch(EXEC, _exec(("id\n", "", 0))) as m:
            await _p.create_container(
                "n",
                "img",
                annotations={"klangk.netfilter.rules": "github.com:443"},
                replace=False,
            )
        args = _args(m)
        assert [
            "--annotation",
            "klangk.netfilter.rules=github.com:443",
        ] == args[args.index("--annotation") : args.index("--annotation") + 2]

    async def test_hooks_dir_emitted(self):
        with patch(EXEC, _exec(("id\n", "", 0))) as m:
            await _p.create_container(
                "n", "img", hooks_dir=["/etc/klangk/hooks"], replace=False
            )
        args = _args(m)
        assert ["--hooks-dir", "/etc/klangk/hooks"] == args[
            args.index("--hooks-dir") : args.index("--hooks-dir") + 2
        ]

    async def test_multiple_hooks_dirs_each_emit_a_flag(self):
        # #1770: --hooks-dir overrides (does not append) podman's default
        # hook search paths, so a filtered container passes the klangk dir
        # AND the standard default dirs — one --hooks-dir flag each, in
        # order, so operator createContainer hooks keep running.
        dirs = [
            "/etc/klangk/hooks",
            "/usr/share/containers/oci/hooks.d",
            "/etc/containers/oci/hooks.d",
        ]
        with patch(EXEC, _exec(("id\n", "", 0))) as m:
            await _p.create_container(
                "n", "img", hooks_dir=dirs, replace=False
            )
        args = _args(m)
        assert args.count("--hooks-dir") == 3
        emitted = [
            args[i + 1] for i, a in enumerate(args) if a == "--hooks-dir"
        ]
        assert emitted == dirs

    async def test_no_hooks_dir_or_annotation_by_default(self):
        with patch(EXEC, _exec(("id\n", "", 0))) as m:
            await _p.create_container("n", "img", replace=False)
        args = _args(m)
        assert "--hooks-dir" not in args
        assert "--annotation" not in args
        assert "--cap-drop" not in args

    async def test_cap_drop_emitted(self):
        with patch(EXEC, _exec(("id\n", "", 0))) as m:
            await _p.create_container(
                "n", "img", cap_drop=["NET_ADMIN"], replace=False
            )
        args = _args(m)
        assert ["--cap-drop", "NET_ADMIN"] == args[
            args.index("--cap-drop") : args.index("--cap-drop") + 2
        ]


class TestStartContainer:
    async def test_start(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.start_container("cid")
        assert _args(m) == ["start", "cid"]


class TestExecContainer:
    async def test_basic(self):
        with patch(EXEC, _exec(("out", "", 0))) as m:
            rc, out, err = await _p.exec_container("cid", ["ls", "/"])
        assert _args(m) == ["exec", "cid", "ls", "/"]
        assert (rc, out, err) == (0, "out", "")

    async def test_with_user(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.exec_container("cid", ["id"], user="root")
        assert _args(m) == ["exec", "-u", "root", "cid", "id"]

    async def test_nonzero_returned(self):
        with patch(EXEC, _exec(("", "fail", 1))):
            rc, _out, err = await _p.exec_container("cid", ["false"])
        assert rc == 1
        assert err == "fail"


class TestWaitForContainerReady:
    async def test_returns_when_sentinel_appears(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            result = await _p.wait_for_container_ready("cid")
        assert result is None
        # One podman exec spinning on the sentinel until it exists.
        assert _args(m) == [
            "exec",
            "cid",
            "sh",
            "-c",
            "while [ ! -f /tmp/.klangk-ready ]; do sleep 0.1; done",
        ]

    async def test_raises_when_exec_fails(self):
        # run returns rc=-1 on timeout (check=False); the sentinel never
        # appeared, so wait_for_container_ready raises PodmanError(500).
        with patch(EXEC, _exec(("", "timed out", -1))):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.wait_for_container_ready("cid", timeout=0.5)
        assert exc.value.status == 500
        assert "cid" in exc.value.message
        assert "0.5s" in exc.value.message


class TestExecContainerWithStdin:
    async def test_stdin_data_adds_interactive_flag(self):
        with patch(EXEC, _exec(("ok", "", 0))) as m:
            rc, out, err = await _p.exec_container(
                "cid", ["cat"], stdin_data=b"hello"
            )
        assert _args(m) == ["exec", "-i", "cid", "cat"]
        assert (rc, out) == (0, "ok")

    async def test_timeout_passed(self):
        with patch(EXEC, _exec(("", "", 0))):
            await _p.exec_container("cid", ["ls"], timeout=60.0)

    async def test_extra_env_adds_flags_before_container(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.exec_container(
                "cid",
                ["env"],
                extra_env={"HOME": "/home/x", "FOO": "bar"},
            )
        # -e flags precede the container id and command.
        assert _args(m) == [
            "exec",
            "-e",
            "HOME=/home/x",
            "-e",
            "FOO=bar",
            "cid",
            "env",
        ]

    async def test_extra_env_with_user_and_stdin(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.exec_container(
                "cid",
                ["sh"],
                user="root",
                stdin_data=b"x",
                extra_env={"A": "1"},
            )
        assert _args(m) == [
            "exec",
            "-i",
            "-u",
            "root",
            "-e",
            "A=1",
            "cid",
            "sh",
        ]


class TestExecContainerBytes:
    async def test_returns_raw_bytes(self):
        with patch(EXEC, _exec(("binary\x00data", "", 0))) as m:
            rc, out, err = await _p.exec_container_bytes(
                "cid", ["cat", "/bin/x"]
            )
        assert _args(m) == ["exec", "cid", "cat", "/bin/x"]
        assert rc == 0
        assert isinstance(out, bytes)
        assert out == b"binary\x00data"

    async def test_with_user(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.exec_container_bytes("cid", ["cat", "/f"], user="klangk")
        assert _args(m) == ["exec", "-u", "klangk", "cid", "cat", "/f"]

    async def test_extra_env_adds_flags_before_container(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.exec_container_bytes(
                "cid", ["env"], extra_env={"HOME": "/home/x"}
            )
        assert _args(m) == [
            "exec",
            "-e",
            "HOME=/home/x",
            "cid",
            "env",
        ]

    async def test_nonzero_returned(self):
        with patch(EXEC, _exec(("", "err", 1))):
            rc, out, err = await _p.exec_container_bytes("cid", ["false"])
        assert rc == 1
        assert out == b""
        assert err == "err"

    async def test_timeout(self):
        """Verify run_raw handles timeout the same way as run."""
        mock_proc = _procs(("", "", 0))[0]
        # First call raises TimeoutError (asyncio.wait_for catches it),
        # second call (after kill) returns normally.
        mock_proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError, 0])
        mock_proc.kill = MagicMock()

        def side_effect(*args, **kwargs):
            sf = kwargs.get("stdout")
            ef = kwargs.get("stderr")
            if hasattr(sf, "write"):
                sf.write(b"")
            if hasattr(ef, "write"):
                ef.write(b"")
            return mock_proc

        with patch(EXEC, AsyncMock(side_effect=side_effect)):
            rc, out, err = await _p.exec_container_bytes(
                "cid", ["sleep", "999"], timeout=0.001
            )
        assert rc == -1
        mock_proc.kill.assert_called_once()

    async def test_stdin_data(self):
        with patch(EXEC, _exec(("ok", "", 0))) as m:
            await _p.exec_container("cid", ["cat"], stdin_data=b"input")
        # The -i flag is added for stdin
        assert _args(m) == ["exec", "-i", "cid", "cat"]


class TestRunRaw:
    """Tests for run_raw edge cases not covered by exec_container_bytes."""

    async def test_stdin_data(self):
        with patch(EXEC, _exec(("out", "", 0))):
            rc, out, err = await _p.run_raw(
                ["exec", "cid", "cat"], stdin_data=b"hello"
            )
        assert rc == 0
        assert out == b"out"

    async def test_check_raises_on_error(self):
        with patch(EXEC, _exec(("", "fail", 1))):
            with pytest.raises(podman.PodmanError):
                await _p.run_raw(["exec", "cid", "false"], check=True)


class TestExecContainerStream:
    async def test_yields_chunks(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(
            side_effect=[b"chunk1", b"chunk2", b""]
        )
        mock_proc.wait = AsyncMock(return_value=0)

        with patch(EXEC, AsyncMock(return_value=mock_proc)):
            result = []
            async for chunk in _p.exec_container_stream(
                "cid", ["tar", "-czf", "-"]
            ):
                result.append(chunk)
        assert result == [b"chunk1", b"chunk2"]

    async def test_with_user(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)

        with patch(EXEC, AsyncMock(return_value=mock_proc)) as m:
            async for _ in _p.exec_container_stream(
                "cid", ["cat", "/f"], user="klangk"
            ):
                pass
        assert list(m.call_args.args[1:]) == [
            "exec",
            "-u",
            "klangk",
            "cid",
            "cat",
            "/f",
        ]

    async def test_extra_env_adds_flags_before_container(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=0)

        with patch(EXEC, AsyncMock(return_value=mock_proc)) as m:
            async for _ in _p.exec_container_stream(
                "cid", ["env"], extra_env={"HOME": "/home/x"}
            ):
                pass
        assert list(m.call_args.args[1:]) == [
            "exec",
            "-e",
            "HOME=/home/x",
            "cid",
            "env",
        ]

    async def test_kills_process_on_early_exit(self):
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(
            side_effect=[b"data", b"more", b"more2"]
        )
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=-9)

        with patch(EXEC, AsyncMock(return_value=mock_proc)):
            gen = _p.exec_container_stream("cid", ["cat"])
            await gen.__anext__()
            await gen.aclose()
        mock_proc.kill.assert_called_once()

    async def test_raises_on_nonzero_exit_no_output(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock(return_value=1)

        with patch(EXEC, AsyncMock(return_value=mock_proc)):
            with pytest.raises(podman.PodmanError):
                async for _ in _p.exec_container_stream("cid", ["cat"]):
                    pass

    async def test_nonzero_exit_with_output_does_not_raise(self):
        mock_proc = MagicMock()
        mock_proc.returncode = 1
        mock_proc.stdout = AsyncMock()
        mock_proc.stdout.read = AsyncMock(side_effect=[b"partial", b""])
        mock_proc.wait = AsyncMock(return_value=1)

        with patch(EXEC, AsyncMock(return_value=mock_proc)):
            chunks = []
            async for chunk in _p.exec_container_stream("cid", ["tar"]):
                chunks.append(chunk)
        assert chunks == [b"partial"]


class TestRemoveContainer:
    async def test_force_default(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.remove_container("cid")
        assert _args(m) == ["rm", "-f", "cid"]

    async def test_no_force(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.remove_container("cid", force=False)
        assert _args(m) == ["rm", "cid"]

    async def test_missing_is_ignored(self):
        with patch(EXEC, _exec(("", "no such container", 1))):
            await _p.remove_container("cid")  # no raise

    async def test_other_error_raises(self):
        with patch(EXEC, _exec(("", "in use", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.remove_container("cid")
        assert exc.value.status == 409

    async def test_other_error_empty_stderr_fallback(self):
        with patch(EXEC, _exec(("", "", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.remove_container("cid")
        assert exc.value.message == "podman rm"


class TestListContainers:
    async def test_parses_json(self):
        payload = json.dumps([{"Id": "a", "Labels": {"k": "v"}}])
        with patch(EXEC, _exec((payload, "", 0))) as m:
            result = await _p.list_containers("k=v")
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
            assert await _p.list_containers("k=v") == []


# --- volumes ---


class TestInspectVolume:
    async def test_missing(self):
        with patch(EXEC, _exec(("", "no such volume", 1))):
            assert await _p.inspect_volume("v") is None

    async def test_found(self):
        payload = json.dumps([{"Name": "v", "CreatedAt": "now"}])
        with patch(EXEC, _exec((payload, "", 0))):
            info = await _p.inspect_volume("v")
        assert info["Name"] == "v"

    async def test_empty_list(self):
        with patch(EXEC, _exec(("[]", "", 0))):
            assert await _p.inspect_volume("v") is None


class TestCreateVolume:
    async def test_with_labels(self):
        info = json.dumps([{"Name": "v", "CreatedAt": "t"}])
        with patch(EXEC, _exec(("v\n", "", 0), (info, "", 0))) as m:
            result = await _p.create_volume("v", {"a": "1"})
        assert result["Name"] == "v"
        assert ["--label", "a=1"] == _args(m, 0)[2:4]

    async def test_without_labels(self):
        info = json.dumps([{"Name": "v", "CreatedAt": "t"}])
        with patch(EXEC, _exec(("v\n", "", 0), (info, "", 0))) as m:
            await _p.create_volume("v")
        assert _args(m, 0) == ["volume", "create", "v"]


class TestListVolumes:
    async def test_parses(self):
        payload = json.dumps([{"Name": "v"}])
        with patch(EXEC, _exec((payload, "", 0))):
            assert (await _p.list_volumes("k=v"))[0]["Name"] == "v"

    async def test_empty(self):
        with patch(EXEC, _exec(("", "", 0))):
            assert await _p.list_volumes("k=v") == []


class TestRemoveVolume:
    async def test_success(self):
        with patch(EXEC, _exec(("", "", 0))) as m:
            await _p.remove_volume("v")
        assert _args(m) == ["volume", "rm", "v"]

    async def test_not_found(self):
        with patch(EXEC, _exec(("", "no such volume", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.remove_volume("v")
        assert exc.value.status == 404

    async def test_in_use(self):
        with patch(EXEC, _exec(("", "volume is being used", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.remove_volume("v")
        assert exc.value.status == 409

    async def test_empty_stderr_fallback(self):
        with patch(EXEC, _exec(("", "", 1))):
            with pytest.raises(podman.PodmanError) as exc:
                await _p.remove_volume("v")
        assert exc.value.message == "podman volume rm"
