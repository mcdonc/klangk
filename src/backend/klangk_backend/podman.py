"""Thin async wrapper around the ``podman`` CLI.

Replaces the previous ``aiodocker`` (Docker REST API) dependency so the
backend can drive a *rootless, daemonless* Podman engine with no socket and
no ``podman system service``. Every call shells out via
``asyncio.create_subprocess_exec`` and parses ``--format json``.

Non-zero exits raise :class:`PodmanError` whose ``status`` mimics the
``aiodocker.exceptions.DockerError.status`` codes (404 not-found, 409
conflict/in-use) that the HTTP layer relies on for control flow; anything
else maps to 500.

The binary is configurable via ``KLANGK_PODMAN_BIN`` (defaults to
``podman``) so dev environments can point at ``docker`` instead.
"""

import asyncio
import json
import logging
import tempfile

from . import util

logger = logging.getLogger(__name__)

PODMAN_BIN = util.resolve_env_secret("KLANGK_PODMAN_BIN", "podman")


class PodmanError(Exception):
    """A podman CLI invocation failed.

    ``status`` is an HTTP-like code (404, 409, 500) derived from stderr so
    callers can branch the same way they did on
    ``aiodocker.exceptions.DockerError.status``.
    """

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"[{status}] {message}")


def _classify(stderr: str) -> int:
    """Map podman stderr text to an HTTP-like status code."""
    low = stderr.lower()
    if "no such" in low or "not found" in low or "no container" in low:
        return 404
    if "in use" in low or "being used" in low or "already in use" in low:
        return 409
    return 500


async def _run(
    args: list[str],
    *,
    check: bool = True,
    stdin_data: bytes | None = None,
) -> tuple[int, str, str]:
    """Run ``podman <args>`` and return ``(returncode, stdout, stderr)``.

    When ``check`` is true a non-zero exit raises :class:`PodmanError`.

    Output is captured to temp files rather than ``stdout=PIPE`` +
    ``communicate()``. Lifecycle commands such as ``podman start --publish``
    spawn a long-lived rootless network helper (``pasta``) that inherits the
    child's stdout/stderr. Under uvloop (which uvicorn selects) that helper
    keeps the inherited *pipe* write-end open for the container's lifetime,
    so ``communicate()`` blocks forever waiting for an EOF that never comes.
    A regular file has no such EOF dependency: the helper harmlessly inherits
    a file fd and we read the output after the process exits.
    """
    with (
        tempfile.TemporaryFile() as out_f,
        tempfile.TemporaryFile() as err_f,
    ):
        proc = await asyncio.create_subprocess_exec(
            PODMAN_BIN,
            *args,
            stdin=(asyncio.subprocess.PIPE if stdin_data is not None else None),
            stdout=out_f,
            stderr=err_f,
        )
        if stdin_data is not None:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()
        await proc.wait()
        out_f.seek(0)
        err_f.seek(0)
        out = out_f.read().decode("utf-8", errors="replace")
        err = err_f.read().decode("utf-8", errors="replace")
    rc = proc.returncode or 0
    if check and rc != 0:
        raise PodmanError(_classify(err), err.strip() or f"podman {args[0]}")
    return rc, out, err


# --- Containers ---


async def inspect_container(container_id: str) -> dict | None:
    """Return the inspect dict for a container, or None if it is gone.

    Mirrors ``containers.get(id)`` + ``.show()``: the result carries
    ``["State"]["Running"]`` and ``["Config"]["Labels"]``.
    """
    rc, out, _err = await _run(
        ["container", "inspect", container_id], check=False
    )
    if rc != 0:
        return None
    data = json.loads(out)
    return data[0] if data else None


