#!/usr/bin/env python3
"""Detect non-module-scope (deferred) imports in Python packages.

Imports inside functions, methods, or branches are flagged. Add
``# allow-deferred-import`` to suppress — either on the import line or on
a comment line directly above it (needed when the import is long enough
that a trailing comment would exceed the line-length limit).

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


def _is_type_checking(node: ast.AST) -> bool:
    """Check if a node sits directly inside ``if TYPE_CHECKING:``.

    That block is the canonical module-scope pattern for annotation-only
    imports (erased at runtime), so its imports are exempt without a marker.
    """
    parent = getattr(node, "_parent", None)
    if not isinstance(parent, ast.If):
        return False
    test = parent.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _is_guarded_optional(node: ast.AST) -> bool:
    """Check if a node is a module-scope ``try: import`` over ``ImportError``.

    The guarded-optional-dependency pattern (import at module scope inside
    ``try/except ImportError``) is also module-scope in spirit — the import
    executes once at load, with a fallback for absent extras.
    """
    parent = getattr(node, "_parent", None)
    if not (isinstance(parent, ast.Try) and _is_top_level(parent)):
        return False
    return any(
        isinstance(h, ast.ExceptHandler)
        and isinstance(h.type, ast.Name)
        and h.type.id in ("ImportError", "ModuleNotFoundError")
        for h in parent.handlers
    )


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


def _is_marked(lines: list[str], lineno: int, comment: str) -> bool:
    """Is the import at *lineno* suppressed? The marker may sit on the
    import line itself, on a comment line directly above it, or — when
    the import sits inside a nested block (``if``/``try``/…) — on any
    consecutive comment line above it."""
    if _line_has_comment(lines, lineno, comment):
        return True
    i = lineno - 2  # 0-based index of the line above
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped.startswith("#"):
            return False
        if comment in stripped:
            return True
        i -= 1
    return False


def check_deferred_imports(package_dir: str) -> list[str]:
    """Flag imports that are not at module scope.

    Lines with ``# allow-deferred-import`` (on the import line or the
    comment line directly above) are exempted.
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
            if (
                _is_top_level(node)
                or _is_type_checking(node)
                or _is_guarded_optional(node)
            ):
                continue
            if _is_marked(source_lines, node.lineno, "allow-deferred-import"):
                continue

            errors.append(_deferred_import_error(root, pyfile, node))
    return errors


def _deferred_import_error(root: Path, pyfile: Path, node) -> str:
    """The error line for one flagged deferred import."""
    rel = pyfile.relative_to(root.parent)
    if isinstance(node, ast.Import):
        names = ", ".join(a.name for a in node.names)
        return f"{rel}:{node.lineno}: deferred import: import {names}"
    module = node.module or ""
    prefix = "." * node.level + module
    names = ", ".join(a.name for a in node.names)
    return f"{rel}:{node.lineno}: deferred import: from {prefix} import {names}"


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
