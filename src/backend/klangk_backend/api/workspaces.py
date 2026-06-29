"""Workspace routes: CRUD, duplicate, restart, export/import, members, roles, group shares, ACL, and user search."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import (
    StreamingResponse,
)
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from .. import (
    acl,
    auth,
    container,
    model,
    wshandler,
    workspaces,
)
from ..model import (
    ACTION_ALLOW,
    PRINCIPAL_GROUP,
    PRINCIPAL_USER,
)
from ..util import (
    resolve_env_bool,
    sanitize_disposition_name,
)
from ._common import (
    FILE_UPLOAD_SIZE_MAX,
    WorkspaceAclEntry,
    _admin_resource,
    _workspace_resource,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _annotate_running(items: list[dict]) -> list[dict]:
    """Add ``running`` boolean to each workspace dict."""
    for ws in items:
        ws["running"] = container.registry.get_state(ws["id"]) is not None
    return items


@router.get("/workspaces")
async def list_workspaces(
    user: dict = Depends(auth.get_current_user),
    limit: int | None = Query(None, ge=1, le=100),
    offset: int | None = Query(None, ge=0),
    sort: Literal["name", "created"] = Query("created"),
    order: Literal["asc", "desc"] = Query("desc"),
    q: str | None = Query(None),
):
    """List workspaces owned by the user.

    Without ``limit``/``offset`` (backward-compatible) returns a bare list.
    With pagination params returns an envelope
    ``{"items": [...], "has_more": bool, "next_offset": int | None}``.
    ``sort`` (``name``/``created``), ``order`` (``asc``/``desc``) and ``q``
    (name substring) apply in both shapes.
    """
    result = await workspaces.list_workspaces(
        user["id"],
        limit=limit or 10,
        offset=offset or 0,
        sort=sort,
        order=order,
        q=q,
    )
    _annotate_running(result["items"])
    if limit is None and offset is None:
        return result["items"]
    return result


@router.get("/workspaces/shared")
async def list_shared_workspaces(
    user: dict = Depends(auth.get_current_user),
    limit: int | None = Query(None, ge=1, le=100),
    offset: int | None = Query(None, ge=0),
    sort: Literal["name", "created"] = Query("created"),
    order: Literal["asc", "desc"] = Query("desc"),
    q: str | None = Query(None),
):
    """List workspaces shared with the user.

    Without ``limit``/``offset`` (backward-compatible) returns a bare list.
    With pagination params returns an envelope (see ``list_workspaces``).
    """
    result = await model.list_shared_workspaces(
        user["id"],
        limit=limit or 10,
        offset=offset or 0,
        sort=sort,
        order=order,
        q=q,
    )
    _annotate_running(result["items"])
    if limit is None and offset is None:
        return result["items"]
    return result


class CreateWorkspaceRequest(BaseModel):
    name: str
    image: str | None = None
    default_command: str | None = None
    auto_start: bool = False
    mounts: list[str] | None = None
    env: dict[str, str] | None = None


@router.post("/workspaces")
async def create_workspace(
    body: CreateWorkspaceRequest, user: dict = Depends(auth.get_current_user)
):
    if body.auto_start and not resolve_env_bool("KLANGK_ALLOW_AUTOSTART"):
        raise HTTPException(
            status_code=400,
            detail="Auto-start is not enabled on this server"
            " (set KLANGK_ALLOW_AUTOSTART=1)",
        )
    if body.image and body.image not in container.ALLOWED_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Image {body.image!r} is not allowed. "
            f"Allowed: {sorted(container.ALLOWED_IMAGES)}",
        )
    if body.mounts:
        mount_err = container.validate_mounts(body.mounts)
        if mount_err:
            raise HTTPException(status_code=400, detail=mount_err)
    try:
        ws = await workspaces.create_workspace(
            user["id"],
            body.name,
            image=body.image,
            default_command=body.default_command,
            auto_start=body.auto_start,
            mounts=body.mounts,
            env=body.env,
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {body.name!r} already exists",
        )
    except OSError as e:  # pragma: no cover
        raise HTTPException(status_code=400, detail=str(e))
    # Grant owner full access via ACL
    resource = f"/workspaces/{ws['id']}"
    await model.add_acl_entry(
        resource, 0, ACTION_ALLOW, "*", PRINCIPAL_USER, user_id=user["id"]
    )

    # Create workspace role groups and their ACL entries.
    role_groups = {
        f"owners-{ws['id']}": ["*"],
        f"coders-{ws['id']}": [
            "terminal",
            "code-in-isolation",
            "spectate-on-shared-terminals",
            "files",
            "chat",
        ],
        f"collaborators-{ws['id']}": [
            "terminal",
            "code-in-isolation",
            "code-in-shared-terminals",
            "spectate-on-shared-terminals",
            "share-terminals",
            "files",
            "chat",
        ],
        f"spectators-{ws['id']}": [
            "terminal",
            "spectate-on-shared-terminals",
            "chat",
        ],
    }
    pos = 1
    for group_name, perms in role_groups.items():
        group = await model.create_group(
            group_name, description=f"{group_name} for workspace {ws['name']}"
        )
        for perm in perms:
            await model.add_acl_entry(
                resource,
                pos,
                ACTION_ALLOW,
                perm,
                PRINCIPAL_GROUP,
                group_id=group["id"],
            )
            pos += 1
        # Add the creator to the owners group.
        if group_name.startswith("owners-"):
            await model.add_user_to_group(user["id"], group["id"])

    # Eagerly start the container so it's running by the time the
    # user connects.  Errors are logged but don't fail the create.
    if body.auto_start:
        try:
            await workspaces.eager_start_workspace(ws)
        except Exception:
            logger.warning(
                "Eager start failed for workspace %s",
                ws["id"],
                exc_info=True,
            )

    wshandler.state.notify_user_workspaces_changed(user["id"])
    return ws


class UpdateWorkspaceRequest(BaseModel):
    name: str | None = None
    image: str | None = None
    default_command: str | None = None
    auto_start: bool | None = None
    mounts: list[str] | None = None
    env: dict[str, str] | None = None


@router.put("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    body: UpdateWorkspaceRequest,
    user: dict = Depends(acl.has_permission("edit", _workspace_resource)),
):
    fields = body.model_dump(exclude_unset=True)
    if fields.get("auto_start") and not resolve_env_bool(
        "KLANGK_ALLOW_AUTOSTART"
    ):
        raise HTTPException(
            status_code=400,
            detail="Auto-start is not enabled on this server"
            " (set KLANGK_ALLOW_AUTOSTART=1)",
        )
    if "image" in fields and fields["image"] is not None:
        if fields["image"] not in container.ALLOWED_IMAGES:
            raise HTTPException(
                status_code=400,
                detail=f"Image {fields['image']!r} is not allowed. "
                f"Allowed: {sorted(container.ALLOWED_IMAGES)}",
            )
    if "mounts" in fields and fields["mounts"]:
        mount_err = container.validate_mounts(fields["mounts"])
        if mount_err:
            raise HTTPException(status_code=400, detail=mount_err)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    updated = await model.update_workspace(
        workspace_id, workspace["user_id"], **fields
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"status": "updated"}


class DuplicateWorkspaceRequest(BaseModel):
    name: str


@router.post("/workspaces/{workspace_id}/duplicate")
async def duplicate_workspace(
    workspace_id: str,
    body: DuplicateWorkspaceRequest,
    user: dict = Depends(acl.has_permission("create", _workspace_resource)),
):
    source = await model.get_workspace(workspace_id)
    if source is None:  # pragma: no cover — race after ACL check
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        ws = await workspaces.create_workspace(
            user["id"],
            body.name,
            image=source.get("image"),
            default_command=source.get("default_command"),
            auto_start=source.get("auto_start", False),
            mounts=source.get("mounts"),
            env=source.get("env"),
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {body.name!r} already exists",
        )
    # Grant owner full access via ACL
    await model.add_acl_entry(
        f"/workspaces/{ws['id']}",
        0,
        ACTION_ALLOW,
        "*",
        PRINCIPAL_USER,
        user_id=user["id"],
    )
    return ws


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("delete", _workspace_resource)),
):
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Capture shared members before we tear down ACL entries, so we can
    # notify them (and the owner/deleter) that the workspace is gone.
    members = await model.get_workspace_members(workspace_id)

    # Prefer the live container_id from the registry (tracks the currently
    # running container) over the DB value (may be stale if the container
    # was already stopped by idle timeout).
    live_state = container.registry.get_state(workspace_id)
    cid = (
        live_state.container_id
        if live_state
        else workspace.get("container_id")
    )
    # reset_workspace_state (below) stops the agent session and clears
    # shared state; the agent subprocess runs inside the container, so
    # stopping the container kills it either way.
    if cid:
        await container.registry.stop_and_remove_container(cid)
    await wshandler.reset_workspace_state(workspace_id)

    deleted = await workspaces.delete_workspace(
        workspace_id, workspace["user_id"]
    )
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="Workspace not found")
    # Clean up ACL entries for this workspace
    await model.delete_acl_entries_for_resource(f"/workspaces/{workspace_id}")
    # Notify the deleter, the owner, and any shared members so their
    # workspace list refreshes (members were fetched above, before the
    # resource's ACL entries were removed).
    member_ids = {m["id"] for m in members}
    member_ids.update({user["id"], workspace["user_id"]})
    for uid in member_ids:
        wshandler.state.notify_user_workspaces_changed(uid)
    return {"status": "deleted"}


@router.post("/workspaces/{workspace_id}/restart")
async def restart_workspace(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("terminal", _workspace_resource)),
):
    """Restart a workspace container.

    Stops and removes the running container.  The next WebSocket
    connect will start a fresh one with the same workspace config.
    """
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    live_state = container.registry.get_state(workspace_id)
    cid = (
        live_state.container_id
        if live_state
        else workspace.get("container_id")
    )
    if cid:
        await container.registry.stop_and_remove_container(cid)
    await wshandler.reset_workspace_state(workspace_id)
    return {"status": "restarted"}


@router.get("/workspaces/{workspace_id}/status")
async def workspace_status(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("terminal", _workspace_resource)),
):
    """Return container status for a workspace.

    Returns running state, container health, idle timeout info,
    and allocated ports.
    """
    workspace = await model.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    live_state = container.registry.get_state(workspace_id)
    if live_state is None:
        return {
            "running": False,
            "container_id": None,
            "health": None,
            "idle_seconds": None,
            "idle_timeout": None,
            "ports": [],
        }

    idle_secs = time.time() - live_state.last_activity
    idle_timeout = live_state.get_idle_timeout()
    ports = await container.registry.get_workspace_ports(workspace_id)

    return {
        "running": True,
        "container_id": live_state.container_id,
        "health": None,  # placeholder for #1015
        "idle_seconds": round(idle_secs, 1),
        "idle_timeout": idle_timeout,
        "ports": ports,
    }


# --- Workspace export/import endpoints ---


@router.get("/workspaces/{workspace_id}/export")
async def export_workspace(
    workspace_id: str,
    admin: dict = Depends(acl.has_permission("admin", _admin_resource)),
):
    """Export a workspace as a .tar.gz archive (admin only).

    The archive contains workspace.json (metadata) and the home
    directory tree under home/.
    """
    workspace = await model.get_workspace(workspace_id, admin["id"])
    if workspace is None:
        # Admin may not own the workspace — look it up without access check.
        workspace = await model.get_workspace_by_id(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    home_dir = workspaces.home_path(workspace["user_id"], workspace_id)
    ws_name = workspace["name"]

    metadata = workspaces.workspace_metadata(workspace)

    # Estimate uncompressed size for client progress display.
    estimated_size = 0
    if home_dir.exists():
        try:
            result = subprocess.run(
                ["du", "-sb", str(home_dir)],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                estimated_size = int(result.stdout.split()[0])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass  # fall back to 0

    # Stream the tarball using GNU tar piped to stdout. Uses the shared
    # _build_export_tar_args (workspaces.py), same as build_workspace_archive.
    # Symlinks are stored as symlinks (not dereferenced).
    _CHUNK_SIZE = 256 * 1024  # 256 KB read chunks

    async def _stream():
        tmpdir = tempfile.mkdtemp()
        try:
            # Write workspace.json to temp dir
            meta_file = os.path.join(tmpdir, "workspace.json")
            with open(meta_file, "w") as f:
                json.dump(metadata, f, indent=2)

            tar_args = workspaces._build_export_tar_args("-", tmpdir, home_dir)

            proc = await asyncio.create_subprocess_exec(
                *tar_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                while True:
                    chunk = await proc.stdout.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
                await proc.wait()
            finally:
                if proc.returncode is None:  # pragma: no cover
                    proc.kill()
                    await proc.wait()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    safe_name = sanitize_disposition_name(ws_name)
    # Rough estimate: gzip typically compresses to ~20% of original
    # for text-heavy home dirs (source code, dotfiles, configs).
    estimated_compressed = max(int(estimated_size * 0.2), 1)
    return StreamingResponse(
        _stream(),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}.tar.gz"',
            "X-Estimated-Size": str(estimated_compressed),
        },
    )


@router.post("/workspaces/import")
async def import_workspace(
    file: UploadFile,
    name: str | None = None,
    user: dict = Depends(auth.get_current_user),
):
    """Import a workspace from a .tar.gz archive.

    Creates a new workspace with metadata from workspace.json and
    extracts the home directory from the archive.
    """
    # Stream upload to a temp file — abort if over the configured limit.
    max_upload = FILE_UPLOAD_SIZE_MAX
    tmp = tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False)
    total = 0
    try:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > max_upload:
                raise HTTPException(
                    status_code=413,
                    detail=f"Archive exceeds {max_upload // (1024 * 1024)} MB limit",
                )
            tmp.write(chunk)
        tmp.close()
    except HTTPException:
        os.unlink(tmp.name)
        raise
    except Exception:
        os.unlink(tmp.name)
        raise

    # Extract workspace.json from the archive using tar (fast, no Python parsing).
    ws = None
    try:
        result = subprocess.run(
            ["tar", "xzf", tmp.name, "-O", "workspace.json"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail="Archive missing workspace.json or is corrupt",
            )
        metadata = json.loads(result.stdout)

        ws_name = name or metadata.get("name")
        if not ws_name:
            raise HTTPException(
                status_code=400,
                detail="No workspace name in archive or request",
            )

        image = metadata.get("image")
        if image and image not in container.ALLOWED_IMAGES:
            image = None  # fall back to default

        mounts = metadata.get("mounts")
        if mounts and container.validate_mounts(mounts):
            mounts = None  # invalid mounts, drop them

        # Sanitize env: drop keys that could interfere with
        # container startup (KLANGK_*, LD_*, PATH, etc.)
        raw_env = metadata.get("env")
        if isinstance(raw_env, dict):
            blocked = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PATH"}
            env = {
                k: v
                for k, v in raw_env.items()
                if not k.startswith("KLANGK_") and k not in blocked
            }
        else:
            env = None

        try:
            ws = await workspaces.create_workspace(
                user["id"],
                ws_name,
                image=image,
                default_command=metadata.get("default_command"),
                auto_start=metadata.get("auto_start", False),
                mounts=mounts,
                env=env,
            )
        except SAIntegrityError:
            raise HTTPException(
                status_code=409,
                detail=f"A workspace named {ws_name!r} already exists",
            )

        # Grant owner full access via ACL (mirrors create_workspace). Without
        # this an imported workspace has no ACL entries, so even its owner is
        # denied terminal/exec/files access.
        await model.add_acl_entry(
            f"/workspaces/{ws['id']}",
            0,
            ACTION_ALLOW,
            "*",
            PRINCIPAL_USER,
            user_id=user["id"],
        )

        # Extract home directory using tar (much faster than Python tarfile
        # for archives with many members). --strip-components=1 removes
        # the "home/" prefix. Only extract if home/ exists in the archive.
        home_dir = workspaces.home_path(user["id"], ws["id"])
        home_dir.mkdir(parents=True, exist_ok=True)
        check = subprocess.run(
            ["tar", "tzf", tmp.name, "home/"],
            capture_output=True,
            timeout=30,
        )
        if check.returncode == 0:
            result = await asyncio.to_thread(
                subprocess.run,
                [
                    "tar",
                    "xzf",
                    tmp.name,
                    "--strip-components=1",
                    "--no-same-owner",
                    "--no-same-permissions",
                    "-C",
                    str(home_dir),
                    "home/",
                ],
                capture_output=True,
                timeout=300,
            )
            if result.returncode != 0:
                await workspaces.delete_workspace(ws["id"], user["id"])
                raise HTTPException(
                    status_code=400,
                    detail="Failed to extract home directory from archive",
                )

    except HTTPException:
        raise
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        if ws:
            await workspaces.delete_workspace(ws["id"], user["id"])
        raise HTTPException(
            status_code=400, detail="Invalid or corrupt archive"
        )
    finally:
        os.unlink(tmp.name)

    wshandler.state.notify_user_workspaces_changed(user["id"])
    return ws


# --- Workspace sharing endpoints ---


async def _check_workspace_share(request: Request, user: dict) -> str:
    """Resource function for workspace share permission."""
    workspace_id = request.path_params["workspace_id"]
    return f"/workspaces/{workspace_id}"


async def _broadcast_workspace_members(workspace_id: str) -> None:
    """Push updated workspace members to all connected subscribers."""
    session = wshandler.state.get_session(workspace_id)
    if not session:
        return
    members = await model.get_workspace_members(workspace_id)
    workspace = await model.get_workspace(workspace_id)
    if workspace:
        owner = await model.get_user_by_id(workspace.get("user_id", ""))
        if owner and not any(m["id"] == owner["id"] for m in members):
            members.append({"id": owner["id"], "email": owner["email"]})
    session.broadcast({"type": "workspace_members", "members": members})


@router.get("/workspaces/{workspace_id}/members")
async def get_workspace_members(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    return await model.get_workspace_members(workspace_id)


class AddMemberRequest(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/members")
async def add_workspace_member(
    workspace_id: str,
    body: AddMemberRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    target = await model.get_user_by_email(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == user["id"]:
        raise HTTPException(
            status_code=400, detail="Cannot share with yourself"
        )
    # Add ACL entries granting the target user view+terminal+files+chat
    # on this workspace, packed at the next available positions.
    resource = f"/workspaces/{workspace_id}"
    existing = await model.get_acl_entries(resource)
    next_pos = max((e["position"] for e in existing), default=-1) + 1
    for perm in ("view", "terminal", "files", "chat"):
        await model.add_acl_entry(
            resource,
            next_pos,
            ACTION_ALLOW,
            perm,
            PRINCIPAL_USER,
            user_id=target["id"],
        )
        next_pos += 1
    await _broadcast_workspace_members(workspace_id)
    wshandler.state.notify_user_workspaces_changed(user["id"])
    wshandler.state.notify_user_workspaces_changed(target["id"])
    return {
        "status": "shared",
        "user_id": target["id"],
        "email": target["email"],
    }


@router.delete("/workspaces/{workspace_id}/members/{member_id}")
async def remove_workspace_member(
    workspace_id: str,
    member_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    # Remove all ACL entries for this user on this workspace
    resource = f"/workspaces/{workspace_id}"
    entries = await model.get_acl_entries(resource)
    remaining = [
        e
        for e in entries
        if not (
            e["principal_type"] == PRINCIPAL_USER and e["user_id"] == member_id
        )
    ]
    # Rewrite entries with new positions
    for i, entry in enumerate(remaining):
        entry["position"] = i
    await model.replace_acl_entries(resource, remaining)
    await _broadcast_workspace_members(workspace_id)
    wshandler.state.notify_user_workspaces_changed(user["id"])
    wshandler.state.notify_user_workspaces_changed(member_id)
    return {"status": "removed"}


ROLE_GROUP_SUFFIXES = ["owners", "coders", "collaborators", "spectators"]


@router.get("/workspaces/{workspace_id}/roles")
async def get_workspace_roles(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Return the workspace's role groups with their members."""
    roles = []
    for suffix in ROLE_GROUP_SUFFIXES:
        group_name = f"{suffix}-{workspace_id}"
        group = await model.get_group_by_name(group_name)
        if group is None:
            continue
        members = await model.get_group_members(group["id"])
        roles.append(
            {
                "role": suffix,
                "group_id": group["id"],
                "group_name": group_name,
                "members": [
                    {"id": m["id"], "email": m["email"]} for m in members
                ],
            }
        )
    return roles