async def create_container(
    name: str,
    image: str,
    *,
    labels: dict[str, str] | None = None,
    binds: list[str] | None = None,
    tmpfs: dict[str, str] | None = None,
    publish: list[tuple[int, int]] | None = None,
    add_hosts: list[str] | None = None,
    dns: list[str] | None = None,
    env: list[str] | None = None,
    init: bool = False,
    interactive: bool = False,
    pull: str = "never",
    replace: bool = True,
) -> str:
    """Create a container (``podman create``) and return its id.

    ``publish`` is a list of ``(host_port, container_port)`` pairs.
    ``replace=True`` uses ``--replace`` so an existing container with the
    same name is removed first (the ``create_or_replace`` equivalent).

    ``pull`` maps to podman's ``--pull`` policy (default ``never``). With
    ``never`` the image must already be in the local store (or a configured
    additional image store) — the airgapped default, which fails fast instead
    of attempting a registry pull. Set it to ``missing`` to pull from a
    registry when the image isn't present locally.
    """
    args = ["create", f"--pull={pull}", "--name", name]
    if replace:
        args.append("--replace")
    if init:
        args.append("--init")
    if interactive:
        args.append("-i")
    for key, value in (labels or {}).items():
        args += ["--label", f"{key}={value}"]
    for bind in binds or []:
        args += ["-v", bind]
    for path, opts in (tmpfs or {}).items():
        args += ["--tmpfs", f"{path}:{opts}"]
    for host_port, container_port in publish or []:
        args += ["-p", f"{host_port}:{container_port}"]
    for host in add_hosts or []:
        args += ["--add-host", host]
    for server in dns or []:
        args += ["--dns", server]
    for entry in env or []:
        args += ["-e", entry]
    args.append(image)
    _rc, out, _err = await _run(args)
    return out.strip()


async def start_container(container_id: str) -> None:
    """Start a created container (``podman start``)."""
    await _run(["start", container_id])


async def remove_container(container_id: str, *, force: bool = True) -> None:
    """Remove a container (``podman rm [-f]``); never raises on 404.

    Matches the previous ``delete(force=True)`` callers, which treated a
    missing container as already-removed.
    """
    args = ["rm"]
    if force:
        args.append("-f")
    args.append(container_id)
    rc, _out, err = await _run(args, check=False)
    if rc != 0 and _classify(err) != 404:
        raise PodmanError(_classify(err), err.strip() or "podman rm")


async def list_containers(label: str) -> list[dict]:
    """List containers matching ``label`` (``key=value``).

    Each entry carries ``Id`` and ``Labels`` (podman includes labels in the
    ``ps`` JSON, so no extra inspect is needed for adoption).
    """
    _rc, out, _err = await _run(
        ["ps", "-a", "--filter", f"label={label}", "--format", "json"]
    )
    out = out.strip()
    return json.loads(out) if out else []


# --- Volumes ---


async def inspect_volume(name: str) -> dict | None:
    """Return a volume's inspect dict, or None if it does not exist."""
    rc, out, _err = await _run(["volume", "inspect", name], check=False)
    if rc != 0:
        return None
    data = json.loads(out)
    return data[0] if data else None


async def create_volume(
    name: str, labels: dict[str, str] | None = None
) -> dict:
    """Create a labelled volume and return its inspect dict."""
    args = ["volume", "create"]
    for key, value in (labels or {}).items():
        args += ["--label", f"{key}={value}"]
    args.append(name)
    await _run(args)
    info = await inspect_volume(name)
    if info is None:  # pragma: no cover - created then vanished
        raise PodmanError(500, f"volume {name!r} vanished after create")
    return info


async def list_volumes(label: str) -> list[dict]:
    """List volumes matching ``label`` (``key=value``)."""
    _rc, out, _err = await _run(
        ["volume", "ls", "--filter", f"label={label}", "--format", "json"]
    )
    out = out.strip()
    return json.loads(out) if out else []


async def remove_volume(name: str) -> None:
    """Remove a volume (``podman volume rm``).

    Raises :class:`PodmanError` with status 404 (no such volume) or 409
    (in use) so the HTTP layer can map them to responses.
    """
    rc, _out, err = await _run(["volume", "rm", name], check=False)
    if rc != 0:
        raise PodmanError(_classify(err), err.strip() or "podman volume rm")
