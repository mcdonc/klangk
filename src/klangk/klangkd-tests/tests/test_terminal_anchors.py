"""Anchor-registration tests for Terminal (#2520).

Covers Terminal.register_window_anchors, _register_service_anchor, and
_workspace_id_for_container — the pane-pid -> attribution-anchor wiring.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from klangk.terminal import Terminal

WID = "ws-anchor-0001"
CID = "abc123def456"


def _terminal():
    pod = MagicMock()
    registry = MagicMock()
    registry.states = {
        WID: MagicMock(container_id=CID),
        "other": MagicMock(container_id="zzz"),
    }
    settings = MagicMock()
    settings.disable_tmux = ""
    ledger = MagicMock()
    app_state = MagicMock()
    app_state.state.podman = pod
    app_state.state.container_registry = registry
    app_state.state.settings = settings
    app_state.state.process_ledger = ledger
    return Terminal(app_state), pod, ledger


class TestRegisterWindowAnchors:
    async def test_registers_pane_pids_for_new_windows(self):
        term, pod, ledger = _terminal()
        # covers: good rows, a non-digit pane pid, a line without a tab
        # covers: good rows, a wanted window with a non-digit pane pid,
        # a wanted window with a no-tab line
        pod.exec_container = AsyncMock(
            return_value=(
                0,
                "@1\t101\n@2\t202\n@3\tnotnum\ngarbageline-no-tab\n@9\t999\n",
                "",
            )
        )
        await term.register_window_anchors(
            CID,
            "sess",
            [{"id": "@1"}, {"id": "@2"}, {"id": "@3"}, {"id": "@4"}],
            "alice",
        )
        calls = [
            c for c in ledger.set_anchor.call_args_list if c.args[2] == WID
        ]
        principals = sorted(c.args[1] for c in calls)
        assert principals == ["user:alice", "user:alice"]
        pids = sorted(c.args[0] for c in calls)
        assert pids == [101, 202]

    async def test_no_exec_output_no_anchors(self):
        term, pod, ledger = _terminal()
        pod.exec_container = AsyncMock(return_value=(1, "", "err"))
        await term.register_window_anchors(CID, "sess", [], "alice")
        ledger.set_anchor.assert_not_called()

    async def test_unknown_container_no_anchors(self):
        term, pod, ledger = _terminal()
        pod.exec_container = AsyncMock(return_value=(0, "@1\t101\n", ""))
        await term.register_window_anchors("nope", "sess", [], "alice")
        ledger.set_anchor.assert_not_called()


class TestRegisterServiceAnchor:
    async def test_registers_service_pane_as_agent(self):
        term, pod, ledger = _terminal()
        pod.exec_container = AsyncMock(return_value=(0, "303\n", ""))
        await term._register_service_anchor(CID)
        ledger.set_anchor.assert_called_once_with(303, "agent", WID)

    async def test_exec_failure_no_anchor(self):
        term, pod, ledger = _terminal()
        pod.exec_container = AsyncMock(return_value=(1, "", "boom"))
        await term._register_service_anchor(CID)
        ledger.set_anchor.assert_not_called()

    async def test_nonnumeric_output_skipped(self):
        term, pod, ledger = _terminal()
        pod.exec_container = AsyncMock(return_value=(0, "x\n\n", ""))
        await term._register_service_anchor(CID)
        ledger.set_anchor.assert_not_called()

    async def test_unknown_container_no_anchor(self):
        term, pod, ledger = _terminal()
        pod.exec_container = AsyncMock(return_value=(0, "303\n", ""))
        await term._register_service_anchor("unknown-cid")
        ledger.set_anchor.assert_not_called()


def test_workspace_id_for_container():
    term, _, _ = _terminal()
    assert term._workspace_id_for_container(CID) == WID
    assert term._workspace_id_for_container("nope") is None
