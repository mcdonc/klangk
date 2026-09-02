"""Workspace resource routes: files (list/read/delete/rename/download/
upload) and container resources (images + named volumes) — merged from the
former files and images submodules (images.py was misnamed: it lists
volumes too)."""

import io
import json
import logging
import posixpath

from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    UploadFile,
)
from fastapi.responses import (
    StreamingResponse,
)
from pydantic import BaseModel, Field

from .. import (
    acl,
)
from ..container.spec import VOLUME_NAME_PATTERN
from ..podman import PodmanError as PodmanError
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
    user: dict = Depends(acl.has_permission("files-view", workspace_resource)),
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
    user: dict = Depends(acl.has_permission("files-view", workspace_resource)),
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
    user: dict = Depends(acl.has_permission("files-view", workspace_resource)),
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
    user: dict = Depends(acl.has_permission("files-view", workspace_resource)),
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
    user: dict = Depends(acl.has_permission("files-view", workspace_resource)),
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
    user: dict = Depends(acl.has_permission("files-view", workspace_resource)),
    _write: dict = Depends(
        acl.has_permission("files-write", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    cid = _require_container(workspace_id, app.state.container_registry)

    filename = path if path else posixpath.basename(file.filename or "")
    if not filename:
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
    _user: dict = Depends(acl.has_permission("view-images")),
    app=Depends(get_app_dep),
):
    """The image listing for the workspace create/edit UIs (#2974).

    Deployment-level toggles (nix/sudo availability) moved to the
    authenticated-only fields on ``/api/v1/config`` — they are
    deployment config, not image data.
    """
    return {
        "default": app.state.container_registry.image_name,
        "allowed": sorted(app.state.container_registry.allowed_images),
    }


# --- Volume management ---


def _named_volume_sources(mounts) -> list[str]:
    """The named-volume sources in a workspace's mounts list.

    A mount spec is ``<source>:<dest>``; a source with no ``/`` that
    doesn't start with ``.`` is a named volume — the same rule as
    ``container.spec.is_named_volume``, inlined so the api layer
    doesn't reach into the container package.
    """
    sources = []
    for spec in mounts or []:
        source = spec.split(":")[0]
        if "/" not in source and not source.startswith("."):
            sources.append(source)
    return sources


def _volume_usage_map(rows) -> dict[str, list[str]]:
    """Volume name → the workspace names whose mounts use it (#2993)."""
    usage: dict[str, list[str]] = {}
    for row in rows:
        mounts = json.loads(row["mounts"]) if row["mounts"] else None
        for source in _named_volume_sources(mounts):
            usage.setdefault(source, []).append(row["name"])
    for names in usage.values():
        names.sort()
    return usage


async def _creator_handles(app, volumes) -> dict[str, str | None]:
    """Creator ``klangk.user-id`` label → the user's handle (#2993).

    A label whose user no longer exists (deleted creator) maps to
    ``None`` — the id stays in ``user_id`` as provenance.
    """
    handles: dict[str, str | None] = {}
    for v in volumes:
        uid = (v.get("Labels") or {}).get("klangk.user-id")
        if not uid or uid in handles:
            continue
        creator = await app.state.model.users.get_user_by_id(uid)
        handles[uid] = (creator or {}).get("handle") or None
    return handles


def _volume_matches(item: dict, needle: str) -> bool:
    """Whether a listing row matches the search needle — volume name,
    creator handle, or a using workspace's name (case-insensitive)."""
    return (
        needle in item["name"].lower()
        or needle in (item["created_by"] or "").lower()
        or any(needle in w.lower() for w in item["workspaces"])
    )


def _sort_volume_items(items: list[dict], sort: str, order: str) -> None:
    """Sort listing rows in place by the whitelisted key (``name`` or
    ``created``; unknown → created) with the name tiebreaker always
    ascending — the list_users/list_workspaces posture (``ORDER BY col
    DESC, id``). Two stable sorts: name first, then the primary key.
    ``created`` is podman's RFC3339Nano string; lexicographic order is
    exact within one UTC offset (the stored-format reality of this
    field) and approximate across a DST boundary.
    """
    primary = "name" if sort == "name" else "created"
    items.sort(key=lambda it: it["name"])
    items.sort(
        key=lambda it: it[primary] or "",
        reverse=order.lower() == "desc",
    )


@router.get("/volumes")
async def list_volumes(
    page: int = 1,
    page_size: int = 10,
    sort: str = "created",
    order: str = "desc",
    q: str | None = None,
    _user: dict = Depends(acl.has_permission("view-volumes")),
    app=Depends(get_app_dep),
):
    """The whole instance-managed volume inventory (#2993).

    The admin tab's listing gate is ``view-volumes`` (the tab's
    visibility keys on it); ``manage-volumes`` covers create/delete.
    The ``klangk.user-id`` label is surfaced as provenance, not used
    as an access filter — an admin operating the tab sees every
    volume this instance manages, who created it (``created_by``, the
    creator's handle), and which workspaces mount it (``workspaces``).

    Server-side paginated/sorted/filtered like the other admin tabs:
    ``q`` matches volume name, creator handle, or a using workspace
    name (case-insensitive substring); ``sort`` is ``name`` | ``created``;
    returns the paged envelope ``{volumes, page, page_size, total}``.
    """
    volumes = await app.state.podman.list_volumes(
        f"klangk.instance={app.state.util.instance_id()}"
    )
    usage = _volume_usage_map(
        await app.state.model.workspaces.workspace_mount_rows()
    )
    handles = await _creator_handles(app, volumes)
    items = [
        {
            "name": v["Name"],
            "created": v.get("CreatedAt", ""),
            "user_id": (v.get("Labels") or {}).get("klangk.user-id"),
            "created_by": handles.get(
                (v.get("Labels") or {}).get("klangk.user-id")
            ),
            "workspaces": usage.get(v["Name"], []),
        }
        for v in volumes
    ]
    if q:
        needle = q.lower()
        items = [it for it in items if _volume_matches(it, needle)]
    _sort_volume_items(items, sort, order)
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    start = (page - 1) * page_size
    return {
        "volumes": items[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": len(items),
    }


# The podman-safe volume-name rule lives next to its sibling
# ``is_named_volume`` in ``container.spec`` (moved there in #3018 so the
# workspace mount validator shares the single home): starts with an
# alphanumeric (so a leading "-" can never be parsed as a flag by the
# podman CLI, whose argv we build by appending the name verbatim),
# continues with alphanumerics/underscore/dot/hyphen only, and stays
# within 64 chars. Pydantic violations surface as 422 here; the same
# pattern gates workspace mount sources via
# ``container.spec.valid_volume_name`` (#3018).


class CreateVolumeRequest(BaseModel):
    name: str = Field(pattern=VOLUME_NAME_PATTERN)


@router.post("/volumes")
async def create_volume(
    body: CreateVolumeRequest,
    user: dict = Depends(acl.has_permission("manage-volumes")),
    app=Depends(get_app_dep),
):
    existing = await app.state.podman.inspect_volume(body.name)
    if existing is not None:
        labels = existing.get("Labels") or {}
        if labels.get("klangk.instance") == app.state.util.instance_id():
            raise HTTPException(
                status_code=409, detail=f"Volume {body.name!r} already exists"
            )
    # Per-user volume quota (#2972): a create past the cap is refused
    # with 429 (not 403 — the refusal is capacity, not authorization;
    # not 503 either — the workspace-admission 503s are auto-retried by
    # the CLI's request_with_retry, and a quota refusal must not be).
    # Checked after the conflict probe so an in-instance duplicate
    # still reports 409 (the route's caller can list the instance
    # inventory either way). Only runs when a quota is configured —
    # the default 0 keeps the create path exactly as before, with no
    # extra podman call. The per-user lock spans count+create: without
    # it, N concurrent creates each count the same pre-create total
    # and all pass a cap they jointly exceed. The workspace-start
    # auto-create door (container/spec.py ensure_volumes) shares the
    # same lock and quota gate.
    labels = {
        "klangk.managed": "true",
        "klangk.instance": app.state.util.instance_id(),
        "klangk.user-id": user["id"],
    }
    quota = app.state.settings.volume_quota_per_user
    if quota > 0:
        async with app.state.podman.volume_create_lock(user["id"]):
            count = await app.state.podman.count_user_volumes(
                app.state.util.instance_id(), user["id"]
            )
            if count >= quota:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"volume quota reached: {count} of this user's "
                        f"volumes already exist and the server caps it at "
                        f"{quota} (KLANGKD_VOLUME_QUOTA_PER_USER). Delete "
                        "a volume first, or ask the operator to raise the "
                        "cap."
                    ),
                )
            info = await app.state.podman.create_volume(body.name, labels)
    else:
        # A non-managed name (another instance's or the operator's volume)
        # falls through to create_volume: podman's own create failure
        # raises PodmanError, which reaches the client as a bare 500 with
        # no probed name in the body. 500-vs-200 still hints at
        # existence, but no longer with a 409 + echoed detail (#2973).
        # In-instance cross-user names still 409 (issue scope:
        # instance-managed volumes only; the admin-surface rework,
        # #2993, narrows who can call this at all).
        info = await app.state.podman.create_volume(body.name, labels)
    return {"name": info["Name"], "created": info.get("CreatedAt", "")}


@router.delete("/volumes/{name}")
async def delete_volume(
    name: Annotated[str, Path(pattern=VOLUME_NAME_PATTERN)],
    _user: dict = Depends(acl.has_permission("manage-volumes")),
    app=Depends(get_app_dep),
):
    """Delete an instance-managed volume (#2993).

    ``manage-volumes`` is the whole gate — the surface is admin-only
    by seed, so the former per-user label check is gone (the admin
    tab lists and deletes any volume this instance manages). The
    creator label stays on the row as provenance. The name is
    pattern-validated (#2971): a raw path param reaches podman argv
    verbatim, and podman parses a leading-dash name as a flag —
    `--all` would remove every unused volume on the host.
    """
    info = await app.state.podman.inspect_volume(name)
    if info is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    labels = info.get("Labels") or {}
    if labels.get("klangk.instance") != app.state.util.instance_id():
        raise HTTPException(
            status_code=404,
            detail="Volume not managed by this Klangk instance",
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
