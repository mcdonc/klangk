"""Tests for the create choke-point orchestrator (#1244).

``ContainerRegistry.bringup`` runs inside ``start_container`` for every
fresh container. It ensures the shared home (``/home/klangk`` — needed
under both layouts by the ``service`` tmux session and, under the shared
layout, every login shell) and fires the service command. The underlying
primitives (``Workspaces.ensure_shared_home`` /
``Terminal.ensure_service_session``) have their own coverage; these tests
pin the orchestration: that each is called with the right args, in the
right cases, in the right order (#2717: shared-home population before
the service session).
"""

from unittest.mock import AsyncMock, MagicMock

from klangk.container import ContainerRegistry

_app_state = MagicMock()
_app_state.state.terminal.ensure_service_session = AsyncMock()
_app_state.state.workspaces.ensure_shared_home = AsyncMock()


def _registry():
    """A ContainerRegistry bound to the mock app_state.

    ``bringup`` reads only ``self.app_state`` (the workspaces and
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
        _app_state.state.workspaces.ensure_shared_home.reset_mock()

    async def test_populates_shared_home_then_fires_service_command(self):
        """A configured service command fires after the shared home is
        ensured; the service session itself carries no home parameter
        (HOME is pinned to the constant inside it, #2717)."""
        calls: list[str] = []

        async def shared_home(workspace_id, container_id):
            calls.append("shared_home")

        async def service_session(
            container_id, service_command, setup_state=None
        ):
            calls.append("service_session")

        _app_state.state.workspaces.ensure_shared_home.side_effect = (
            shared_home
        )
        _app_state.state.terminal.ensure_service_session.side_effect = (
            service_session
        )
        try:
            await _registry().bringup(
                "ws-id",
                "cid",
                "openclaw gateway",
                setup_state="complete",
            )
        finally:
            _app_state.state.workspaces.ensure_shared_home.side_effect = None
            _app_state.state.terminal.ensure_service_session.side_effect = None
        _app_state.state.workspaces.ensure_shared_home.assert_awaited_once_with(
            "ws-id", "cid"
        )
        _app_state.state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "openclaw gateway",
            setup_state="complete",
        )
        # Sequencing: the shared home is populated BEFORE the service
        # session is ensured, so the session's login shell finds a
        # populated /home/klangk/.profile (#2717).
        assert calls == ["shared_home", "service_session"]

    async def test_ensures_shared_home_even_without_service_command(self):
        """No service_command → the shared home is still ensured (login
        shells under the shared layout need it), but nothing is fired."""
        await _registry().bringup("ws-id", "cid", None, "complete")
        _app_state.state.workspaces.ensure_shared_home.assert_awaited_once_with(
            "ws-id", "cid"
        )
        _app_state.state.terminal.ensure_service_session.assert_not_awaited()

    async def test_skips_service_command_when_empty(self):
        """An empty service_command string is treated as 'none'."""
        await _registry().bringup("ws-id", "cid", "", "complete")
        _app_state.state.terminal.ensure_service_session.assert_not_awaited()

    async def test_threads_setup_state_through_predicate(self):
        """setup_state flows to ensure_service_session, which gates on it.

        A 'pending' setup_state still calls ensure_service_session (the
        gating happens inside it via should_fire_service_command), so the
        orchestrator's job is just to pass the value through unchanged.
        """
        await _registry().bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            setup_state="pending",
        )
        _app_state.state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "openclaw gateway",
            setup_state="pending",
        )

    async def test_threads_none_setup_state(self):
        """A None setup_state (caller omitted it) is passed through."""
        await _registry().bringup(
            "ws-id",
            "cid",
            "openclaw gateway",
            None,
        )
        _app_state.state.terminal.ensure_service_session.assert_awaited_once_with(
            "cid",
            "openclaw gateway",
            setup_state=None,
        )
