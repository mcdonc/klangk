"""Connection: per-WebSocket connection state and command handlers."""

import logging
import time
from datetime import datetime, timedelta, timezone


from .. import container, model
from ..container.spec import SHARED_HOME
from ..exceptions import NodeDrainingError, WorkspaceCapacityError
from ..model.container_events import CAUSE_WS_CONNECT
from ..terminal import TerminalSession
from ..podman import ExecSession, PodmanError
from .safe_websocket import SafeWebSocket, WS_ERRORS
from .support import (
    send_error,
    send_event,
    format_idle_timeout,
    format_container_info,
)
from .session import WorkspaceSession, get_shared_terminals
from .controllers import (
    SshAgentForwarder,
    ExecController,
    TerminalController,
    SharedTerminalController,
)

logger = logging.getLogger(__name__)


class Connection:
    """Per-WebSocket connection state and command handlers."""

    def __init__(self, ws: SafeWebSocket, user: dict, app):
        self.app = app
        self.sock = ws
        self.user = user
        self.workspace_id: str | None = None
        self.container_id: str | None = None
        # Terminal sessions are owned by the TerminalController
        # collaborator; Connection delegates the terminal_* commands to
        # it.  The ``terminal_session``/``terminal_task`` (and
        # ``terminal_cols``/``terminal_rows``) properties below proxy
        # to the controller for backwards compatibility with code
        # (and tests) that read/write those fields directly.
        self.terminal = TerminalController(self)
        # Exec sessions are owned by the ExecController collaborator;
        # Connection delegates the exec_* commands to it.  The
        # ``exec_session``/``exec_task`` properties below proxy to the
        # controller for backwards compatibility with code (and tests)
        # that read/write those fields directly.
        self.exec = ExecController(self)
        self.workspace: dict | None = None
        self._idle_cb = None
        self.pending_status_msg: str | None = None
        self.browser_id: str | None = None
        # Opt-in flag for the service_health liveness heartbeat
        # (#1175 item 3b); toggled by the ``subscribe_health_heartbeat``
        # command.  Off by default so the heartbeat is opt-in.
        self.wants_health_heartbeat: bool = False
        self._user_home: str | None = None
        self._service_command: str | None = None
        self._home_created: bool = False
        self.terminal_cols: int = 80
        self.terminal_rows: int = 24
        # Tracks which shared terminal this connection is viewing.
        # Set on join_shared_terminal, cleared on stop_terminal/terminal_start.
        # Shared-terminal state is owned by the
        # SharedTerminalController collaborator; Connection delegates
        # the share/unshare/join/list/create/delete commands to it.
        # The ``viewing_shared`` property below proxies to the
        # controller for backwards compatibility with code (and tests)
        # that read/write that field directly.
        self.shared = SharedTerminalController(self)
        # SSH agent forwarding is owned by the SshAgentForwarder
        # collaborator; Connection delegates the ssh_agent_* commands to
        # it. Relay state (proc/task/socket) lives on the forwarder
        # (``self.ssh_agent.*``), not on Connection.
        self.ssh_agent = SshAgentForwarder(self)

    # --- SSH agent forwarding (delegates to SshAgentForwarder) ---

    async def handle_ssh_agent_start(self) -> None:
        await self.ssh_agent.start()

    async def handle_ssh_agent_data(self, msg: dict) -> None:
        await self.ssh_agent.data(msg)

    async def handle_ssh_agent_stop(self) -> None:
        await self.ssh_agent.stop_command()

    async def _stop_ssh_agent(self) -> None:
        await self.ssh_agent.stop()

    async def _forward_ssh_agent_output(self) -> None:
        await self.ssh_agent.forward_output()

    # --- Terminal sessions (delegates to TerminalController) ---

    # Backwards-compatible proxies for the state formerly held on
    # Connection itself.  Reads and writes are forwarded to the
    # controller so existing callers (and tests) that read/write
    # ``terminal_session``/``terminal_task``/``terminal_cols``/
    # ``terminal_rows`` directly keep working unchanged.
    @property
    def terminal_session(self):
        return self.terminal.session

    @terminal_session.setter
    def terminal_session(self, value):
        self.terminal.session = value

    @property
    def terminal_task(self):
        return self.terminal.task

    @terminal_task.setter
    def terminal_task(self, value):
        self.terminal.task = value

    @property
    def terminal_cols(self):
        return self.terminal.cols

    @terminal_cols.setter
    def terminal_cols(self, value):
        self.terminal.cols = value

    @property
    def terminal_rows(self):
        return self.terminal.rows

    @terminal_rows.setter
    def terminal_rows(self, value):
        self.terminal.rows = value

    async def handle_terminal_start(self, msg: dict) -> None:
        await self.terminal.start(msg)

    async def handle_browser_reattach(self, msg: dict) -> None:
        await self.terminal.browser_reattach(msg)

    async def handle_terminal_input(self, msg: dict) -> None:
        await self.terminal.input(msg)

    async def handle_terminal_resize(self, msg: dict) -> None:
        await self.terminal.resize(msg)

    async def handle_terminal_stop(self) -> None:
        await self.terminal.stop_command()

    async def handle_terminal_new_window(self, msg: dict) -> None:
        await self.terminal.new_window(msg)

    async def handle_terminal_select_window(self, msg: dict) -> None:
        await self.terminal.select_window(msg)

    async def handle_terminal_close_window(self, msg: dict) -> None:
        await self.terminal.close_window(msg)

    async def handle_terminal_rename_window(self, msg: dict) -> None:
        await self.terminal.rename_window(msg)

    async def handle_terminal_list_windows(self) -> None:
        await self.terminal.list_windows()

    def tmux_session_name(self) -> str:
        return self.terminal.tmux_session_name()

    def sync_terminal_windows(self, windows: list[dict]) -> None:
        self.terminal.sync_terminal_windows(windows)

    def _notify_user_terminal_windows(self, windows: list[dict]) -> None:
        self.terminal.notify_user_terminal_windows(windows)

    async def activate_session(self, session: TerminalSession) -> bool:
        return await self.terminal.activate_session(session)

    async def stop_terminal(self) -> None:
        await self.terminal.stop()

    async def forward_terminal_output(self, session: TerminalSession) -> None:
        await self.terminal.forward_output(session)

    async def _claim_and_stop_terminal(self) -> None:
        await self.terminal.claim_and_stop()

    # --- Exec sessions (delegates to ExecController) ---

    # Backwards-compatible proxies for the state formerly held on
    # Connection itself.  Reads and writes are forwarded to the
    # controller so existing callers (and tests) that read/write
    # ``exec_session``/``exec_task`` directly keep working unchanged.
    @property
    def exec_session(self):
        return self.exec.session

    @exec_session.setter
    def exec_session(self, value):
        self.exec.session = value

    @property
    def exec_task(self):
        return self.exec.task

    @exec_task.setter
    def exec_task(self, value):
        self.exec.task = value

    async def handle_exec_start(self, msg: dict) -> None:
        await self.exec.start(msg)

    async def handle_exec_input(self, msg: dict) -> None:
        await self.exec.input(msg)

    async def handle_exec_close_stdin(self) -> None:
        await self.exec.close_stdin()

    async def handle_exec_stop(self) -> None:
        await self.exec.stop_command()

    async def stop_exec(self) -> None:
        await self.exec.stop()

    async def forward_exec_output(self, session: ExecSession) -> None:
        await self.exec.forward_output(session)

    async def _claim_and_stop_exec(self) -> None:
        await self.exec.claim_and_stop()

    async def start_workspace_container(
        self, workspace_id: str, workspace: dict
    ) -> None:
        """Start/restart container for a workspace."""
        home_path = str(
            self.app.state.workspaces.get_home_host_path(workspace_id)
        )
        cfg_path = str(
            self.app.state.workspaces.get_config_host_path(workspace_id)
        )

        # Home layout (#2169 chunk 2, #2720). Per-handle (the default):
        # ensure the /home/{handle} -> .users/{user_id} symlink exists
        # BEFORE starting the container, because mounts under
        # /home/{handle}/ need the symlink in place so podman doesn't
        # auto-create a real dir. Shared: every connection (and the
        # ``service`` session) uses the one shared /home/klangk — no
        # per-user symlink, no .users/{uid} dirs, no per-user skel
        # (``ensure_shared_home`` populates /home/klangk at every fresh
        # container create, under both layouts), and no handle lookup
        # (the handle is irrelevant on this path).
        if workspace.get("per_handle_home", True):
            handle = await self.app.state.model.users.get_user_handle(
                self.user["id"]
            )
            workspace_home = self.app.state.workspaces.home_path(workspace_id)
            (
                self._user_home,
                self._home_created,
            ) = await self.app.state.workspaces.ensure_home_symlink(
                workspace_home, handle, self.user["id"]
            )
        else:
            self._user_home = SHARED_HOME
            self._home_created = False

        hosting_hostname, hosting_proto, hosting_base_path = (
            self.app.state.util.derive_hosting_info(
                self.sock.headers,
                self.sock.client.host if self.sock.client else None,
            )
        )
        (
            container_id,
            container_status,
        ) = await self.app.state.container_registry.start_container(
            container.ContainerStartSpec(
                workspace_id=workspace_id,
                home_path=home_path,
                existing_container_id=workspace.get("container_id"),
                num_ports=workspace.get(
                    "num_ports", container.DEFAULT_PORTS_PER_WORKSPACE
                ),
                hosting_hostname=hosting_hostname,
                hosting_proto=hosting_proto,
                hosting_base_path=hosting_base_path,
                image=workspace.get("image"),
                config_path=cfg_path,
                extra_mounts=workspace.get("mounts"),
                extra_env=workspace.get("env"),
                user_id=self.user["id"],
                health_check=workspace.get("health_check"),
                setup_state=workspace.get("setup_state"),
                service_command=workspace.get("service_command"),
                allowed_domains=workspace.get("allowed_domains"),
                rejected_domains=workspace.get("rejected_domains"),
                workspace_settings=workspace.get("settings"),
                egress_mode=workspace.get(
                    "egress_mode", model.EGRESS_MODE_INTERACTIVE
                ),
                audit_cause=CAUSE_WS_CONNECT,
                audit_actor_id=self.user["id"],
                per_handle_home=workspace.get("per_handle_home", True),
            )
        )
        self.container_status = container_status
        self.workspace_id = workspace_id
        self.container_id = container_id
        self._service_command = workspace.get("service_command")

        session = self.app.state.sockets.get_or_create_session(
            workspace_id, app=self.app
        )
        token_expiry = datetime.now(timezone.utc) + timedelta(
            hours=self.app.state.auth.workspace_token_expire_hours
        )
        await session.add_subscriber(
            self.sock, container_id, token_expiry=token_expiry
        )

        # Register idle timeout notification (per-connection)
        sock = self.sock

        async def on_idle(wid: str) -> None:
            try:
                send_event(sock, "container_stopped", "idle timeout")
            except WS_ERRORS:
                pass

        self._idle_cb = on_idle
        # No await between lock release and callback registration — the idle
        # loop cannot interleave here in asyncio's single-threaded model.
        # If an await is added before on_idle_stop, move registration inside the lock.
        self.app.state.container_registry.on_idle_stop(workspace_id, on_idle)

        # Cache workspace info for auto-restart
        self.workspace = workspace

        # Clear any stale pending_status_msg from a prior connect/restart.
        self.pending_status_msg = None

        # Populate skeleton if this is a new user home (symlink was
        # created above, before container start).
        if self._home_created:
            await self.app.state.workspaces.populate_home_skel(
                container_id, self.user["id"]
            )

        logger.info("Container ready for workspace %s", workspace_id)

    async def handle_workspace_connect(self, msg: dict) -> None:

        t_connect_start = time.monotonic()
        workspace_id = msg.get("workspaceId")
        if not workspace_id:
            send_error(self.sock, "Missing workspaceId")
            return

        principals = await self.app.state.acl.get_principals(self.user["id"])
        if not await self.app.state.acl.check_permission(
            f"/workspaces/{workspace_id}", principals, "terminal"
        ):
            # #2891: machine-readable ``forbidden`` so clients can swap
            # the restart/overlay loop for an access-revoked view instead
            # of matching the message text.
            send_error(self.sock, "Permission denied", code="forbidden")
            return
        workspace = await self.app.state.workspaces.get_workspace(workspace_id)
        if workspace is None:
            send_error(self.sock, "Workspace not found", code="not_found")
            return

        logger.info(
            "workspace-open: check permissions and fetch workspace "
            "from DB: %.3fs",
            time.monotonic() - t_connect_start,
        )

        await self.handle_workspace_disconnect()

        t_container = time.monotonic()
        try:
            await self.start_workspace_container(workspace_id, workspace)
        except ValueError as exc:
            send_error(self.sock, str(exc))
            return
        except NodeDrainingError as exc:
            # Draining node (#2527): a graceful restart is in progress
            # and new starts are disabled; existing workspaces keep
            # running. Error frame, not a drop.
            send_error(self.sock, str(exc))
            return
        except WorkspaceCapacityError as exc:
            # Capacity refusal (#2525): the host cannot fit the
            # workspace's memory limit or the user hit the running
            # quota. Error frame with the actionable message ("stop a
            # workspace first / free host memory") and a machine-
            # readable ``code`` so the UI can render it as a capacity
            # refusal rather than a generic failure — not a drop; the
            # client stays connected and can retry once capacity frees.
            send_error(self.sock, str(exc), code="capacity")
            return
        logger.info(
            "workspace-open: start or reuse container "
            "(see breakdown above): %.3fs",
            time.monotonic() - t_container,
        )

        t_post = time.monotonic()
        ports = await self.app.state.container_registry.get_workspace_ports(
            workspace_id
        )
        status = getattr(self, "container_status", "created")
        container_name, ports_str = format_container_info(
            workspace_id,
            ports,
            self.app.state.util.instance_id(),
            (self.workspace or {}).get("name") or "",
        )
        status_msg = {
            "connected": f"Connected to running container "
            f"{container_name}{ports_str}",
            "restarted": f"Restarted stopped container "
            f"{container_name}{ports_str}",
            "created": f"Created new container {container_name}{ports_str}",
        }.get(status, "Container ready")
        status_msg += format_idle_timeout(
            self.app.state.container_registry.idle_timeout_seconds
        )

        self.sock.send_json(
            {
                "type": "container_ready",
                "workspaceId": workspace_id,
                "userId": self.user["id"],
                "ports": ports,
                "serviceCommand": workspace.get("service_command"),
                "userHome": self._user_home,
            }
        )

        logger.info(
            "workspace-open: send members and shared terminals to "
            "client: %.3fs",
            time.monotonic() - t_post,
        )

        self.pending_status_msg = status_msg
        logger.info(
            "workspace-open: TOTAL workspace connect (user sees "
            "container_ready after this): %.3fs",
            time.monotonic() - t_connect_start,
        )
        logger.info(
            "User %s connected to workspace %s (ports %s)",
            self.user["email"],
            workspace_id,
            ports,
        )

    async def handle_workspace_disconnect(self) -> None:
        await self.cleanup()
        self.workspace_id = None
        self.container_id = None

    async def handle_restart_container(self) -> None:
        """Restart a stopped container (e.g., after idle timeout)."""
        if not self.workspace_id:
            send_error(self.sock, "Not connected to a workspace")
            return
        # Restarting affects everyone in the workspace; require the
        # #2946 lifecycle permission (terminal is the connect gate only).
        if not await self.has_perm("restart-workspace"):
            # #2891: same machine-readable refusal as workspace_connect.
            send_error(self.sock, "Permission denied", code="forbidden")
            return

        # Save before cleanup — cleanup clears state fields.
        workspace_id = self.workspace_id

        send_event(self.sock, "container_restart", "Restarting container...")

        try:
            await self.cleanup()
        except WS_ERRORS as e:
            logger.warning("Cleanup error during restart: %s", e)

        # Always read the workspace fresh from the DB (#2676): the cached
        # self.workspace dict can carry a stale container_id (an unclean
        # host shutdown/restart can leave the running container under a new
        # id), which sends the restart down the create path into a network
        # sidecar collision instead of the reuse path a reconnect takes.
        # Like handle_workspace_connect, read without the owner filter —
        # access is already gated by the terminal-permission ACL check
        # above, and an owner-only read would break restart for shared
        # workspaces' non-owner members.
        workspace = await self.app.state.workspaces.get_workspace(workspace_id)
        if workspace is None:
            send_error(self.sock, "Workspace not found", code="not_found")
            return

        try:
            await self.start_workspace_container(workspace_id, workspace)
        except NodeDrainingError as exc:
            # Draining node (#2527) — same clear refusal on the WS restart
            # path as the API's 503.
            send_error(self.sock, str(exc))
            return
        except WorkspaceCapacityError as exc:
            # Capacity refusal (#2525) — same clear refusal on the WS
            # restart path as the API's 503, with the machine-readable
            # capacity code.
            send_error(self.sock, str(exc), code="capacity")
            return
        except (PodmanError, ValueError) as exc:
            # A failed (re)start must not drop the whole WebSocket with a
            # traceback (#2676) — the user's session survives and can retry;
            # the error frame surfaces the actionable podman message.
            send_error(self.sock, f"Container restart failed: {exc}")
            return
        await self._announce_restarted_container(workspace_id)

    async def _announce_restarted_container(self, workspace_id) -> None:
        """Record activity, re-bind sibling connections to the new
        container, and send the container_ready frame."""
        self.app.state.container_registry.record_activity(self.container_id)

        # Update container_id on ALL connections to this workspace
        # so they don't try to exec into the old (removed) container.
        new_cid = self.container_id
        for sock, conn in list(self.app.state.sockets.connections.items()):
            if conn.workspace_id == workspace_id and conn is not self:
                conn.container_id = new_cid

        ports = await self.app.state.container_registry.get_workspace_ports(
            workspace_id
        )
        container_name, ports_str = format_container_info(
            workspace_id,
            ports,
            self.app.state.util.instance_id(),
            (self.workspace or {}).get("name") or "",
        )
        status_msg = f"Container restarted {container_name}{ports_str}"

        timeout_mins = (
            self.app.state.container_registry.idle_timeout_seconds / 60
        )
        if timeout_mins == int(timeout_mins):
            status_msg += f" — idle timeout: {int(timeout_mins)}m"
        else:
            status_msg += f" — idle timeout: {timeout_mins:.1f}m"

        send_event(self.sock, "container_ready", status_msg)

        logger.info(
            "Container restarted via restart_container command for workspace %s",
            workspace_id,
        )

    async def has_perm(self, perm: str) -> bool:
        """Check if the connected user has a workspace permission."""
        if not self.workspace_id:
            return False
        principals = await self.app.state.acl.get_principals(self.user["id"])
        return await self.app.state.acl.check_permission(
            f"/workspaces/{self.workspace_id}", principals, perm
        )

    # --- Shared terminals (delegates to SharedTerminalController) ---

    @property
    def viewing_shared(self):
        return self.shared.viewing_shared

    @viewing_shared.setter
    def viewing_shared(self, value):
        self.shared.viewing_shared = value

    def _find_window(
        self,
        ws_session: WorkspaceSession,
        user_id: str,
        window_id: str,
        *,
        shared: bool = False,
        error_msg: str = "Window not found",
    ) -> dict | None:
        return self.shared.find_window(
            ws_session,
            user_id,
            window_id,
            shared=shared,
            error_msg=error_msg,
        )

    async def handle_share_window(self, msg: dict) -> None:
        await self.shared.share_window(msg)

    async def handle_unshare_window(self, msg: dict) -> None:
        await self.shared.unshare_window(msg)

    async def handle_join_shared_terminal(self, msg: dict) -> None:
        await self.shared.join_shared_terminal(msg)

    async def handle_list_shared_terminals(self) -> None:
        await self.shared.list_shared_terminals()

    def broadcast_shared_terminals(self, ws_session) -> None:
        self.shared.broadcast_shared_terminals(ws_session)

    async def handle_create_shared_terminal(self, msg: dict) -> None:
        await self.shared.create_shared_terminal(msg)

    async def handle_delete_shared_terminal(self, msg: dict) -> None:
        await self.shared.delete_shared_terminal(msg)

    async def _handle_list_error(self, e: Exception) -> None:
        await self.shared.handle_list_error(e)

    # --- SSH agent forwarding ---

    async def handle_heartbeat(self) -> None:
        if self.container_id is not None:
            self.app.state.container_registry.record_activity(
                self.container_id
            )

    async def handle_ui_ready(self) -> None:
        if self.workspace_id:
            sess = self.app.state.sockets.get_session(self.workspace_id)
            if sess:
                sess.browser_subscribers.add(self.sock)
        status_msg = self.pending_status_msg
        self.pending_status_msg = None
        if status_msg:
            send_event(self.sock, "container_ready", status_msg)
        # Send shared terminal list from in-memory state.
        ws_session = self.app.state.sockets.get_session(self.workspace_id)
        if ws_session:
            terminals = get_shared_terminals(
                ws_session, self.app.state.sockets
            )
            self.sock.send_json(
                {"type": "shared_terminals", "terminals": terminals}
            )

    async def handle_set_handle(self, msg: dict) -> None:
        handle = msg.get("handle", "").strip()
        if not self.workspace_id:
            send_error(self.sock, "Not connected to a workspace")
            return
        try:
            await self.app.state.model.users.set_user_handle(
                self.user["id"], handle
            )
            # Update the per-workspace symlink (per-handle layout only;
            # the shared layout has no per-user symlink and its home is
            # the constant SHARED_HOME — nothing to refresh, #2720).
            workspace = self.workspace
            if workspace and workspace.get("per_handle_home", True):
                workspace_home = self.app.state.workspaces.home_path(
                    self.workspace_id
                )
                (
                    container_home,
                    created,
                ) = await self.app.state.workspaces.ensure_home_symlink(
                    workspace_home, handle, self.user["id"]
                )
                if created and self.container_id:
                    await self.app.state.workspaces.populate_home_skel(
                        self.container_id,
                        self.user["id"],
                    )
                self._user_home = container_home
            self.sock.send_json(
                {
                    "type": "handle_set",
                    "handle": handle,
                    "home": self._user_home,
                }
            )
        except ValueError as exc:
            self.sock.send_json(
                {
                    "type": "handle_error",
                    "error": str(exc),
                }
            )

    async def cleanup(self) -> None:
        # Remove idle callback
        workspace_id = self.workspace_id
        idle_cb = self._idle_cb
        if workspace_id and idle_cb:
            self.app.state.container_registry.remove_idle_callback(
                workspace_id, idle_cb
            )
            self._idle_cb = None

        # Revoke per-connection browser registrations
        self.app.state.container_registry.revoke_browser(self.sock)
        self.browser_id = None

        await self.stop_terminal()
        await self.stop_exec()
        await self._stop_ssh_agent()

        # Remove this connection from the workspace session's subscriber sets.
        # If no subscribers remain, remove the session entirely. The container
        # is NOT killed — idle timeout handles that.
        session = (
            self.app.state.sockets.get_session(workspace_id)
            if workspace_id
            else None
        )
        if session:
            empty = await session.remove_subscriber(self.sock)
            if empty:
                # Lock is released by remove_subscriber, so use the
                # lock-acquiring version.
                await self.app.state.sockets.remove_session(workspace_id)
