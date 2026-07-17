#!/usr/bin/env python3
"""Detect non-module-scope (deferred) imports in Python packages.

Imports inside functions, methods, or branches are flagged. Add
``# noqa: allow-deferred-import`` on the import line to suppress.

Usage:
    check_imports.py src/klangk/klangk src/klangk/klangk/cli
    check_imports.py src/klangk/klangk/main.py  # discovers package
    check_imports.py                                      # discovers from cwd
"""

import ast
import sys
from pathlib import Path


def _is_top_level(node: ast.AST) -> bool:
    """Check if a node is at module scope (parent is ast.Module)."""
    return isinstance(getattr(node, "_parent", None), ast.Module)


def _parse_file(filepath: Path):
    """Parse a file and annotate parent nodes. Returns (tree, lines) or None."""
    try:
        source_text = filepath.read_text()
        tree = ast.parse(source_text)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node  # noqa: SLF001
    return tree, source_text.splitlines()


def _line_has_comment(lines: list[str], lineno: int, comment: str) -> bool:
    return comment in lines[lineno - 1] if lineno <= len(lines) else False


def check_deferred_imports(package_dir: str) -> list[str]:
    """Flag imports that are not at module scope.

    Lines with ``# noqa: allow-deferred-import`` are exempted.
    Returns error lines.
    """
    root = Path(package_dir).resolve()
    if not root.is_dir():
        return [f"ERROR: {package_dir} is not a directory"]

    errors: list[str] = []
    for pyfile in sorted(root.rglob("*.py")):
        parsed = _parse_file(pyfile)
        if parsed is None:
            continue
        tree, source_lines = parsed

        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            if _is_top_level(node):
                continue
            if _line_has_comment(
                source_lines, node.lineno, "noqa: allow-deferred-import"
            ):
                continue

            rel = pyfile.relative_to(root.parent)
            if isinstance(node, ast.Import):
                names = ", ".join(a.name for a in node.names)
                errors.append(f"{rel}:{node.lineno}: deferred import: import {names}")
            else:
                module = node.module or ""
                prefix = "." * node.level + module
                names = ", ".join(a.name for a in node.names)
                errors.append(
                    f"{rel}:{node.lineno}: deferred import:"
                    f" from {prefix} import {names}"
                )
    return errors


def _find_package_root(filepath: Path) -> Path | None:
    """Walk up from a .py file to find the top-level package directory.

    Returns the deepest directory that still has an ``__init__.py`` in
    every ancestor up to the package root, or None if the file isn't
    inside a package.
    """
    parent = filepath.parent
    root = None
    while (parent / "__init__.py").exists():
        root = parent
        parent = parent.parent
    return root


def _packages_from_files(files: list[str]) -> list[str]:
    """Derive unique package directories from a list of .py file paths."""
    roots: set[str] = set()
    for f in files:
        p = Path(f).resolve()
        if p.suffix != ".py":
            continue
        pkg = _find_package_root(p)
        if pkg is not None:
            roots.add(str(pkg))
    return sorted(roots)


def _find_packages_in_dir(directory: Path) -> list[str]:
    """Find all Python packages (dirs with __init__.py) under *directory*."""
    roots: set[Path] = set()
    for init in sorted(directory.rglob("__init__.py")):
        pkg = _find_package_root(init)
        if pkg is not None:
            roots.add(pkg)
    return sorted(str(r) for r in roots)


def main() -> int:
    args = sys.argv[1:]

    if not args:
        # No arguments: discover packages under cwd
        package_dirs = _find_packages_in_dir(Path.cwd())
    elif any(a.endswith(".py") for a in args):
        # File paths: discover packages from them
        package_dirs = _packages_from_files(args)
    else:
        # Package directories
        package_dirs = args

    if not package_dirs:
        return 0

    all_errors: list[str] = []
    for pkg_dir in package_dirs:
        all_errors.extend(check_deferred_imports(pkg_dir))

    if all_errors:
        for line in all_errors:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
