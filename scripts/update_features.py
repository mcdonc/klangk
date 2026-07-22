#!/usr/bin/env python3
"""Fetch/symlink features declared in the checked-in ``features.yaml``.

The feature declaration list is checked into the repo at its root
(``features.yaml``) — the source of truth for which features a build compiles.
The materialized payload (fetched/symlinked feature trees + ``features.lock``)
lives under a payload directory the caller supplies via ``--payload-dir``,
normally a fresh ``mktemp -d`` the build script owns and cleans up (#1660).

Features can be sourced from git repos (``git:`` key) or local paths
(``path:`` key without ``git:``).  Local-path features are symlinked into the
payload directory; relative paths resolve against the repo root (where the
checked-in ``features.yaml`` lives).
"""

import argparse
import atexit
import os
import re
import shutil
import subprocess
import sys
import tempfile

try:
    import yaml
except ImportError:
    print("PyYAML is required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The feature declaration list is checked into the repo at its root — the
# source of truth for which features a build compiles. The materialized
# payload (fetched/symlinked trees + features.lock) lands under the caller's
# ``--payload-dir`` (a build-owned tempdir), NOT next to this file (#1660).
YAML_PATH = os.path.join(ROOT, "features.yaml")


def _make_temp_payload_dir():
    """Create a fresh tempdir for the materialized payload and return its path.

    Used when the caller (e.g. a direct ``python3 update_features.py``) didn't
    pass ``--payload-dir``. Build scripts always pass an explicit dir so they
    can read the materialized trees afterward; this default exists so the
    script remains runnable standalone for debugging. The tempdir is recorded
    with :mod:`atexit` so a forgotten standalone invocation can't leak — the
    dir is removed on interpreter exit whether main() succeeds, fails, or is
    interrupted.
    """
    payload = tempfile.mkdtemp(prefix="klangk-features-")
    atexit.register(shutil.rmtree, payload, ignore_errors=True)
    return payload


def resolve_ref(git_url, ref):
    """Resolve a git ref (branch, tag, or SHA) to a commit SHA."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", git_url, ref],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for line in result.stdout.strip().splitlines():
            sha, _name = line.split("\t", 1)
            return sha
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Not resolvable on the remote (unknown branch/tag/SHA, or a synthesized
    # commit such as a GitHub PR merge ref that exists only in a CI checkout).
    return None


def _normalize_git_url(url):
    """Canonicalize a git URL so equivalent forms compare equal.

    Handles ssh/git/https variants, embedded credentials, and trailing .git.
    """
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("git@"):
        u = u.replace(":", "/", 1).replace("git@", "https://", 1)
    u = re.sub(r"^(ssh|git)://", "https://", u)
    # drop embedded credentials: https://user:pass@host/... -> https://host/...
    u = re.sub(r"^(https?://)[^/@]+@", r"\1", u)
    u = re.sub(r"\.git$", "", u)
    return u.rstrip("/").lower()


def _repo_origin_url():
    """Normalized origin URL of the repo update_features.py runs inside (or "")."""
    try:
        result = subprocess.run(
            ["git", "-C", ROOT, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return _normalize_git_url(result.stdout)
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""


def _copy_feature_tree(source, dest):
    """Copy a feature directory tree into dest, dropping any .git inside."""
    if os.path.islink(dest) or os.path.exists(dest):
        if os.path.islink(dest):
            os.unlink(dest)
        else:
            shutil.rmtree(dest)
    shutil.copytree(source, dest)
    git_dir = os.path.join(dest, ".git")
    if os.path.exists(git_dir):
        shutil.rmtree(git_dir)


def fetch_feature(feature, features_dir):
    """Fetch a single feature from a git repo into features_dir.

    Never silently degrades to the remote's default branch. If the requested
    ref can't be resolved on the remote, we either copy the feature from the
    local working tree (when the source repo *is* this repo -- e.g. a CI PR
    checkout whose synthesized merge-commit SHA isn't on the remote) or fail.
    """
    git_url = feature["git"]
    ref = feature.get("ref", "main")
    subpath = feature.get("path", "")

    name = feature["name"]

    dest = os.path.join(features_dir, name)
    local_origin = _repo_origin_url()

    # None means the ref isn't on the remote at all.
    sha = resolve_ref(git_url, ref)

    # GitHub PR builds check out a synthesized merge commit whose SHA exists
    # only on the runner, so `git ls-remote` can never resolve it. For features
    # that live in *this* repo, fall back to the already-checked-out working
    # tree (which has the exact content being built) instead of cloning.
    if sha is None:
        local_path = os.path.join(ROOT, subpath) if subpath else ROOT
        if (
            local_origin
            and _normalize_git_url(git_url) == local_origin
            and os.path.isdir(local_path)
        ):
            print(
                f"  {name}: ref '{ref}' not on remote; "
                f"using local working tree {subpath or '.'}/"
            )
            _copy_feature_tree(local_path, dest)
            return {
                "name": name,
                "git": git_url,
                "path": subpath,
                "ref": ref,
                "sha": "local",
            }
        print(f"  ERROR: Could not resolve ref '{ref}' for {git_url}", file=sys.stderr)
        return None

    print(f"  {name}: {git_url} @ {ref} -> {sha[:12]}")

    # Clone into temp dir, then check out the resolved ref.
    with tempfile.TemporaryDirectory() as tmpdir:
        clone_dir = os.path.join(tmpdir, "repo")
        result = subprocess.run(
            ["git", "clone", "--depth=1", "--branch", ref, git_url, clone_dir],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            # ref might be a SHA (not a branch name); full clone + checkout.
            result = subprocess.run(
                ["git", "clone", git_url, clone_dir],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                print(
                    f"  ERROR: git clone failed: {result.stderr.strip()}",
                    file=sys.stderr,
                )
                return None
            checkout = subprocess.run(
                ["git", "checkout", ref],
                cwd=clone_dir,
                capture_output=True,
                text=True,
            )
            if checkout.returncode != 0:
                print(
                    f"  ERROR: git checkout '{ref}' failed: {checkout.stderr.strip()}",
                    file=sys.stderr,
                )
                return None

        # Guard against silently landing on the default branch: the checked-out
        # HEAD must match the ref we resolved on the remote.
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=clone_dir,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if head != sha:
            print(
                f"  ERROR: checked-out HEAD {head[:12]} != resolved {sha[:12]} "
                f"for '{ref}'; refusing to use default-branch content",
                file=sys.stderr,
            )
            return None

        source = os.path.join(clone_dir, subpath) if subpath else clone_dir

        if not os.path.isdir(source):
            print(f"  ERROR: path '{subpath}' not found in {git_url}", file=sys.stderr)
            return None

        _copy_feature_tree(source, dest)

    return {"name": name, "git": git_url, "path": subpath, "ref": ref, "sha": sha}


def link_feature(feature, features_dir):
    """Symlink a local-path feature into features_dir."""
    name = feature["name"]
    source = os.path.expandvars(os.path.expanduser(feature["path"]))

    # Resolve relative paths against the directory containing features.yaml
    # (the repo root — features.yaml is checked in there, #1660).
    if not os.path.isabs(source):
        source = os.path.normpath(os.path.join(os.path.dirname(YAML_PATH), source))

    if not os.path.isdir(source):
        print(f"  ERROR: local path '{source}' does not exist", file=sys.stderr)
        return None

    dest = os.path.join(features_dir, name)

    # Remove old version (dir, symlink, or broken symlink)
    if os.path.islink(dest) or os.path.exists(dest):
        if os.path.islink(dest):
            os.unlink(dest)
        else:
            shutil.rmtree(dest)

    os.symlink(source, dest)
    print(f"  {name}: {source} (local symlink)")
    return {"name": name, "path": source}


def write_lock(entries, lock_path):
    """Write the lockfile."""
    with open(lock_path, "w") as f:
        yaml.dump({"features": entries}, f, default_flow_style=False, sort_keys=False)


def feature_name(feature):
    """Get the feature name from a features.yaml entry. Name is required."""
    name = feature.get("name")
    if not name:
        raise ValueError(f"Feature entry missing required 'name' field: {feature}")
    return name


def read_lock(lock_path):
    """Read existing lock entries as a dict keyed by name."""
    if not os.path.exists(lock_path):
        return {}
    with open(lock_path) as f:
        data = yaml.safe_load(f)
    return {e["name"]: e for e in (data or {}).get("features", [])}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Materialize features declared in the checked-in features.yaml."
    )
    parser.add_argument(
        "only",
        nargs="?",
        default=None,
        help="Optional feature name: fetch just that one (preserving existing lock entries).",
    )
    parser.add_argument(
        "--payload-dir",
        default=None,
        help=(
            "Where to write fetched/symlinked feature trees + features.lock. "
            "Defaults to a fresh mktemp -d the caller must clean up. Build "
            "scripts always pass this explicitly (#1660)."
        ),
    )
    parser.add_argument(
        "--local-only",
        action="store_true",
        help=(
            "Skip git-sourced features (only materialize local-path entries). "
            "For tests/CI that want to verify the local-feature contract "
            "without network access — remote features are exercised by the "
            "real build (scripts/flutterbuildweb.sh) instead. Git entries "
            "are noted in features.lock with sha: 'skipped' so the lock "
            "shape stays consistent (#1664)."
        ),
    )
    args = parser.parse_args(argv)

    if not os.path.isfile(YAML_PATH):
        print(
            f"ERROR: {YAML_PATH} not found — the feature declaration list is "
            "checked into the repo at its root. Run from a klangk checkout.",
            file=sys.stderr,
        )
        return 1

    payload_dir = args.payload_dir or _make_temp_payload_dir()
    os.makedirs(payload_dir, exist_ok=True)
    lock_path = os.path.join(payload_dir, "features.lock")

    with open(YAML_PATH) as f:
        config = yaml.safe_load(f)

    features = config.get("features", [])
    if not features:
        print("No features listed in features.yaml")
        return 0

    # Filter to a single feature if requested
    only = args.only
    if only:
        matched = [p for p in features if feature_name(p) == only]
        if not matched:
            print(f"Feature '{only}' not found in features.yaml", file=sys.stderr)
            sys.exit(1)
        features = matched

    print(f"Fetching {len(features)} feature{'s' if len(features) != 1 else ''}...")
    if not args.payload_dir:
        print(f"  (payload dir: {payload_dir} — pass --payload-dir to pin it)")

    # Preserve existing lock entries when updating a single feature
    old_lock = read_lock(lock_path)
    lock_map = dict(old_lock)

    for feature in features:
        if "git" in feature:
            if args.local_only:
                print(
                    f"  SKIP: {feature['name']} (git entry, --local-only)",
                    file=sys.stderr,
                )
                # Record the skip in the lock so its shape stays consistent
                # (every declared feature appears) without fetching. Preserve
                # a real SHA from the prior lock if one exists (an interleaved
                # `update_features <name>` may have already resolved it) — only
                # write sha='skipped' when there's nothing better to keep.
                prior = lock_map.get(feature["name"])
                prior_sha = prior.get("sha") if prior else None
                lock_map[feature["name"]] = {
                    "name": feature["name"],
                    "git": feature["git"],
                    "path": feature.get("path", ""),
                    "ref": feature.get("ref", "main"),
                    "sha": prior_sha
                    if prior_sha and prior_sha != "skipped"
                    else "skipped",
                }
                continue
            entry = fetch_feature(feature, payload_dir)
        elif "path" in feature:
            entry = link_feature(feature, payload_dir)
        else:
            print(
                f"  SKIP: entry needs 'git' or 'path' key: {feature}",
                file=sys.stderr,
            )
            continue
        if entry:
            lock_map[entry["name"]] = entry

    # Remove features that were in the old lockfile but dropped from features.yaml
    if not only:
        yaml_names = {
            feature_name(p)
            for p in config.get("features", [])
            if "git" in p or "path" in p
        }
        for name in list(lock_map):
            if name not in yaml_names:
                feature_dir = os.path.join(payload_dir, name)
                if os.path.islink(feature_dir):
                    os.unlink(feature_dir)
                    print(f"  Removed {name} (no longer in features.yaml)")
                elif os.path.isdir(feature_dir):
                    shutil.rmtree(feature_dir)
                    print(f"  Removed {name} (no longer in features.yaml)")
                del lock_map[name]

    write_lock(list(lock_map.values()), lock_path)
    print(f"Wrote {lock_path} with {len(lock_map)} features")
    return 0


if __name__ == "__main__":
    sys.exit(main())
