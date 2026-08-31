"""Workspace resource routes: files (list/read/delete/rename/download/
upload) and container resources (images + named volumes) — merged from the
former files and images submodules (images.py was misnamed: it lists
volumes too)."""

import io
import logging
import posixpath

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    UploadFile,
)
from fastapi.responses import (
    StreamingResponse,
)
from pydantic import BaseModel

from .. import (
    acl,
    auth,
)
from ..podman import PodmanError as PodmanError
from ..settings import parse_bool_setting
from ..util import (
    sanitize_disposition_name,
)
from .common import get_app_dep
from .common import (
    workspace_resource,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Files (definitions first — the merged router preserves the original
# files-then-images include order).
# ---------------------------------------------------------------------------


def _require_container(workspace_id: str, container_registry) -> str:
    """Return the container_id for a running workspace, or raise 409."""
    state = container_registry.get_state(workspace_id)
    if state is None:
        raise HTTPException(status_code=409, detail="Container not running")
    state.record_activity()
    return state.container_id


def _files_http_error(
    e: Exception, *, not_found: str = "Path not found"
) -> HTTPException:
    """Translate a files-layer exception to its HTTP response (#2553).

    The shared except-ladder of the file routes: ValueError -> 400 (the
    caller's message), FileNotFoundError -> 404 (*not_found* wording
    varies per route), FileExistsError -> 409, OSError -> 500. Routes
    with an extra case (or none of these) keep their own handling.
    """
    if isinstance(e, ValueError):
        return HTTPException(status_code=400, detail=str(e))
    if isinstance(e, FileNotFoundError):
        return HTTPException(status_code=404, detail=not_found)
    if isinstance(e, FileExistsError):
        return HTTPException(
            status_code=409, detail="Destination already exists"
        )
    if isinstance(e, PermissionError):
        return HTTPException(status_code=403, detail=str(e))
    return HTTPException(status_code=500, detail=str(e))


@router.get("/workspaces/{workspace_id}/files")
async def list_files(
    workspace_id: str,
    path: str = "/",
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    app=Depends(get_app_dep),
):
    cid = _require_container(workspace_id, app.state.container_registry)
    try:
        return await app.state.files.list_files(cid, path)
    except (ValueError, OSError) as e:
        raise _files_http_error(e) from None


@router.get("/workspaces/{workspace_id}/files/content")
async def read_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    _download: dict = Depends(
        acl.has_permission("files-download", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    cid = _require_container(workspace_id, app.state.container_registry)
    try:
        content = await app.state.files.read_file(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(
            status_code=404, detail="File not found or too large"
        )
    return {"path": path, "content": content}


@router.delete("/workspaces/{workspace_id}/files")
async def delete_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    _write: dict = Depends(
        acl.has_permission("files-write", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    cid = _require_container(workspace_id, app.state.container_registry)
    try:
        deleted = await app.state.files.delete_path(cid, path)
    except (ValueError, FileNotFoundError, OSError) as e:
        raise _files_http_error(e) from None
    return {"path": deleted, "status": "deleted"}


class RenameFileRequest(BaseModel):
    old_path: str
    new_path: str


@router.post("/workspaces/{workspace_id}/files/rename")
async def rename_file(
    workspace_id: str,
    body: RenameFileRequest,
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    _write: dict = Depends(
        acl.has_permission("files-write", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    cid = _require_container(workspace_id, app.state.container_registry)
    try:
        renamed = await app.state.files.rename_path(
            cid, body.old_path, body.new_path
        )
    except (ValueError, FileNotFoundError, FileExistsError, OSError) as e:
        raise _files_http_error(e, not_found="Source not found") from None
    return {"path": renamed, "status": "renamed"}


@router.get("/workspaces/{workspace_id}/files/download")
async def download_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    _download: dict = Depends(
        acl.has_permission("files-download", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    cid = _require_container(workspace_id, app.state.container_registry)
    try:
        info = await app.state.files.stat_path(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if info is None:
        raise HTTPException(status_code=404, detail="Path not found")
    name = sanitize_disposition_name(posixpath.basename(path) or "download")
    if not info["is_dir"]:
        return StreamingResponse(
            app.state.files.stream_file(cid, path),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
            },
        )
    return StreamingResponse(
        app.state.files.stream_dir_tar(cid, path),
        media_type="application/gzip",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.tar.gz"',
        },
    )


@router.post("/workspaces/{workspace_id}/files/upload")
async def upload_file(
    workspace_id: str,
    file: UploadFile,
    path: str = "",
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    _write: dict = Depends(
        acl.has_permission("files-write", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    cid = _require_container(workspace_id, app.state.container_registry)

    filename = path if path else posixpath.basename(file.filename or "")
    if not filename:  # pragma: no cover
        raise HTTPException(status_code=400, detail="No filename provided")

    max_upload = app.state.settings.file_upload_size_max
    buf = io.BytesIO()
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > max_upload:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds {max_upload // (1024 * 1024)} MB limit",
            )
        buf.write(chunk)

    try:
        saved_path = await app.state.files.write_file(
            cid, filename, buf.getvalue()
        )
    except (ValueError, OSError) as e:
        raise _files_http_error(e) from None
    return {"path": saved_path, "status": "uploaded"}


# --- Browser bridge endpoint ---


# ---------------------------------------------------------------------------
# Images + named volumes (formerly images.py)
# ---------------------------------------------------------------------------


@router.get("/images")
async def list_images(
    _user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    return {
        "default": app.state.container_registry.image_name,
        "allowed": sorted(app.state.container_registry.allowed_images),
        # #2202/#2560: whether the per-workspace nix flag can trigger the
        # per-workspace /nix mount. The create UI shows the "nix" toggle
        # only when the feature is armed — a backend configured (btrfs
        # snapshot or fuse-overlayfs, #2219) AND nix_enabled on (#2560,
        # off by default); the flag is inert otherwise (workspaces use the
        # nix image's baked /nix).
        "nix_available": app.state.nix.available,
        # #2017: whether the deploy allows sudo at all (the per-workspace
        # knob may only lock a workspace down below this). The create/edit
        # UIs show the sudo toggle only when this is true — on a
        # sudo-forbidding deploy the toggle is a no-op (sudo is off for
        # every workspace regardless).
        "sudo_available": parse_bool_setting(app.state.settings.allow_sudo),
    }


# --- Volume management ---


@router.get("/volumes")
async def list_volumes(
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    volumes = await app.state.podman.list_volumes(
        f"klangk.instance={app.state.util.instance_id()}"
    )
    uid = user["id"]
    return [
        {
            "name": v["Name"],
            "created": v.get("CreatedAt", ""),
        }
        for v in volumes
        if (v.get("Labels") or {}).get("klangk.user-id") == uid
    ]


class CreateVolumeRequest(BaseModel):
    name: str


@router.post("/volumes")
async def create_volume(
    body: CreateVolumeRequest,
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    if await app.state.podman.inspect_volume(body.name) is not None:
        raise HTTPException(
            status_code=409, detail=f"Volume {body.name!r} already exists"
        )
    info = await app.state.podman.create_volume(
        body.name,
        {
            "klangk.managed": "true",
            "klangk.instance": app.state.util.instance_id(),
            "klangk.user-id": user["id"],
        },
    )
    return {"name": info["Name"], "created": info.get("CreatedAt", "")}


@router.delete("/volumes/{name}")
async def delete_volume(
    name: str,
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    info = await app.state.podman.inspect_volume(name)
    if info is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    labels = info.get("Labels") or {}
    if labels.get("klangk.instance") != app.state.util.instance_id():
        raise HTTPException(
            status_code=404,
            detail="Volume not managed by this Klangk instance",
        )
    if labels.get("klangk.user-id") != user["id"]:
        raise HTTPException(
            status_code=403,
            detail="Volume belongs to another user",
        )
    try:
        await app.state.podman.remove_volume(name)
    except PodmanError as e:
        if e.status == 404:
            raise HTTPException(
                status_code=404, detail="Volume not found"
            ) from None
        if e.status == 409:
            raise HTTPException(
                status_code=409, detail="Volume is in use"
            ) from None
        raise
    return {"status": "deleted"}
