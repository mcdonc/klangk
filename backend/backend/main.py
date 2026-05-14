"""Bark backend: FastAPI app with HTTP + WebSocket endpoints."""

import logging
import os
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import auth, container_manager, file_service, user_store, workspace_manager
from .ws_handler import handle_websocket

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _seed_default_user() -> None:
    """Create default user if it doesn't exist."""
    import bcrypt
    username = os.environ.get("BARK_DEFAULT_USER", "admin")
    password = os.environ.get("BARK_DEFAULT_PASSWORD", "admin")
    existing = await user_store.get_user_by_username(username)
    if existing is None:
        password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        await user_store.create_user(username, password_hash)
        logger.info("Created default user '%s'", username)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await user_store.init_db()
    await _seed_default_user()
    container_manager.start_cleanup_loop()
    logger.info("Bark backend started")
    yield
    await container_manager.shutdown()
    logger.info("Bark backend stopped")


app = FastAPI(title="Bark", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Auth endpoints ---

@app.post("/auth/register", response_model=auth.TokenResponse)
async def register(req: auth.RegisterRequest):
    return await auth.register(req)


@app.post("/auth/login", response_model=auth.TokenResponse)
async def login(req: auth.LoginRequest):
    return await auth.login(req)


@app.post("/auth/logout")
async def logout(user: dict = Depends(auth.get_current_user)):
    # Stop all user containers
    await container_manager.stop_user_containers(user["id"])
    # Note: token invalidation happens via blocklist in auth.logout()
    # but we need the raw token here — handled in middleware or manually
    return {"status": "ok"}


# --- Workspace endpoints ---

@app.get("/workspaces")
async def list_workspaces(user: dict = Depends(auth.get_current_user)):
    return await workspace_manager.list_workspaces(user["id"])


@app.post("/workspaces")
async def create_workspace(name: str, user: dict = Depends(auth.get_current_user)):
    try:
        return await workspace_manager.create_workspace(user["id"], name)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str, user: dict = Depends(auth.get_current_user)):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Stop and remove container if running
    if workspace.get("container_id"):
        await container_manager.remove_container(workspace["container_id"])

    deleted = await workspace_manager.delete_workspace(workspace_id, user["id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"status": "deleted"}


# --- Message history endpoints ---

@app.get("/workspaces/{workspace_id}/messages")
async def get_messages(workspace_id: str, user: dict = Depends(auth.get_current_user)):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return await user_store.get_messages(workspace_id)


# --- File endpoints ---

@app.get("/workspaces/{workspace_id}/files")
async def list_files(
    workspace_id: str,
    path: str = ".",
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        return file_service.list_files(user["id"], workspace["name"], path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/workspaces/{workspace_id}/files/content")
async def read_file(
    workspace_id: str,
    path: str,
    user: dict = Depends(auth.get_current_user),
):
    workspace = await workspace_manager.get_workspace(workspace_id, user["id"])
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    try:
        content = file_service.read_file(user["id"], workspace["name"], path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if content is None:
        raise HTTPException(status_code=404, detail="File not found or too large")
    return {"path": path, "content": content}


@app.post("/workspaces/{workspace_id}/files/upload")
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
    if not filename:
        raise HTTPException(status_code=400, detail="No filename provided")

    content = await file.read()
    try:
        saved_path = file_service.write_file(user["id"], workspace["name"], filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"path": saved_path, "status": "uploaded"}


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await handle_websocket(ws)


# --- Static files (Flutter Web) ---
# Must be last so API routes take priority

_frontend_dir = Path(__file__).parent.parent.parent / "frontend" / "build" / "web"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
