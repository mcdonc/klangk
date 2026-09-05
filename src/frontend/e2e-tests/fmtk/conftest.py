"""pytest wiring for the fmtk e2e suites (#3232).

One session-scoped :class:`Harness` boots the scratch stack (backend +
proxy are adopted/kept across runs, like ``fmtk-up``); tests get an
``app`` (FmtkClient) bound to the running debug app. After every test the
app-error drain asserts the toolkit saw zero uncaught Flutter errors —
a suite-level invariant, so a scenario that "passes" while spewing errors
is red (#3232 done-when). Attribution caveat: the toolkit's error monitor
is a rolling, never-cleared window over the current app instance — an
error surfacing in test B may originate in test A of the same session —
and ``restart_app`` drains+raises *before* stopping so the outgoing
instance cannot launder its errors away.

Env knobs: FMTK_E2E_FRESH=1 wipes the scratch state before the session
(fresh DB); FMTK_E2E_HEADLESS forces Chrome mode (default auto: headless
under CI or without DISPLAY). Run via the ``test-fmtk-e2e`` devenv script.
"""

from __future__ import annotations

import os

import pytest

from fmtkharness import Harness


@pytest.fixture(scope="session")
def harness() -> Harness:
    instance = Harness()
    instance.boot(fresh=os.environ.get("FMTK_E2E_FRESH") == "1")
    yield instance
    instance.teardown()


@pytest.fixture(scope="session")
def app(harness: Harness):
    return harness.client


@pytest.fixture(autouse=True)
def no_app_errors(app):
    """Every test must leave the app error-free (drained post-test)."""
    yield
    errors = app.app_errors()
    assert not errors, f"uncaught app errors during test: {errors}"
