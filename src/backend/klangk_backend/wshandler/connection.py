"""Connection: per-WebSocket connection state and command handlers."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone


from .. import acl as _acl
from .. import container, model
from ..terminal import TerminalSession
from ..podman import ExecSession
from .constants import (
    agent_conversations,
    agent_tasks,
    cancel_agent_task,
)
from .safe_websocket import SafeWebSocket, WS_ERRORS
from .helpers import (
    send_error,
    send_event,
    format_idle_timeout,
    format_container_info,
    get_presence_list,
    get_shared_terminals,
)
from .agent_mention import (
    handle_agent_mention,
    mentions_agent,
    addresses_other_user,
)
from .session import WorkspaceSession
from .controllers import (
    SshAgentForwarder,
    ExecController,
    TerminalController,
    SharedTerminalController,
)

logger = logging.getLogger(__name__)


class Connection:
    """Per-WebSocket connection state and command handlers."""

    def __init__(self, ws: SafeWebSocket, user: dict, app_state):
        self.app_state = app_state
        self.sock = ws
        self.user = user
        self.workspace_id: str | None = None
        self.container_id: str | None = None
        # Terminal sessions are owned by the TerminalController
        # collaborator; Connection delegates the terminal_* commands to
        # it.  The ``terminal_session``/``terminal_task`` (and
        # ``_terminal_cols``/``_terminal_rows``) properties below proxy
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
        self._terminal_cols: int = 80
        self._terminal_rows: int = 24
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
        # it.  The ``_ssh_agent_*`` properties below proxy to the
        # forwarder for backwards compatibility with code (and tests)
        # that read/write those fields directly.
        self.ssh_agent = SshAgentForwarder(self)

    # --- SSH agent forwarding (delegates to SshAgentForwarder) ---

    # Backwards-compatible proxies for the state formerly held on
    # Connection itself.  Reads and writes are forwarded to the
    # collaborator so existing callers (and the terminal/exec code
    # that consumes ``_ssh_agent_socket``) keep working unchanged.
    @property
    def _ssh_agent_proc(self):
        return self.ssh_agent.proc

    @_ssh_agent_proc.setter
    def _ssh_agent_proc(self, value):
        self.ssh_agent.proc = value

    @property
    def _ssh_agent_task(self):
        return self.ssh_agent.task

    @_ssh_agent_task.setter
    def _ssh_agent_task(self, value):
        self.ssh_agent.task = value

    @property
    def _ssh_agent_socket(self):
        return self.ssh_agent.socket

    @_ssh_agent_socket.setter
    def _ssh_agent_socket(self, value):
        self.ssh_agent.socket = value

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

    async def _log_ssh_agent_stderr(self) -> None:
        await self.ssh_agent.log_stderr()

    # --- Terminal sessions (delegates to TerminalController) ---

    # Backwards-compatible proxies for the state formerly held on
    # Connection itself.  Reads and writes are forwarded to the
    # controller so existing callers (and tests) that read/write
    # ``terminal_session``/``terminal_task``/``_terminal_cols``/
    # ``_terminal_rows`` directly keep working unchanged.
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
    def _terminal_cols(self):
        return self.terminal.cols

    @_terminal_cols.setter
    def _terminal_cols(self, value):
        self.terminal.cols = value

    @property
    def _terminal_rows(self):
        return self.terminal.rows

    @_terminal_rows.setter
    def _terminal_rows(self, value):
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

    async def activate_session(
        self, session: TerminalSession, cols: int, rows: int
    ) -> bool:
        return await self.terminal.activate_session(session, cols, rows)

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
        host_path = str(
            self.app_state.workspaces.get_workspace_host_path(workspace_id)
        )
        home_path = str(
            self.app_state.workspaces.get_home_host_path(workspace_id)
        )
        cfg_path = str(
            self.app_state.workspaces.get_config_host_path(workspace_id)
        )

        # Ensure the per-user home symlink exists BEFORE starting the
        # container, because mounts under /home/{handle}/ need the
        # symlink in place so podman doesn't auto-create a real dir.
        handle = await self.app_state.model.users.get_user_handle(
            self.user["id"]
        )
        workspace_home = self.app_state.workspaces.home_path(workspace_id)
        (
            self._user_home,
            self._home_created,
        ) = await self.app_state.workspaces.ensure_home_symlink(
            workspace_home, handle, self.user["id"]
        )

        hosting_hostname, hosting_proto, hosting_base_path = (
            self.app_state.util.derive_hosting_info(
                self.sock.headers,
                self.sock.client.host if self.sock.client else None,
            )
        )
        (
            container_id,
            container_status,
        ) = await self.app_state.container_registry.start_container(
            workspace_id,
            host_path,
            home_path,
            workspace.get("container_id"),
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
        )
        self.container_status = container_status
        self.workspace_id = workspace_id
        self.container_id = container_id
        self._service_command = workspace.get("service_command")

        session = self.app_state.sockets.get_or_create_session(
            workspace_id, app_state=self.app_state
        )
        token_expiry = datetime.now(timezone.utc) + timedelta(
            hours=self.app_state.auth.workspace_token_expire_hours
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
        self.app_state.container_registry.on_idle_stop(workspace_id, on_idle)

        # Cache workspace info for auto-restart
        self.workspace = workspace

        # Clear any stale pending_status_msg from a prior connect/restart.
        self.pending_status_msg = None

        # Populate skeleton if this is a new user home (symlink was
        # created above, before container start).
        if self._home_created:
            await self.app_state.workspaces.populate_home_skel(
                container_id, self.user["id"]
            )

        logger.info("Container ready for workspace %s", workspace_id)

    async def _send_chat_history(self, workspace_id: str) -> None:
        """Send chat history to the connecting user."""
        chat_history = await model.get_chat_messages(workspace_id)
        if chat_history:
            self.sock.send_json(
                {"type": "chat_history", "messages": chat_history}
            )

    async def _broadcast_join(
        self, workspace_id: str, rejoining: bool
    ) -> None:
        """Send presence list and broadcast join to other subscribers."""
        presence = await get_presence_list(
            workspace_id, self.app_state.sockets
        )
        self.sock.send_json({"type": "presence_list", "users": presence})
        session = self.app_state.sockets.get_session(workspace_id)
        if session and not rejoining:
            join_msg = {
                "type": "presence_join",
                "user_id": self.user["id"],
                "user_email": self.user["email"],
                "user_handle": self.user["handle"],
            }
            for sock in list(session.subscribers):
                if sock is not self.sock:
                    sock.send_json(join_msg)

            sys_msg = await model.add_chat_message(
                workspace_id,
                self.user["id"],
                self.user["email"],
                f"{self.user.get('handle') or self.user['email']} joined",
                message_type=model.MSG_SYSTEM,
            )
            session.broadcast({"type": "chat_message", **sys_msg})

    async def handle_workspace_connect(self, msg: dict) -> None:

        t_connect_start = time.monotonic()
        workspace_id = msg.get("workspaceId")
        if not workspace_id:
            send_error(self.sock, "Missing workspaceId")
            return

        principals = await _acl.get_principals(self.user["id"])
        if not await _acl.check_permission(
            f"/workspaces/{workspace_id}", principals, "terminal"
        ):
            send_error(self.sock, "Permission denied")
            return
        workspace = await self.app_state.workspaces.get_workspace(workspace_id)
        if workspace is None:
            send_error(self.sock, "Workspace not found")
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
        logger.info(
            "workspace-open: start or reuse container "
            "(see breakdown above): %.3fs",
            time.monotonic() - t_container,
        )

        t_post = time.monotonic()
        ports = await self.app_state.container_registry.get_workspace_ports(
            workspace_id
        )
        status = getattr(self, "container_status", "created")
        container_name, ports_str = format_container_info(
            workspace_id, ports, self.app_state.util.instance_id()
        )
        status_msg = {
            "connected": f"Connected to running container "
            f"{container_name}{ports_str}",
            "restarted": f"Restarted stopped container "
            f"{container_name}{ports_str}",
            "created": f"Created new container {container_name}{ports_str}",
        }.get(status, "Container ready")
        status_msg += format_idle_timeout(
            self.app_state.container_registry.idle_timeout_seconds
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

        await self._send_chat_history(workspace_id)

        if self.container_id:
            asyncio.create_task(self._start_agent_if_needed())

        rejoining = self.app_state.sockets.cancel_pending_leave(
            workspace_id, self.user["id"]
        )
        await self._broadcast_join(workspace_id, rejoining)

        logger.info(
            "workspace-open: send chat history, members, and "
            "presence to client: %.3fs",
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
        # Restarting affects everyone in the workspace; require admin.
        if not await self._has_perm("admin"):
            send_error(self.sock, "Permission denied")
            return

        # Save before cleanup — cleanup clears state fields.
        workspace_id = self.workspace_id
        user = self.user
        workspace = self.workspace

        send_event(self.sock, "container_restart", "Restarting container...")

        try:
            await self.cleanup()
        except WS_ERRORS as e:
            logger.warning("Cleanup error during restart: %s", e)

        if workspace is None:
            workspace = await self.app_state.workspaces.get_workspace(
                workspace_id, user["id"]
            )
        if workspace is None:
            send_error(self.sock, "Workspace not found")
            return

        await self.start_workspace_container(workspace_id, workspace)
        self.app_state.container_registry.record_activity(self.container_id)

        # Update container_id on ALL connections to this workspace
        # so they don't try to exec into the old (removed) container.
        new_cid = self.container_id
        for sock, conn in list(self.app_state.sockets.connections.items()):
            if conn.workspace_id == workspace_id and conn is not self:
                conn.container_id = new_cid

        ports = await self.app_state.container_registry.get_workspace_ports(
            workspace_id
        )
        container_name, ports_str = format_container_info(
            workspace_id, ports, self.app_state.util.instance_id()
        )
        status_msg = f"Container restarted {container_name}{ports_str}"

        timeout_mins = (
            self.app_state.container_registry.idle_timeout_seconds / 60
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

    async def handle_shutdown_container(self) -> None:
        """Explicitly shut down the workspace container."""
        if not self.workspace_id:
            send_error(self.sock, "Not connected to a workspace")
            return
        # Shutting down affects everyone in the workspace; require admin.
        if not await self._has_perm("admin"):
            send_error(self.sock, "Permission denied")
            return
        if not self.container_id:
            send_error(self.sock, "No container running")
            return

        workspace_id = self.workspace_id
        container_id = self.container_id
        session = self.app_state.sockets.get_session(workspace_id)

        # Clear container_id on ALL connections to prevent stale exec attempts.
        for sock, conn_obj in list(self.app_state.sockets.connections.items()):
            if conn_obj.workspace_id == workspace_id:
                conn_obj.container_id = None

        await self.app_state.container_registry.notify_workspace_killed(
            workspace_id
        )

        try:
            await self.app_state.container_registry.stop_and_remove_container(
                container_id
            )
        except Exception as e:
            logger.warning("Error stopping container: %s", e)

        # Stop the Pi RPC subprocess now that its container is gone.
        await self.app_state.agents.stop_session(workspace_id)

        # Notify subscribers AFTER the container is fully stopped, so
        # reconnecting clients don't find a half-dead container.
        if session:
            session.broadcast(
                {
                    "type": "event",
                    "event": {
                        "type": "CUSTOM",
                        "name": "container_stopped",
                        "value": {"reason": "shut down by user"},
                    },
                }
            )

        logger.info(
            "Container shut down by user for workspace %s", workspace_id
        )

    async def _has_perm(self, perm: str) -> bool:
        """Check if the connected user has a workspace permission."""
        if not self.workspace_id:
            return False
        principals = await _acl.get_principals(self.user["id"])
        return await _acl.check_permission(
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
            self.app_state.container_registry.record_activity(
                self.container_id
            )

    async def handle_chat_send(self, msg: dict) -> None:
        workspace_id = self.workspace_id
        if not workspace_id:
            send_error(self.sock, "Not connected to a workspace")
            return
        # Defense in depth: chat is reachable here by anyone connected,
        # but it can drive the agent -- an @mention (or follow-up) routes to
        # a subprocess that can make workspace changes -- so it is a
        # privileged channel, not a passive one. Require the ``chat``
        # permission at the send path rather than merely assuming it from
        # role membership (#1136). Spectators no longer receive ``chat``
        # (see _ROLE_GROUP_PERMISSIONS). NOTE: this reads the materialized
        # ACL, so it does not by itself repair workspaces seeded before
        # that change (their stale spectators ``chat`` ACE still passes);
        # no migration is shipped -- no production deployments yet -- so
        # the role change covers new workspaces and this gate enforces the
        # model going forward.
        if not await self._has_perm("chat"):
            send_error(self.sock, "chat_send requires chat permission")
            return
        text = msg.get("message", "").strip()
        if not text:
            return
        chat_msg = await model.add_chat_message(
            workspace_id, self.user["id"], self.user["email"], text
        )
        session = self.app_state.sockets.get_session(workspace_id)
        if session:
            session.broadcast({"type": "chat_message", **chat_msg})

        # Route to agent on @mention or natural follow-up.
        #
        # After an @mention, the same user's messages route to the
        # agent indefinitely until someone else speaks (interjection).
        # After interjection, a 30s window applies — follow-ups from
        # the original user still route within that window.  Messages
        # starting with @someone-else always break the conversation.
        should_route = False
        user_id = self.user["id"]
        conv = agent_conversations.get(workspace_id)

        if await mentions_agent(text):
            should_route = True
            agent_conversations[workspace_id] = {
                "user_id": user_id,
                "time": time.monotonic(),
                "interjected": False,
            }
        elif conv and not await addresses_other_user(text):
            if user_id == conv["user_id"]:
                if not conv["interjected"]:
                    # No interjection — route indefinitely
                    should_route = True
                    conv["time"] = time.monotonic()
                elif time.monotonic() - conv["time"] < 30:
                    # Interjected but within 30s window
                    should_route = True
                    conv["time"] = time.monotonic()
                else:
                    # Window expired
                    del agent_conversations[workspace_id]
            else:
                # Different human speaking — mark interjection
                conv["interjected"] = True
        elif conv:
            # Addressed to someone else — break conversation
            del agent_conversations[workspace_id]

        if should_route and self.container_id:
            # One agent-run slot per workspace: cancel any in-flight run
            # so concurrent @mentions don't orphan the earlier task.
            # Pass the asking user's identity so the agent can resolve
            # "my" (its process has no user identity of its own).
            cancel_agent_task(workspace_id)
            agent_tasks[workspace_id] = asyncio.create_task(
                handle_agent_mention(
                    self.app_state.sockets,
                    workspace_id,
                    self.container_id,
                    text,
                    user_id=self.user.get("id"),
                    user_handle=self.user.get("handle"),
                    user_home=self._user_home,
                )
            )

    async def handle_chat_delete(self, msg: dict) -> None:
        workspace_id = self.workspace_id
        if not workspace_id:
            return
        message_id = msg.get("message_id", "")
        if not message_id:
            return
        deleted = await model.delete_chat_message(message_id, self.user["id"])
        if deleted:
            session = self.app_state.sockets.get_session(workspace_id)
            if session:
                session.broadcast(
                    {
                        "type": "chat_updated",
                        "message_id": message_id,
                        "message": "<message deleted by author>",
                    }
                )

    async def handle_chat_load_more(self, msg: dict) -> None:
        workspace_id = self.workspace_id
        if not workspace_id:
            return
        before_id = msg.get("before_id", "")
        if not before_id:
            return
        limit = min(msg.get("limit", 50), 100)
        messages = await model.get_chat_messages_before(
            workspace_id, before_id, limit
        )
        self.sock.send_json(
            {
                "type": "chat_history_page",
                "messages": messages,
                "has_more": len(messages) == limit,
            }
        )

    async def _start_agent_if_needed(self) -> None:
        """Start the Pi RPC agent so it shows in presence."""
        try:
            session = await self.app_state.agents.get_session(
                self.workspace_id
            )
            await session.ensure_started()
            # Broadcast updated presence now that agent is alive
            if self.workspace_id:
                ws_session = self.app_state.sockets.get_session(
                    self.workspace_id
                )
                if ws_session:
                    presence = await get_presence_list(
                        self.workspace_id, self.app_state.sockets
                    )
                    ws_session.broadcast(
                        {"type": "presence_list", "users": presence}
                    )
        except Exception:
            logger.debug("Failed to start agent eagerly", exc_info=True)

    async def handle_chat_agent_abort(self) -> None:
        workspace_id = self.workspace_id
        if not workspace_id:
            return
        cancel_agent_task(workspace_id)

    async def handle_ui_ready(self) -> None:
        if self.workspace_id:
            sess = self.app_state.sockets.get_session(self.workspace_id)
            if sess:
                sess.browser_subscribers.add(self.sock)
        status_msg = self.pending_status_msg
        self.pending_status_msg = None
        if status_msg:
            send_event(self.sock, "container_ready", status_msg)
        # Send shared terminal list from in-memory state.
        ws_session = self.app_state.sockets.get_session(self.workspace_id)
        if ws_session:
            terminals = get_shared_terminals(
                ws_session, self.app_state.sockets
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
            await self.app_state.model.users.set_user_handle(
                self.user["id"], handle
            )
            # Update the per-workspace symlink.
            workspace = self.workspace
            if workspace:
                workspace_home = self.app_state.workspaces.home_path(
                    self.workspace_id
                )
                (
                    container_home,
                    created,
                ) = await self.app_state.workspaces.ensure_home_symlink(
                    workspace_home, handle, self.user["id"]
                )
                if created and self.container_id:
                    await self.app_state.workspaces.populate_home_skel(
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
            self.app_state.container_registry.remove_idle_callback(
                workspace_id, idle_cb
            )
            self._idle_cb = None

        # Revoke per-connection browser registrations
        self.app_state.container_registry.revoke_browser(self.sock)
        self.browser_id = None

        await self.stop_terminal()
        await self.stop_exec()
        await self._stop_ssh_agent()

        # Remove this connection from the workspace session's subscriber sets.
        # If no subscribers remain, remove the session entirely. The container
        # is NOT killed — idle timeout handles that.
        session = (
            self.app_state.sockets.get_session(workspace_id)
            if workspace_id
            else None
        )
        if session:
            empty = await session.remove_subscriber(self.sock)
            if not empty:
                # Schedule a debounced presence_leave if user has no
                # other connections.  If they reconnect within the delay
                # window the leave (and re-join) are suppressed.
                still_connected = any(
                    self.app_state.sockets.connections.get(s) is not None
                    and self.app_state.sockets.connections[s].user["id"]
                    == self.user["id"]
                    for s in session.subscribers
                )
                if not still_connected:
                    self.app_state.sockets.schedule_pending_leave(
                        workspace_id, self.user, session
                    )
            else:
                # Lock is released by remove_subscriber, so use the
                # lock-acquiring version.
                await self.app_state.sockets.remove_session(workspace_id)
