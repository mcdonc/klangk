"""Build / update the shared nix seed dir (#2225).

Ships as the ``klangk-build-nix-seed`` console script: it locates the seed
Dockerfile bundled in the wheel via ``importlib.resources`` (with a
source-tree fallback for dev), drives the *configured* podman
(``KLANGKD_PODMAN_BIN``, the same binary the server uses), and writes
``<out-dir>/nix`` + ``<out-dir>/nix.conf``. The operator then points
``nix_seed.path`` at the output (fuse: directly; btrfs: load it into a
subvolume first). Works the same from a ``pip install klangk`` deployment
(host container, bare pip) and from a devenv shell — no separate source-tree
script.

Server-side tool (uses klangk's settings + podman subprocess env); not part of
the standalone ``klangk.cli`` client, which must not import the server package.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.resources
import logging
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .podman import subprocess_env
from .settings import KlangkSettings

logger = logging.getLogger("klangk.seed")

# Throwaway build image tag. The image is a build sandbox only — its /nix is
# extracted, the image is not shipped.
IMAGE = "klangk-nix-seed:latest"
# Bundled-data dir inside the wheel: site-packages/klangk/nix-seed/Dockerfile.
_BUNDLE_DIR = "nix-seed"


class SeedError(Exception):
    """A seed build / extract / verify step failed."""


# --- podman resolution -----------------------------------------------------


def resolve_podman_bin(env: Mapping[str, str] | None = None) -> str:
    """The podman binary klangkd is configured to use (``KLANGKD_PODMAN_BIN``).

    Resolved through ``KlangkSettings`` so the tool honours the same setting the
    server uses — not a bare ``podman`` from the devenv shell.
    """
    return KlangkSettings(
        env if env is not None else os.environ
    ).podman_bin or ("podman")


def sig_policy_args() -> list[str]:
    """``--signature-policy`` args when ``CONTAINERS_SIGNATURE_POLICY`` is set.

    Mirrors ``scripts/_podman_common.sh``: Nix's rootless podman ships no
    default ``/etc/containers/policy.json``, so a build that verifies image
    signatures fails in a fresh env (#1230). We pass the flag only when the var
    names a policy (creating a permissive one on first use, as enterShell does).
    """
    pol = os.environ.get("CONTAINERS_SIGNATURE_POLICY", "").strip()
    if not pol:
        return []
    p = Path(pol)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('{"default": [{"type": "insecureAcceptAnything"}]}')
    return ["--signature-policy", str(p)]


# --- subprocess helper -----------------------------------------------------


async def run(
    binary: str,
    args: list[str],
    *,
    timeout: float = 1800.0,
    check: bool = True,
) -> tuple[int, str, str]:
    """Run ``<binary> <args>`` → ``(rc, stdout, stderr)``.

    Standalone sibling of ``Podman.run`` (the seed tools aren't on a request
    path and don't need an app). Used for both podman (the build) and the btrfs
    CLI (the subvolume loader). Long default timeout — the build installs nix +
    devenv. A missing binary surfaces as ``SeedError`` (not a raw
    ``FileNotFoundError``), and a wedged call times out + is killed.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=subprocess_env(),
        )
    except FileNotFoundError as exc:
        raise SeedError(f"{binary} not found on PATH") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        label = args[0] if args else binary
        raise SeedError(f"{binary} {label} timed out after {timeout}s")
    rc = proc.returncode or 0
    out_s = out.decode("utf-8", "replace")
    err_s = err.decode("utf-8", "replace")
    if check and rc != 0:
        raise SeedError(
            f"{binary} {' '.join(args[:2])} failed (rc={rc}): {err_s.strip()}"
        )
    return rc, out_s, err_s


# --- build steps -----------------------------------------------------------


async def build_image(
    podman_bin: str, dockerfile_text: str, *, no_cache: bool
) -> None:
    """Build the seed sandbox image from the bundled Dockerfile text."""
    platform = os.environ.get("KLANGKBUILD_PLATFORM", "").strip()
    with tempfile.TemporaryDirectory() as ctx:
        (Path(ctx) / "Dockerfile").write_text(dockerfile_text)
        args: list[str] = ["build"]
        args += sig_policy_args()
        # Prevent OCI maskedPaths/readonlyPaths overmounts on /proc inside
        # the build container — without this, the kernel rejects proc
        # mounts in nested user namespaces ("VFS: Mount too revealing").
        # Only on nix CI runners (signaled by CONTAINERS_STORAGE_CONF);
        # older podman on ubuntu doesn't support this option.
        if os.environ.get("CONTAINERS_STORAGE_CONF"):
            args += ["--security-opt", "unmask=ALL"]
        if platform:
            args += ["--platform", platform]
        if no_cache:
            args.append("--no-cache")
        args += ["-f", str(Path(ctx) / "Dockerfile"), "-t", IMAGE, ctx]
        logger.info("building seed image (podman build ...)")
        await run(podman_bin, args, timeout=2400.0)


def _extract_seed_members(tar, out: str) -> None:
    """Extract only ``nix`` + ``etc/nix/nix.conf`` members (every member is
    a whitelisted relative path; ``filter="fully_trusted"`` preserves the
    seed's uid-1000 ownership — the workspace klangk user)."""
    for member in tar:
        name = member.name[2:] if member.name.startswith("./") else member.name
        if (
            name == "nix"
            or name.startswith("nix/")
            or name == "etc/nix/nix.conf"
        ):
            tar.extract(member, out, filter="fully_trusted")


def export_to_dir_sync(podman_bin: str, cid: str, out: str) -> None:
    """Stream ``podman export <cid>`` into :mod:`tarfile`, extracting only
    ``nix`` + ``etc/nix/nix.conf`` into *out*.

    Run via :func:`asyncio.to_thread` (sync ``subprocess.Popen`` + blocking
    ``tarfile`` reads). Streaming keeps peak memory at the read buffer (the
    seed is ~3 GB); the store files land read-only (0444/0555) and are made
    writable afterwards. ``filter="fully_trusted"`` preserves the seed's
    uid-1000 ownership (the workspace klangk user); every member is a
    whitelisted relative path.
    """
    proc = subprocess.Popen(
        [podman_bin, "export", cid],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=subprocess_env(),
    )
    extracted = False
    try:
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            _extract_seed_members(tar, out)
            extracted = True
    except tarfile.TarError:
        # Empty / truncated stream — checked against proc.returncode below.
        pass
    finally:
        assert proc.stdout is not None
        proc.stdout.close()
        proc.wait()
    err = (
        proc.stderr.read().decode("utf-8", "replace").strip()
        if proc.stderr
        else ""
    )
    if proc.returncode != 0:
        raise SeedError(f"podman export failed (rc={proc.returncode}): {err}")
    if not extracted:
        raise SeedError(
            f"podman export produced no readable tar stream: {err}"
        )


async def export_and_extract(podman_bin: str, image: str, out: Path) -> None:
    """Create a throwaway container from *image*, stream its ``/nix`` +
    ``/etc/nix/nix.conf`` out into *out*, then remove the container.
    """
    rc, cid_out, _ = await run(
        podman_bin, ["create", "--entrypoint", "/bin/true", image]
    )
    cid = cid_out.strip().splitlines()[-1] if cid_out.strip() else ""
    if not cid:
        raise SeedError("podman create returned no container id")
    try:
        await asyncio.to_thread(export_to_dir_sync, podman_bin, cid, str(out))
    finally:
        await run(podman_bin, ["rm", cid], check=False)

    # tar wrote etc/nix/nix.conf; flatten it to <out>/nix.conf + drop etc/.
    conf = out / "etc" / "nix" / "nix.conf"
    if not conf.is_file():
        raise SeedError("/etc/nix/nix.conf missing from the seed image")
    target = out / "nix.conf"
    if target.exists():
        target.unlink()
    conf.rename(target)
    for d in (conf.parent, conf.parent.parent):
        try:
            d.rmdir()
        except OSError:
            pass


async def verify(podman_bin: str, image: str, out: Path) -> None:
    """Run ``nix --version`` (and ``devenv --version``) against the extracted
    store, mounted read-only into a plain base — proves the seed is self-
    contained."""
    script = (
        "export PATH=/nix/nix-profile/bin:$PATH "
        "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt; "
        "test -f /nix/nix-profile/etc/profile.d/nix.sh; "
        "nix --version && devenv --version"
    )
    await run(
        podman_bin,
        [
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            "-v",
            f"{out / 'nix'}:/nix:ro",
            "-v",
            f"{out / 'nix.conf'}:/etc/nix/nix.conf:ro",
            image,
            "-c",
            script,
        ],
        timeout=300.0,
    )


# --- tree helpers ----------------------------------------------------------


def chmod_w(path: Path) -> None:
    """Recursively grant the owner u+rwx so read-only nix store files can be
    removed/moved by the host owner."""
    for root, dirs, files in os.walk(path):
        try:
            os.chmod(root, stat.S_IRWXU)
        except OSError:
            pass
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)
            except OSError:
                pass


