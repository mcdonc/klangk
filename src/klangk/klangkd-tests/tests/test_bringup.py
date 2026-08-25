"""Tests for the create choke-point orchestrator (#1244).

``ContainerRegistry._bringup`` runs inside ``start_container`` for every
fresh container. It materializes the agent identity's home (the agent
never connects over the WebSocket, so nothing else would — yet the sandbox
``setup.sh`` contract and the ``service`` tmux session both need
``/home/klangk`` to exist) and fires the service command. The underlying
primitives (``Workspaces.ensure_agent_home`` / ``populate_home_skel``,
``Terminal.ensure_service_session``) have their own coverage; these tests
pin the orchestration: that each is called with the right args, in the
right cases.
"""

from unittest.mock import AsyncMock, MagicMock

from klangk.container import ContainerRegistry
from klangk.model import AGENT_USER_ID

_app_state = MagicMock()
_app_state.state.terminal.ensure_service_session = AsyncMock()
_app_state.state.workspaces.ensure_agent_home = AsyncMock(
    return_value=("/home/klangk", False)
)
_app_state.state.workspaces.populate_home_skel = AsyncMock()


def _registry():
    """A ContainerRegistry bound to the mock app_state.

    ``_bringup`` reads only ``self.app_state`` (the workspaces and
    terminal siblings), so we skip the heavy ``__init__`` (which parses
    settings + builds collaborators that these tests don't exercise) and
    attach the mock app_state directly.
    """
    reg = object.__new__(ContainerRegistry)
    reg.app = _app_state
    return reg


class TestBringup:
    def setup_method(self):
        _app_state.state.terminal.ensure_service_session.reset_mock()
        _app_state.state.workspaces.ensure_agent_home.reset_mock()
        _app_state.state.workspaces.ensure_agent_home.return_value = (
            "/home/klangk",
            False,
        )
        _app_state.state.workspaces.populate_home_skel.reset_mock()

    async def test_ensures_agent_home_then_fires_service_command(self):
        """A configured service command fires with the resolved agent home."""
        await _registry()._bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            setup_state="complete",
        )
        _app_state.state.workspaces.ensure_agent_home.assert_awaited_once_with(
            "ws-id"
        )
        _app_state.state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "/home/klangk",
            "openclaw gateway",
            setup_state="complete",
        )

    async def test_populates_skel_only_on_first_creation(self):
        """``created=True`` from ensure_agent_home triggers the skel copy
        into the agent's own home path; an already-existing home does not."""
        _app_state.state.workspaces.ensure_agent_home.return_value = (
            "/home/klangk",
            True,
        )
        await _registry()._bringup("ws-id", "cid", None, "complete")
        _app_state.state.workspaces.populate_home_skel.assert_awaited_once_with(
            "cid", AGENT_USER_ID, home="/home/klangk"
        )

        # Second bringup: home exists → no skel copy.
        _app_state.state.workspaces.ensure_agent_home.return_value = (
            "/home/klangk",
            False,
        )
        _app_state.state.workspaces.populate_home_skel.reset_mock()
        await _registry()._bringup("ws-id", "cid", None, "complete")
        _app_state.state.workspaces.populate_home_skel.assert_not_awaited()

    async def test_ensures_home_even_without_service_command(self):
        """No service_command → the home is still ensured (the sandbox
        setup.sh contract needs it), but nothing is fired."""
        await _registry()._bringup("ws-id", "cid", None, "complete")
        _app_state.state.workspaces.ensure_agent_home.assert_awaited_once_with(
            "ws-id"
        )
        _app_state.state.terminal.ensure_service_session.assert_not_awaited()

    async def test_skips_service_command_when_empty(self):
        """An empty service_command string is treated as 'none'."""
        await _registry()._bringup("ws-id", "cid", "", "complete")
        _app_state.state.terminal.ensure_service_session.assert_not_awaited()

    async def test_threads_setup_state_through_predicate(self):
        """setup_state flows to ensure_service_session, which gates on it.

        A 'pending' setup_state still calls ensure_service_session (the
        gating happens inside it via should_fire_service_command), so the
        orchestrator's job is just to pass the value through unchanged.
        """
        await _registry()._bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            setup_state="pending",
        )
        _app_state.state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "/home/klangk",
            "openclaw gateway",
            setup_state="pending",
        )

    async def test_threads_none_setup_state(self):
        """A None setup_state (caller omitted it) is passed through."""
        await _registry()._bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            None,
        )
        _app_state.state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "/home/klangk",
            "openclaw gateway",
            setup_state=None,
        )
