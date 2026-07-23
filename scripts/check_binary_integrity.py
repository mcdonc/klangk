#!/usr/bin/env python3
"""Pre-commit guard against UTF-8-lossy rewrites of binary files.

A find-and-replace run in text mode with ``errors='replace'`` (or an editor
that re-saves through a UTF-8-normalizing path) silently corrupts any file
whose bytes aren't valid UTF-8: each maximal run of invalid input collapses to
one ``U+FFFD``, which re-encodes to the three bytes ``EF BF BD``. The file is
destroyed and usually inflated ~3x. This bit klangk in #1734, where a
"plugin"->"feature" sweep mangled the bundled libghostty wasm + a font; the
corrupt wasm then crashed ``WebAssembly.instantiate`` at app boot and hung
every frontend e2e test.

The mechanism is independent of the replacement text — the file does NOT need
to contain "plugin" to be damaged; the corruption happens during the lossy
*read*, before the substitution runs.

This hook diffs every staged file against HEAD and fails on the signature of
such a round-trip: the byte sequence ``EF BF BD`` appearing more often than in
the committed version. For files git considers binary we flag *any* increase
(binaries never legitimately gain replacement chars); for text we require a
larger jump to avoid false positives on incidental bytes.
"""

import subprocess
import sys

# U+FFFD encoded in UTF-8 — the tell-tale of a lossy decode.
REPL = bytes.fromhex("efbfbd")
# Text files: only flag a sizeable jump. Calibrated well above the ~0-1
# incidental occurrences seen in klangk's source; a real lossy rewrite of a
# non-UTF-8 text blob produces hundreds.
TEXT_THRESHOLD = 16


def run(args):
    return subprocess.run(args, capture_output=True)


def staged_paths():
    r = run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    return [line for line in r.stdout.decode("utf-8", "ignore").splitlines() if line]


def binary_paths():
    """Paths git classifies as binary in the staged diff (numstat shows `-`)."""
    r = run(["git", "diff", "--cached", "--numstat", "--diff-filter=ACMR"])
    out = set()
    for line in r.stdout.decode("utf-8", "ignore").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "-":
            out.add(parts[2])
    return out


def blob(path, ref):
    """Return the blob bytes for ``path`` at ``ref`` ("" = index/staged,
    "HEAD" = committed), or None if absent (e.g. a newly added file)."""
    obj = f"{ref}:{path}" if ref else f":{path}"
    r = run(["git", "cat-file", "-p", obj])
    return r.stdout if r.returncode == 0 else None


def count_repl(data):
    return data.count(REPL) if data else 0


def find_violations():
    binaries = binary_paths()
    violations = []
    for path in staged_paths():
        new = blob(path, "")
        old = blob(path, "HEAD")
        n_new = count_repl(new)
        n_old = count_repl(old)
        if n_new <= n_old:
            continue
        increase = n_new - n_old
        is_binary = path in binaries
        # Binaries: any gain is the corruption signature. Text: threshold to
        # avoid flagging a stray replacement char in a genuine edit.
        if is_binary or increase >= TEXT_THRESHOLD:
            violations.append((path, n_old, n_new, is_binary))
    return violations


def report(violations):
    print(
        "error: staged file(s) gained UTF-8 replacement characters "
        "(U+FFFD / EF BF BD) - the signature of a text-mode / UTF-8-lossy "
        "rewrite that corrupts binaries (was the file run through a "
        "find-and-replace, or re-saved by an editor?). See #1734.",
        file=sys.stderr,
    )
    for path, old, new, is_bin in violations:
        kind = "binary" if is_bin else "text"
        print(f"  {path} [{kind}]: {old} -> {new} EF BF BD bytes", file=sys.stderr)
    print(file=sys.stderr)
    print(
        "  Restore the byte-faithful version and re-apply edits with a "
        "byte-exact tool:",
        file=sys.stderr,
    )
    print(
        "    git checkout HEAD -- <path>   "
        "# or origin/main -- <path> if HEAD is also bad",
        file=sys.stderr,
    )
    print(
        "    # then use sed/perl/grep on bytes, not open(text).replace().",
        file=sys.stderr,
    )


def main():
    violations = find_violations()
    if not violations:
        return 0
    report(violations)
    return 1


if __name__ == "__main__":
    sys.exit(main())
