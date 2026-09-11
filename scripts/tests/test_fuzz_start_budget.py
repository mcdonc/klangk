"""Contract test: the fuzz harnesses' server-start budget must outlast the
server's own podman give-up point (#3412).

``wait_for_server`` (fuzz-api) and ``_wait_ready`` (fuzz-idle) used fixed
wall clocks (90–120 s) while the server's ``prewarm_podman()`` runs its
first ``podman create`` under ``bringup_timeout()`` (240 s on CI) with one
``_run_create`` retry — up to 480 s, plus the stop+rm teardown. Whenever
the first create was merely slow (still inside the server's budget), the
harness aborted the session first with 0 requests sent. The shared budget
(``fuzzlib.server_start_budget``) is derived from the same
``bringup_timeout`` the server uses, and this test pins that coupling so
the two can't drift apart silently again.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fuzzlib import server_start_budget  # noqa: E402
from klangk.podman import bringup_timeout  # noqa: E402


def test_budget_covers_prewarm_worst_case():
    # Two full create budgets (timeout + the one _run_create retry) plus
    # the 60 s stop+rm tail leave room for boot, migrations, seeding, and
    # the startup reaps.
    assert server_start_budget() >= 2 * bringup_timeout() + 60, (
        "the fuzz start budget no longer covers prewarm's worst case — "
        "update fuzzlib.server_start_budget"
    )


def test_budget_tracks_ci_doubling(monkeypatch):
    # bringup_timeout doubles on CI (240 s); the budget must follow it
    # there, or slow CI runner days abort the session again (#3412).
    monkeypatch.setenv("CI", "true")
    assert server_start_budget() == 30 + 2 * 240.0 + 60, (
        "expected 30 s boot + 2 x bringup_timeout(CI) + 60 s teardown — "
        "if bringup_timeout's CI default changed, update this pin and the "
        "budget's docstring in fuzzlib.py"
    )
