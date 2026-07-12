"""Workspace file routes: list/read/delete/rename/download/upload."""

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
    files,
)
from ._common import get_app_state_dep
from ..util import (
    sanitize_disposition_name,
)
from ._common import (
    FILE_UPLOAD_SIZE_MAX,
    workspace_resource,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_container(workspace_id: str, container_registry) -> str:
    """Return the container_id for a running workspace, or raise 409."""
    state = container_registry.get_state(workspace_id)
    if state is None:
        raise HTTPException(status_code=409, detail="Container not running")
    state.record_activity()
    return state.container_id


@router.get("/workspaces/{workspace_id}/files")
async def list_files(
    workspace_id: str,
    path: str = "/",
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    app_state=Depends(get_app_state_dep),
):
    cid = _require_container(workspace_id, app_state.container_registry)
    try:
        return await files.list_files(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workspaces/{workspace_id}/files/content")
async def read_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    app_state=Depends(get_app_state_dep),
):
    cid = _require_container(workspace_id, app_state.container_registry)
    try:
        content = await files.read_file(cid, path)
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
    app_state=Depends(get_app_state_dep),
):
    cid = _require_container(workspace_id, app_state.container_registry)
    try:
        deleted = await files.delete_path(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Path not found")
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": deleted, "status": "deleted"}


class RenameFileRequest(BaseModel):
    old_path: str
    new_path: str


@router.post("/workspaces/{workspace_id}/files/rename")
async def rename_file(
    workspace_id: str,
    body: RenameFileRequest,
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    app_state=Depends(get_app_state_dep),
):
    cid = _require_container(workspace_id, app_state.container_registry)
    try:
        renamed = await files.rename_path(cid, body.old_path, body.new_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source not found")
    except FileExistsError:
        raise HTTPException(
            status_code=409, detail="Destination already exists"
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": renamed, "status": "renamed"}


@router.get("/workspaces/{workspace_id}/files/download")
async def download_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(acl.has_permission("files", workspace_resource)),
    app_state=Depends(get_app_state_dep),
):
    cid = _require_container(workspace_id, app_state.container_registry)
    try:
        info = await files.stat_path(cid, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if info is None:
        raise HTTPException(status_code=404, detail="Path not found")
    name = sanitize_disposition_name(posixpath.basename(path) or "download")
    if not info["is_dir"]:
        return StreamingResponse(
            files.stream_file(cid, path),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
            },
        )
    return StreamingResponse(
        files.stream_dir_tar(cid, path),
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
    app_state=Depends(get_app_state_dep),
):
    cid = _require_container(workspace_id, app_state.container_registry)

    filename = path if path else posixpath.basename(file.filename or "")
    if not filename:  # pragma: no cover
        raise HTTPException(status_code=400, detail="No filename provided")

    max_upload = FILE_UPLOAD_SIZE_MAX
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
        saved_path = await files.write_file(cid, filename, buf.getvalue())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except OSError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"path": saved_path, "status": "uploaded"}


# --- Browser bridge endpoint ---
