"""Tests for the create choke-point orchestrator (#1244).

``ContainerRegistry._bringup`` runs inside ``start_container`` for every
fresh container. It fires the service command in the agent identity's
home. The underlying primitive (``Terminal.ensure_service_session``) has
its own coverage; these tests pin the orchestration: that it is called
with the right args, and that it is skipped when no command is
configured.
"""

from unittest.mock import AsyncMock, MagicMock

from klangk.container import ContainerRegistry

_app_state = MagicMock()
_app_state.state.terminal.ensure_service_session = AsyncMock()
_app_state.state.model.users.agent_handle = AsyncMock(return_value="klangk")


def _registry():
    """A ContainerRegistry bound to the mock app_state.

    ``_bringup`` reads only ``self.app_state`` (the terminal and model
    siblings), so we skip the heavy ``__init__`` (which parses settings +
    builds collaborators that these tests don't exercise) and attach the
    mock app_state directly.
    """
    reg = object.__new__(ContainerRegistry)
    reg.app = _app_state
    return reg


class TestBringup:
    def setup_method(self):
        _app_state.state.terminal.ensure_service_session.reset_mock()
        _app_state.state.model.users.agent_handle.reset_mock()
        _app_state.state.model.users.agent_handle.return_value = "klangk"

    async def test_fires_service_command_in_agent_home(self):
        """A configured service command fires in the agent identity's home."""
        await _registry()._bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            setup_state="complete",
        )
        _app_state.state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "/home/klangk",
            "openclaw gateway",
            setup_state="complete",
        )

    async def test_skips_service_command_when_none(self):
        """No service_command -> nothing is fired (and no handle lookup)."""
        await _registry()._bringup("ws-id", "cid", None, "complete")
        _app_state.state.model.users.agent_handle.assert_not_awaited()
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
