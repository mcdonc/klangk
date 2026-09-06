"""Workspace resource routes: files (list/read/delete/rename/download/
upload) and container resources (images + named volumes) — merged from the
former files and images submodules (images.py was misnamed: it lists
volumes too).

The data-level file routes (download, upload, rename, delete) each
write a best-effort ``file.download`` / ``file.write`` audit row
(#3257, SV-222471/472) through ``common.record_workspace_event``.
Text reads (``/files/content``) stay unaudited: the viewer's preview
path, bounded by the content-size cap, not an export channel."""

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
    Request,
    UploadFile,
)
from fastapi.responses import (
    StreamingResponse,
)
from pydantic import BaseModel, Field

from .. import (
    acl,
    stepup,
)
from ..container.spec import VOLUME_NAME_PATTERN
from ..podman import PodmanError as PodmanError
from ..util import (
    sanitize_disposition_name,
)
from .common import get_app_dep
from .common import (
    record_workspace_event,
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

    The shared except-ladder of the file routes: ValueError -> 400,
    FileNotFoundError -> 404 (*not_found* wording varies per route),
    FileExistsError -> 409, PermissionError -> 403, anything else ->
    500. Response details are generic (#3150): the files layer's
    exception messages carry podman stderr and container paths, which
    must reach the operator's log, not the client. The branches whose
    exceptions can carry raised-in text (400/403/500) log the
    underlying error and return a fixed wording; the 404/409 branches
    don't log — the files layer raises those with fixed server-
    composed strings, so there is nothing to diagnose. Routes with an
    extra case (or none of these) keep their own handling.
    """
    if isinstance(e, ValueError):
        logger.debug("files route rejected input: %s: %s", type(e).__name__, e)
        return HTTPException(status_code=400, detail="Invalid path")
    if isinstance(e, FileNotFoundError):
        return HTTPException(status_code=404, detail=not_found)
    if isinstance(e, FileExistsError):
        return HTTPException(
            status_code=409, detail="Destination already exists"
        )
    if isinstance(e, PermissionError):
        logger.info("files route denied: %s", e)
        return HTTPException(status_code=403, detail="Permission denied")
    logger.error("files route failed: %s", e, exc_info=True)
    return HTTPException(status_code=500, detail="Internal server error")


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
        raise _files_http_error(e) from None
    if content is None:
        raise HTTPException(
            status_code=404, detail="File not found or too large"
        )
    return {"path": path, "content": content}


@router.delete("/workspaces/{workspace_id}/files")
async def delete_file(
    workspace_id: str,
    path: str,
    request: Request,
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
    # A delete is a write-class change to workspace data (#3257,
    # SV-222472) — no byte size: the removed content is gone.
    await record_workspace_event(
        app, request, user, workspace_id, "file.write", {"path": deleted}
    )
    return {"path": deleted, "status": "deleted"}


class RenameFileRequest(BaseModel):
    old_path: str
    new_path: str


@router.post("/workspaces/{workspace_id}/files/rename")
async def rename_file(
    workspace_id: str,
    body: RenameFileRequest,
    request: Request,
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
    # A rename is a write-class change to workspace data (#3257,
    # SV-222472) — the new path names the row; the old one rides in
    # ``from``.
    await record_workspace_event(
        app,
        request,
        user,
        workspace_id,
        "file.write",
        {"path": renamed, "from": body.old_path},
    )
    return {"path": renamed, "status": "renamed"}


def _download_detail(path: str, info: dict) -> dict:
    """The ``file.download`` detail blob (#3257): the path plus the
    byte size — a file row only, since a directory's stat size is its
    inode, not the archive the stream produces."""
    if info["is_dir"]:
        return {"path": path}
    return {"path": path, "size": info["size"]}


@router.get("/workspaces/{workspace_id}/files/download")
async def download_file(
    workspace_id: str,
    path: str,
    request: Request,
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
        raise _files_http_error(e) from None
    if info is None:
        raise HTTPException(status_code=404, detail="Path not found")
    name = sanitize_disposition_name(posixpath.basename(path) or "download")
    # Data-level audit row (#3257, SV-222471): per-file downloads are
    # the exfiltration channel an incident review walks first.
    await record_workspace_event(
        app,
        request,
        user,
        workspace_id,
        "file.download",
        _download_detail(path, info),
    )
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


def _upload_filename(path: str, file: UploadFile) -> str:
    """The target filename: an explicit path wins, else the upload's
    basename; 400 when neither yields one."""
    filename = path if path else posixpath.basename(file.filename or "")
    if not filename:
        raise HTTPException(status_code=400, detail="No filename provided")
    return filename


async def _read_upload(file: UploadFile, max_upload: int) -> bytes:
    """Stream the whole upload into memory, enforcing the size cap
    (413 past the limit)."""
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
    return buf.getvalue()


@router.post("/workspaces/{workspace_id}/files/upload")
async def upload_file(
    workspace_id: str,
    file: UploadFile,
    request: Request,
    path: str = "",
    user: dict = Depends(acl.has_permission("files-view", workspace_resource)),
    _write: dict = Depends(
        acl.has_permission("files-write", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    cid = _require_container(workspace_id, app.state.container_registry)

    filename = _upload_filename(path, file)
    data = await _read_upload(file, app.state.settings.file_upload_size_max)

    try:
        saved_path = await app.state.files.write_file(
            cid,
            filename,
            data,
        )
    except (ValueError, OSError) as e:
        raise _files_http_error(e) from None
    # Data-level audit row (#3257, SV-222472): every byte written
    # into a workspace through the files API.
    await record_workspace_event(
        app,
        request,
        user,
        workspace_id,
        "file.write",
        {"path": saved_path, "size": len(data)},
    )
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


def _volume_workspace_label(v: dict) -> str | None:
    """The ``klangk.workspace-id`` owning-workspace label (#3153)."""
    return (v.get("Labels") or {}).get("klangk.workspace-id")


def _volume_matches(item: dict, needle: str) -> bool:
    """Whether a listing row matches the search needle — volume name,
    owning workspace name, or a using workspace's name
    (case-insensitive)."""
    return (
        needle in item["name"].lower()
        or needle in (item["workspace"] or "").lower()
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
    Volumes are workspace-owned (#3153): each row surfaces the owning
    workspace (``workspace_id`` from the podman label, ``workspace``
    its resolved name — null when the workspace row is gone) and which
    workspaces mount it (``workspaces``).

    Server-side paginated/sorted/filtered like the other admin tabs:
    ``q`` matches volume name, owning workspace name, or a using
    workspace name (case-insensitive substring); ``sort`` is ``name``
    | ``created``; returns the paged envelope ``{volumes, page,
    page_size, total}``.
    """
    volumes = await app.state.podman.list_volumes(
        f"klangk.instance={app.state.util.instance_id()}"
    )
    usage = _volume_usage_map(
        await app.state.model.workspaces.workspace_mount_rows()
    )
    names = await app.state.model.workspaces.workspace_name_map()
    items = [
        {
            "name": v["Name"],
            "created": v.get("CreatedAt", ""),
            "workspace_id": _volume_workspace_label(v),
            "workspace": names.get(_volume_workspace_label(v)),
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
    # The owning workspace (#3153): volumes are workspace-owned and
    # never shared, so creation must name the one workspace that may
    # mount the volume.
    workspace: str


async def _reject_in_instance_duplicate(app, name: str) -> None:
    """409 when *name* is already an instance-managed volume.

    Checked before the quota so an in-instance duplicate still reports
    409. A non-managed name (another instance's or the operator's
    volume) falls through: podman's own create failure raises
    PodmanError, which reaches the client as a bare 500 with no probed
    name in the body. 500-vs-200 still hints at existence, but no
    longer with a 409 + echoed detail (#2973). In-instance cross-user
    names still 409 (issue scope: instance-managed volumes only; the
    admin-surface rework, #2993, narrows who can call this at all).
    """
    existing = await app.state.podman.inspect_volume(name)
    if existing is None:
        return
    labels = existing.get("Labels") or {}
    if labels.get("klangk.instance") == app.state.util.instance_id():
        raise HTTPException(
            status_code=409, detail=f"Volume {name!r} already exists"
        )


def _volume_quota_error(count: int, quota: int) -> HTTPException:
    """The 429 raised when a create would pass the per-workspace
    volume cap — capacity, not authorization (not 403), and not
    auto-retried (not 503, which the CLI's request_with_retry
    re-drives)."""
    return HTTPException(
        status_code=429,
        detail=(
            f"volume quota reached: {count} of this workspace's "
            f"volumes already exist and the server caps it at "
            f"{quota} (KLANGKD_VOLUME_QUOTA_PER_WORKSPACE). Delete "
            "a volume first, or ask the operator to raise the cap."
        ),
    )


async def _create_volume_checked(
    app, workspace_id: str, name: str, labels: dict
) -> dict:
    """Create the volume under the per-workspace quota.

    The per-workspace lock spans count+create: without it, N concurrent
    creates each count the same pre-create total and all pass a cap
    they jointly exceed. The workspace-start auto-create door
    (container/spec.py ensure_volumes) shares the same lock and quota
    gate. Only runs the count when a quota is configured — the default
    0 keeps the create path exactly as before, with no extra podman
    call.
    """
    quota = app.state.settings.volume_quota_per_workspace
    if quota > 0:
        async with app.state.podman.volume_create_lock(workspace_id):
            count = await app.state.podman.count_workspace_volumes(
                app.state.util.instance_id(), workspace_id
            )
            if count >= quota:
                raise _volume_quota_error(count, quota)
            return await app.state.podman.create_volume(name, labels)
    return await app.state.podman.create_volume(name, labels)


@router.post("/volumes")
async def create_volume(
    body: CreateVolumeRequest,
    user: dict = Depends(acl.has_permission("manage-volumes")),
    app=Depends(get_app_dep),
):
    """Pre-create a volume owned by a workspace (#3153).

    Volumes are workspace-owned and never shared between workspaces,
    so a volume must name its owning workspace at creation — it is
    mountable by that workspace alone. There is deliberately no
    user-id stamp: whoever creates it is irrelevant.
    """
    workspace = await app.state.model.workspaces.get_workspace(body.workspace)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    await _reject_in_instance_duplicate(app, body.name)
    labels = {
        "klangk.managed": "true",
        "klangk.instance": app.state.util.instance_id(),
        "klangk.workspace-id": workspace["id"],
    }
    info = await _create_volume_checked(
        app, workspace["id"], body.name, labels
    )
    return {
        "name": info["Name"],
        "created": info.get("CreatedAt", ""),
        "workspace": workspace["id"],
    }


async def _require_managed_volume(app, name: str) -> dict:
    """The volume's info; 404 when missing or not managed by this
    Klangk instance."""
    info = await app.state.podman.inspect_volume(name)
    if info is None:
        raise HTTPException(status_code=404, detail="Volume not found")
    labels = info.get("Labels") or {}
    if labels.get("klangk.instance") != app.state.util.instance_id():
        raise HTTPException(
            status_code=404,
            detail="Volume not managed by this Klangk instance",
        )
    return info


async def _remove_volume(app, name: str) -> None:
    """Remove the volume; a podman 404 → 404, 409 (in use) → 409."""
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


@router.delete("/volumes/{name}")
async def delete_volume(
    name: Annotated[str, Path(pattern=VOLUME_NAME_PATTERN)],
    _user: dict = Depends(acl.has_permission("manage-volumes")),
    _step_up: None = Depends(stepup.require_step_up()),
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
    await _require_managed_volume(app, name)
    await _remove_volume(app, name)
    return {"status": "deleted"}