class AddToRoleRequest(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/roles/{role}")
async def add_to_workspace_role(
    workspace_id: str,
    role: str,
    body: AddToRoleRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Add a user to a workspace role group."""
    if role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    group_name = f"{role}-{workspace_id}"
    group = await model.get_group_by_name(group_name)
    if group is None:
        raise HTTPException(status_code=404, detail="Role group not found")
    target = await model.get_user_by_email(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    await model.add_user_to_group(target["id"], group["id"])
    return {"ok": True}


@router.delete("/workspaces/{workspace_id}/roles/{role}/{member_id}")
async def remove_from_workspace_role(
    workspace_id: str,
    role: str,
    member_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Remove a user from a workspace role group."""
    if role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    group_name = f"{role}-{workspace_id}"
    group = await model.get_group_by_name(group_name)
    if group is None:
        raise HTTPException(status_code=404, detail="Role group not found")
    await model.remove_user_from_group(member_id, group["id"])
    return {"ok": True}


class ChangeRoleRequest(BaseModel):
    email: str
    role: str | None = None  # None = remove from all roles


@router.patch("/workspaces/{workspace_id}/roles")
async def change_workspace_role(
    workspace_id: str,
    body: ChangeRoleRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Atomically change a user's workspace role.

    If ``role`` is set, removes the user from all other roles and adds
    them to the target role.  If ``role`` is null, removes the user
    from all roles.
    """
    target = await model.get_user_by_email(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    if body.role is not None and body.role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role: {body.role}"
        )

    # Remove from all current roles
    for suffix in ROLE_GROUP_SUFFIXES:
        group_name = f"{suffix}-{workspace_id}"
        group = await model.get_group_by_name(group_name)
        if group is None:
            continue
        await model.remove_user_from_group(target["id"], group["id"])

    # Add to target role if specified
    if body.role is not None:
        group_name = f"{body.role}-{workspace_id}"
        group = await model.get_group_by_name(group_name)
        if group is None:
            raise HTTPException(status_code=404, detail="Role group not found")
        await model.add_user_to_group(target["id"], group["id"])

    return {"ok": True, "email": body.email, "role": body.role}


@router.get("/workspaces/{workspace_id}/groups")
async def get_workspace_groups(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Get groups with access to this workspace via ACL."""
    resource = f"/workspaces/{workspace_id}"
    entries = await model.get_acl_entries_resolved(resource)
    seen = set()
    groups = []
    for e in entries:
        if e["principal_type"] == PRINCIPAL_GROUP and e.get("group_id"):
            gid = e["group_id"]
            if gid not in seen:
                seen.add(gid)
                groups.append({"id": gid, "name": e["principal"]})
    return groups


class AddGroupShareRequest(BaseModel):
    group_id: str


@router.post("/workspaces/{workspace_id}/groups")
async def add_workspace_group(
    workspace_id: str,
    body: AddGroupShareRequest,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Share a workspace with a group (view/terminal/files/chat)."""
    group = await model.get_group_by_id(body.group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    resource = f"/workspaces/{workspace_id}"
    existing = await model.get_acl_entries(resource)
    max_pos = max((e["position"] for e in existing), default=-1)
    for i, perm in enumerate(["view", "terminal", "files", "chat"]):
        await model.add_acl_entry(
            resource,
            max_pos + 1 + i,
            ACTION_ALLOW,
            perm,
            PRINCIPAL_GROUP,
            group_id=body.group_id,
        )
    return {"status": "shared", "group_id": group["id"], "name": group["name"]}


@router.delete("/workspaces/{workspace_id}/groups/{group_id}")
async def remove_workspace_group(
    workspace_id: str,
    group_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Remove all ACL entries for a group on this workspace."""
    resource = f"/workspaces/{workspace_id}"
    entries = await model.get_acl_entries(resource)
    remaining = [
        e
        for e in entries
        if not (
            e["principal_type"] == PRINCIPAL_GROUP
            and e["group_id"] == group_id
        )
    ]
    for i, entry in enumerate(remaining):
        entry["position"] = i
    await model.replace_acl_entries(resource, remaining)
    return {"status": "removed"}


# --- Workspace ACL endpoints (for workspace owners/admins) ---


@router.get("/workspaces/{workspace_id}/acl")
async def get_workspace_acl(
    workspace_id: str,
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Get resolved ACL entries for a workspace."""
    resource = f"/workspaces/{workspace_id}"
    return await model.get_acl_entries_resolved(resource)


@router.put("/workspaces/{workspace_id}/acl")
async def replace_workspace_acl(
    workspace_id: str,
    entries: list[WorkspaceAclEntry],
    user: dict = Depends(acl.has_permission("share", _check_workspace_share)),
):
    """Replace all ACL entries for a workspace."""
    resource = f"/workspaces/{workspace_id}"
    acl_entries = [
        {
            "position": i,
            "action": e.action,
            "principal_type": e.principal_type,
            "permission": e.permission,
            "user_id": e.user_id,
            "group_id": e.group_id,
            "system_principal": e.system_principal,
        }
        for i, e in enumerate(entries)
    ]
    await model.replace_acl_entries(resource, acl_entries)
    return await model.get_acl_entries_resolved(resource)


# --- User search endpoint ---


@router.get("/users/search")
async def search_users(
    q: str,
    _user: dict = Depends(auth.get_current_user),
):
    if len(q) < 1:
        raise HTTPException(status_code=400, detail="Query too short")
    return await model.search_users(q)


# --- File endpoints ---
