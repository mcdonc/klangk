"""Import every backend E2E module from the unit suite (#3057).

The unit suite never collects ``klangkd-tests/e2e-tests``, so an ImportError
there — a removed re-export, a renamed helper — is only caught by CI's
backend-e2e job, minutes later and in a different workflow. Importing the
modules is exactly what pytest collection does first, so it is cheap,
hermetic, and fails in the same place CI would, just earlier. The removed
``_e2e_server.free_port`` re-export (an implicit import only
``test_server_schedule_e2e.py`` relied on) slipped through exactly this gap.
"""

import importlib.util
import os
import sys

import pytest

E2E_DIR = os.path.realpath(
    os.path.join(os.path.dirname(__file__), "..", "e2e-tests")
)
HERMES_DIR = os.path.realpath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "sandboxes",
        "tests",
        "hermes",
    )
)


def e2e_module_files() -> list[str]:
    """Every E2E python file pytest collection would import.

    test_* modules, conftest, and the ``_*`` helper modules they import.
    Standalone scripts (e.g. smoketest_egress.py) are excluded — CI runs
    them as scripts, not via collection.
    """
    files: list[str] = []
    for base in (E2E_DIR, HERMES_DIR):
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            if not entry.endswith(".py"):
                continue
            if (
                entry.startswith("test_")
                or entry == "conftest.py"
                or entry.startswith("_")
            ):
                files.append(os.path.join(base, entry))
    return files


@pytest.mark.parametrize("path", e2e_module_files())
def test_e2e_module_imports(path: str, monkeypatch):
    # The helper modules resolve their intra-suite imports (`from
    # _e2e_server import ...`) through the dir on sys.path. spec-based
    # loading under a unique name avoids sys.modules collisions (two
    # conftest.py files live under these dirs).
    monkeypatch.syspath_prepend(os.path.dirname(path))
    spec = importlib.util.spec_from_file_location(
        f"e2e_import_check_{os.path.basename(path)[:-3]}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