def cp_a(src: Path, dst: Path) -> None:
    """``cp -a`` semantics: recursive copy preserving mode/time/symlinks (used
    to populate the seed subvolume from the build output)."""
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


def clear_seed_dir(out: Path) -> None:
    """Remove an existing ``nix/`` + ``nix.conf`` + ``etc/`` (chmod read-only
    store files first so rmdir/unlink can proceed)."""
    for name in ("nix", "nix.conf", "etc"):
        p = out / name
        if not p.exists() and not p.is_symlink():
            continue
        if p.is_dir():
            chmod_w(p)
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                p.unlink()
            except OSError:
                pass


# --- orchestration ---------------------------------------------------------


async def build_nix_seed(
    out_dir: str | os.PathLike[str],
    *,
    podman_bin: str,
    dockerfile_text: str,
    no_cache: bool = False,
    update: bool = False,
) -> None:
    """Build the seed image, extract ``nix`` + ``nix.conf`` to *out_dir*, verify.

    Without ``update`` the call refuses to clobber an existing seed; with it,
    the prior ``nix/`` + ``nix.conf`` + ``etc/`` are cleared first (per-
    workspace ``/nix`` — fuse uppers / btrfs snapshots — is untouched: it lives
    elsewhere). Raises :class:`SeedError` on any failure.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "nix").exists() or (out / "nix.conf").exists():
        if not update:
            raise SeedError(
                f"{out} already contains a seed (nix/ or nix.conf); pass "
                "--update to rebuild in place"
            )
        clear_seed_dir(out)

    await build_image(podman_bin, dockerfile_text, no_cache=no_cache)
    try:
        await export_and_extract(podman_bin, IMAGE, out)
    finally:
        # Store files extract read-only; make them writable whether or not
        # extraction succeeded so the operator can clean up / re-run.
        chmod_w(out / "nix")
    await verify(podman_bin, IMAGE, out)
    logger.info("seed written to %s", out)


# --- btrfs subvolume loader ------------------------------------------------


async def load_nix_seed_btrfs(
    seed_tree: str | os.PathLike[str],
    btrfs_parent: str | os.PathLike[str],
    *,
    btrfs_bin: str = "btrfs",
) -> Path:
    """Load a seed tree into a btrfs subvolume for the btrfs-snapshot backend.

    Creates ``<btrfs_parent>/seed`` via ``btrfs subvolume create`` and copies
    the seed's ``nix/`` + ``nix.conf`` into it. Refuses to clobber an existing
    subvolume (reseed by deleting it first). The seed store files are made
    writable beforehand so the copy can update paths in a shared tree.
    """
    tree = Path(seed_tree)
    parent = Path(btrfs_parent)
    if not (tree / "nix").is_dir():
        raise SeedError(
            f"{tree}/nix not found — build the seed first "
            "(klangk-build-nix-seed)"
        )
    if not (tree / "nix.conf").is_file():
        raise SeedError(f"{tree}/nix.conf not found")
    parent.mkdir(parents=True, exist_ok=True)
    seed = parent / "seed"
    if seed.exists():
        raise SeedError(
            f"{seed} already exists — remove it first: "
            f"{btrfs_bin} subvolume delete {seed}"
        )
    logger.info("creating seed subvolume %s", seed)
    await run(btrfs_bin, ["subvolume", "create", str(seed)], timeout=60.0)
    chmod_w(tree)
    cp_a(tree / "nix", seed / "nix")
    cp_a(tree / "nix.conf", seed / "nix.conf")
    logger.info("loaded seed into %s", seed)
    return seed


# --- bundled Dockerfile location ------------------------------------------


def bundled_dockerfile() -> Path | None:
    """The Dockerfile shipped in the wheel (``klangk/nix-seed/Dockerfile``), or
    ``None`` if absent (a dev/editable install where the bundle isn't present)."""
    try:
        res = importlib.resources.files("klangk") / _BUNDLE_DIR / "Dockerfile"
        p = Path(str(res))
        return p if p.is_file() else None
    except (ModuleNotFoundError, FileNotFoundError):
        return None


def source_dockerfile() -> Path | None:
    """Dev fallback: the Dockerfile in the source tree
    (``src/containers/nix-seed/Dockerfile``)."""
    p = (
        Path(__file__).resolve().parent.parent.parent
        / "containers"
        / "nix-seed"
        / "Dockerfile"
    )
    return p if p.is_file() else None


def read_dockerfile() -> str:
    """The seed Dockerfile text: bundled (wheel) first, then source (dev)."""
    for finder in (bundled_dockerfile, source_dockerfile):
        p = finder()
        if p is not None:
            return p.read_text()
    raise SeedError(
        "could not locate the nix-seed Dockerfile — the wheel install is "
        "incomplete (klangk/nix-seed/Dockerfile missing)"
    )


# --- entrypoint ------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="klangk-build-nix-seed",
        description=(
            "Build the shared nix seed dir from a wheel install (no devenv "
            "required). Point nix_seed.path at the output dir."
        ),
    )
    parser.add_argument("out_dir", help="output dir (gets nix/ + nix.conf)")
    parser.add_argument(
        "--update",
        action="store_true",
        help="rebuild in place (overwrite an existing seed)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="pass --no-cache to podman build (fresh nix/devenv)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    podman_bin = resolve_podman_bin()
    if not shutil.which(podman_bin):
        print(
            f"ERROR: {podman_bin} not found on PATH (set KLANGKD_PODMAN_BIN)",
            file=sys.stderr,
        )
        return 2

    try:
        dockerfile_text = read_dockerfile()
    except SeedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    try:
        asyncio.run(
            build_nix_seed(
                args.out_dir,
                podman_bin=podman_bin,
                dockerfile_text=dockerfile_text,
                no_cache=args.no_cache,
                update=args.update,
            )
        )
    except SeedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _build_load_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="klangk-load-nix-seed-btrfs",
        description=(
            "Load the nix seed tree into a btrfs subvolume (for "
            "nix_seed.type: btrfs-snapshot). The seed subvolume lands at "
            "<btrfs-parent>/seed."
        ),
    )
    parser.add_argument(
        "seed_tree",
        help="klangk-build-nix-seed output (holds nix/ + nix.conf)",
    )
    parser.add_argument(
        "btrfs_parent",
        help="dir on a btrfs fs mounted with user_subvol_rm_allowed; "
        "the seed subvolume lands at <btrfs-parent>/seed",
    )
    return parser


def load_main(argv: list[str] | None = None) -> int:
    args = _build_load_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not shutil.which("btrfs"):
        print(
            "ERROR: btrfs not found on PATH (install btrfs-progs)",
            file=sys.stderr,
        )
        return 2

    try:
        seed = asyncio.run(
            load_nix_seed_btrfs(args.seed_tree, args.btrfs_parent)
        )
    except SeedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Set in klangkd.yaml:")
    print("  nix_seed:")
    print("    type: btrfs-snapshot")
    print(f"    path: {seed}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
