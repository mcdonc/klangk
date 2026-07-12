"""Tests for bringup: the create choke-point orchestrator (#1244).

``bringup`` runs inside ``start_container`` for every fresh container.
It provisions the agent home and fires the service command. The
underlying primitives (``Agents.ensure_agent_home``,
``Terminal.ensure_service_session``) have their own coverage; these
tests pin the orchestration: that both are called in the right order
with the right args, and that the service-command half is skipped when
no command is configured.
"""

from unittest.mock import AsyncMock, MagicMock

from klangk_backend import bringup
from klangk_backend.agent import Agents

_app_state = MagicMock()
_app_state.terminal.ensure_service_session = AsyncMock()
_app_state.agents = MagicMock(spec=Agents)
_app_state.agents.ensure_agent_home = AsyncMock(return_value="/home/clanker")


class TestBringup:
    def setup_method(self):
        _app_state.terminal.ensure_service_session.reset_mock()
        _app_state.agents.ensure_agent_home.reset_mock()
        _app_state.agents.ensure_agent_home.return_value = "/home/clanker"

    async def test_provisions_home_and_fires_service_command(self):
        """A configured service command fires after the home is ready."""
        await bringup.bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            setup_state="complete",
            app_state=_app_state,
        )
        _app_state.agents.ensure_agent_home.assert_awaited_once_with(
            "ws-id", "cid"
        )
        _app_state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "/home/clanker",
            "openclaw gateway",
            setup_state="complete",
        )

    async def test_skips_service_command_when_none(self):
        """No service_command -> only the agent home is provisioned."""
        await bringup.bringup(
            "ws-id", "cid", None, "complete", app_state=_app_state
        )
        _app_state.agents.ensure_agent_home.assert_awaited_once_with(
            "ws-id", "cid"
        )
        _app_state.terminal.ensure_service_session.assert_not_awaited()

    async def test_skips_service_command_when_empty(self):
        """An empty service_command string is treated as 'none'."""
        await bringup.bringup(
            "ws-id", "cid", "", "complete", app_state=_app_state
        )
        _app_state.terminal.ensure_service_session.assert_not_awaited()

    async def test_threads_setup_state_through_predicate(self):
        """setup_state flows to ensure_service_session, which gates on it.

        A 'pending' setup_state still calls ensure_service_session (the
        gating happens inside it via should_fire_service_command), so the
        orchestrator's job is just to pass the value through unchanged.
        """
        await bringup.bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            setup_state="pending",
            app_state=_app_state,
        )
        _app_state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "/home/clanker",
            "openclaw gateway",
            setup_state="pending",
        )

    async def test_threads_none_setup_state(self):
        """A None setup_state (caller omitted it) is passed through."""
        await bringup.bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            None,
            app_state=_app_state,
        )
        _app_state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "/home/clanker",
            "openclaw gateway",
            setup_state=None,
        )
