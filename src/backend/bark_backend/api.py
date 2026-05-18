"""API route handlers for Bark backend."""

import io
import logging
import os
import sqlite3
import zipfile

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from . import auth, container_manager, file_service, user_store, workspace_manager

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Test/debug endpoints (only when BARK_TEST_MODE is set) ---

if os.environ.get("BARK_TEST_MODE"):  # pragma: no cover

    @router.get("/api/test/idle-timeout")
    async def get_idle_timeout(workspace_id: str | None = None):
        """Get the idle timeout (per-workspace or global default)."""
        if workspace_id:
            return {
                "idle_timeout_seconds": container_manager.get_workspace_idle_timeout(
                    workspace_id
                )
            }
        return {"idle_timeout_seconds": container_manager.IDLE_TIMEOUT_SECONDS}

    @router.post("/api/test/set-idle-timeout")
    async def set_idle_timeout(seconds: int, workspace_id: str | None = None):
        """Set the idle timeout. Per-workspace if workspace_id given, else global."""
        if workspace_id:
            container_manager.set_workspace_idle_timeout(workspace_id, seconds)
        else:
            container_manager.IDLE_TIMEOUT_SECONDS = seconds
            container_manager.CHECK_INTERVAL_SECONDS = max(10, min(60, seconds // 3))
        return {"idle_timeout_seconds": seconds}


# --- Config endpoint ---

SOLIPLEX_URL = os.environ.get("SOLIPLEX_URL", "")


@router.get("/api/config")
async def get_config():
    return {"soliplex_url": SOLIPLEX_URL}


# --- Auth endpoints ---


@router.post("/auth/register", response_model=auth.TokenResponse)
async def register(req: auth.RegisterRequest):
    return await auth.register(req)


@router.post("/auth/login", response_model=auth.TokenResponse)
async def login(req: auth.LoginRequest):
    return await auth.login(req)


@router.post("/auth/logout")
async def logout(user: dict = Depends(auth.get_current_user)):
    await container_manager.stop_user_containers(user["id"])
    return {"status": "ok"}


# --- Workspace endpoints ---


@router.get("/workspaces")
async def list_workspaces(user: dict = Depends(auth.get_current_user)):
    return await workspace_manager.list_workspaces(user["id"])


@router.post("/workspaces")
async def create_workspace(name: str, user: dict = Depends(auth.get_current_user)):
    try:
        return await workspace_manager.create_workspace(user["id"], name)
    except (sqlite3.IntegrityError, OSError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str, user: dict = Depends(auth.get_current_user)
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    if workspace.get("container_id"):
        await container_manager.stop_and_remove_container(workspace["container_id"])

    deleted = await workspace_manager.delete_workspace(workspace_id, user["id"])
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"status": "deleted"}


# --- Message history endpoints ---


@router.get("/workspaces/{workspace_id}/messages")
async def get_messages(workspace_id: str, user: dict = Depends(auth.get_current_user)):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return await user_store.get_messages(workspace_id)


# --- File endpoints ---


@router.get("/workspaces/{workspace_id}/files")
async def list_files(
    workspace_id: str,
    path: str = ".",
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        return file_service.list_files(user["id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/workspaces/{workspace_id}/files/content")
async def read_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        content = file_service.read_file(user["id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(status_code=404, detail="File not found or too large")
    return {"path": path, "content": content}


@router.delete("/workspaces/{workspace_id}/files")
async def delete_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        deleted = file_service.delete_path(user["id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Path not found")
    return {"path": deleted, "status": "deleted"}


@router.post("/workspaces/{workspace_id}/files/rename")
async def rename_file(
    workspace_id: str,
    old_path: str,
    new_path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        renamed = file_service.rename_path(user["id"], workspace_id, old_path, new_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Source not found")
    except FileExistsError:
        raise HTTPException(status_code=409, detail="Destination already exists")
    return {"path": renamed, "status": "renamed"}


@router.get("/workspaces/{workspace_id}/files/download")
async def download_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        resolved = file_service.resolve_path(user["id"], workspace_id, path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not resolved.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if resolved.is_file():
        return FileResponse(resolved, filename=resolved.name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in resolved.rglob("*"):
            if file_path.is_file():
                zf.write(file_path, file_path.relative_to(resolved))
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{resolved.name}.zip"'},
    )


@router.post("/workspaces/{workspace_id}/files/upload")
async def upload_file(
    workspace_id: str,
    file: UploadFile,
    path: str = "",
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    filename = path if path else file.filename
    if not filename:  # pragma: no cover — FastAPI rejects empty filename at 422 first
        raise HTTPException(status_code=400, detail="No filename provided")

    content = await file.read()
    try:
        saved_path = file_service.write_file(
            user["id"], workspace_id, filename, content
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": saved_path, "status": "uploaded"}
