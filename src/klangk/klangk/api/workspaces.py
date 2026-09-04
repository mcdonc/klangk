"""Workspace routes: CRUD, duplicate, restart, export/import, members, roles, group shares, ACL, and user search."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import (
    StreamingResponse,
)
from pydantic import AfterValidator, BaseModel
from sqlalchemy.exc import IntegrityError as SAIntegrityError

from .. import (
    acl,
    auth,
    netfilter as netfilter_mod,
    wshandler,
)
from ..exceptions import (
    AuditWriteError,
    NodeDrainingError,
    WorkspaceCapacityError,
)
from ..model.container_events import (
    CAUSE_API,
    CAUSE_CREATE,
    CAUSE_DELETE,
    CAUSE_RESTART,
    CAUSE_STOP,
)
from ..workspace_settings import (
    validate_nix_optin,
    validate_settings,
    validate_settings_patch,
)
from .common import get_app_dep
from ..model import (
    EGRESS_MODE_DEFAULT,
    EGRESS_MODES,
    PRINCIPAL_GROUP,
    PRINCIPAL_USER,
    normalize_classification_banner,
)
from ..util import (
    sanitize_disposition_name,
)
from .common import (
    ALL_PERMISSIONS,
    WorkspaceAclEntry,
    autostart_allowed,
    serialize_acl_entries,
    workspace_collection_resource,
    workspace_resource,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# Page size used on the backward-compatible bare-list path (no
# ``limit``/``offset``). Callers that pass no pagination params -- e.g.
# the workspace Settings panel, which looks up a workspace by id with
# ``firstWhere`` -- expect the *whole* list, not a silently truncated
# one. The explicit ``limit`` Query is capped at 100 for real list views;
# this larger ceiling keeps legacy clients from being cut off at the
# model default of 10 ("Workspace not found" past 10, #1266). It is a
# safety ceiling, not a hard contract -- a user with more workspaces
# than this should use explicit pagination.
BARE_LIST_LIMIT = 500


def _validate_allowed_domains(
    values: list[str] | None, app
) -> list[str] | None:
    """Validate + normalize a workspace's ``allowed_domains`` list.

    Returns the validated list (de-duplicated, ordered), or ``None`` when
    no list was supplied (unrestricted egress). Raises an HTTP 400 with a
    precise message on any malformed ``host[:port]`` entry. Only warns —
    never rejects — when the network sidecar is not configured: the value
    is still persisted so it takes effect the moment the operator sets
    ``KLANGKD_NETWORK_SIDECAR_IMAGE`` (#1365, #2255).
    """
    if not values:
        return None
    try:
        domains = netfilter_mod.parse_allowed_domains(values)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not app.state.netfilter.enabled():
        logger.warning(
            "Workspace configured allowed_domains=%s but the network "
            "sidecar is disabled on this server "
            "(KLANGKD_NETFILTER_ENABLED=false or "
            "KLANGKD_NETWORK_SIDECAR_IMAGE empty); the value is persisted "
            "but the workspace will fail to start until filtering is "
            "re-enabled (#1365, #2255).",
            domains,
        )
    return domains


def _reject_cidr_specs(values: list[str]) -> None:
    """Raise HTTP 400 on any CIDR spec in a ``rejected_domains`` list.

    The deny list is host-only (#2367): the network sidecar NXDOMAINs a
    rejected name *before* resolution, which has no IP/CIDR dimension,
    and a deny-list must not silently ignore an entry an operator
    believed was blocking something.
    """
    for raw in values:
        spec = raw.strip()
        if spec and "/" in spec:
            raise HTTPException(
                status_code=400,
                detail=(
                    "rejected_domains does not support CIDR specs (a rejected"
                    f" name is NXDOMAIN'd before resolution): {raw!r}."
                    " Use a host/domain spec instead."
                ),
            )


def _validate_rejected_domains(
    values: list[str] | None, app
) -> list[str] | None:
    """Validate + normalize a workspace's ``rejected_domains`` list (#2367).

    The deny counterpart to :func:`_validate_allowed_domains`. Reuses
    :func:`klangk.netfilter.parse_allowed_domains` so the host grammar matches
    (bare = exact apex, ``.host`` = apex + subdomains, ``*.host`` = subdomains
    only, #2377). Host-only: a CIDR spec is rejected up front (see
    :func:`_reject_cidr_specs`). Raises HTTP 400 on a malformed entry or a
    CIDR. Only warns -- never rejects -- when the network sidecar is not
    configured (the value is persisted for when filtering is re-enabled).
    """
    if not values:
        return None
    _reject_cidr_specs(values)
    try:
        domains = netfilter_mod.parse_allowed_domains(
            values, label="rejected_domains"
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not app.state.netfilter.enabled():
        logger.warning(
            "Workspace configured rejected_domains=%s but the network "
            "sidecar is disabled on this server; the value is persisted "
            "but takes effect only once filtering is re-enabled (#2367).",
            domains,
        )
    return domains


def _annotate_running(items: list[dict], container_registry) -> list[dict]:
    """Annotate each workspace dict with live container/health state.

    Adds ``running`` (bool) and, for running workspaces, the live
    ``health`` (``"healthy"`` / ``"unhealthy"`` / ``None`` until the
    first poll completes) and ``health_message`` (the bounded failure
    reason, or ``None``). Surfacing these here means the front-page
    workspace list reflects a workspace that is *already* unhealthy on
    page load -- not only one that transitions while the page is open.

    The ``HealthMonitor`` only broadcasts a ``service_health`` WebSocket
    event on a health *transition* (an anti-spam choice, so a steady-state
    failure doesn't push to every connection every poll). Without this
    annotation, a workspace unhealthy before any client connected would
    never be visible: no transition event fires, and the list payload
    carried no live health. ``registry.get_state`` is already fetched for
    the ``running`` flag, so the health fields ride along at no extra
    lookup cost. See #1173.
    """
    for ws in items:
        state = container_registry.get_state(ws["id"])
        ws["running"] = state is not None
        if state is not None:
            ws["health"] = state.health_status
            ws["health_message"] = state.health_message
            ws["service_started_at"] = state.service_started_at
        else:
            ws["service_started_at"] = None
        # Crash-recovery state (#2524): None when the workspace has no
        # restart bookkeeping (the common case), else the backing-off /
        # crash-loop info so a dead workspace shows *why* it is down.
        ws["restart"] = container_registry.crash.status(ws["id"])
    return items


# Shared list-endpoint query parameters (owned/shared listings): the
# Annotated aliases keep the two endpoints' OpenAPI contract identical
# while declaring the pagination controls once (#2904). Defaults stay
# on the ``=`` in each signature (FastAPI's rule for Annotated params).
LimitQuery = Annotated[int | None, Query(ge=1, le=100)]
OffsetQuery = Annotated[int | None, Query(ge=0)]
SortQuery = Annotated[Literal["name", "created"], Query()]
OrderQuery = Annotated[Literal["asc", "desc"], Query()]
SearchQuery = Annotated[str | None, Query()]


async def _list_response(fetch, app, limit, offset):
    """Shared list-endpoint body (#2553): bare/envelope shape + running
    annotation. *fetch* is a callable(limit, offset) returning the model's
    pagination result. Without ``limit``/``offset`` (backward-compatible)
    returns a bare list; with them, the envelope dict.
    """
    bare = limit is None and offset is None
    result = await fetch(
        limit=BARE_LIST_LIMIT if bare else limit,
        offset=offset or 0,
    )
    _annotate_running(result["items"], app.state.container_registry)
    if bare:
        return result["items"]
    return result


@router.get("/workspaces")
async def list_workspaces(
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
    limit: LimitQuery = None,
    offset: OffsetQuery = None,
    sort: SortQuery = "created",
    order: OrderQuery = "desc",
    q: SearchQuery = None,
):
    """List workspaces owned by the user.

    Without ``limit``/``offset`` (backward-compatible) returns a bare list.
    With pagination params returns an envelope
    ``{"items": [...], "has_more": bool, "next_offset": int | None}``.
    ``sort`` (``name``/``created``), ``order`` (``asc``/``desc``) and ``q``
    (name substring) apply in both shapes.
    """
    return await _list_response(
        lambda limit, offset: app.state.workspaces.list_workspaces(
            user["id"], limit=limit, offset=offset, sort=sort, order=order, q=q
        ),
        app,
        limit,
        offset,
    )


@router.get("/workspaces/shared")
async def list_shared_workspaces(
    user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
    limit: LimitQuery = None,
    offset: OffsetQuery = None,
    sort: SortQuery = "created",
    order: OrderQuery = "desc",
    q: SearchQuery = None,
):
    """List workspaces shared with the user.

    Without ``limit``/``offset`` (backward-compatible) returns a bare list.
    With pagination params returns an envelope (see ``list_workspaces``).
    """
    return await _list_response(
        lambda limit, offset: (
            app.state.model.workspaces.list_shared_workspaces(
                user["id"],
                limit=limit,
                offset=offset,
                sort=sort,
                order=order,
                q=q,
            )
        ),
        app,
        limit,
        offset,
    )


def _validate_workspace_name(value: str) -> str:
    """#3110: a workspace name must carry at least one
    non-whitespace character.

    ``min_length=1`` alone would accept ``" "``; this keeps every
    surface (web frontend, TUI, API callers) consistent with the
    `klangk edit` command's blank-``--name`` rejection (PR #3103).
    """
    if not value.strip():
        raise ValueError("Workspace name cannot be empty or only whitespace")
    return value


WorkspaceName = Annotated[str, AfterValidator(_validate_workspace_name)]


class WorkspaceBodyFields(BaseModel):
    """The optional workspace fields shared verbatim by the create
    (POST) and update (PUT) bodies.

    Fields whose type or default *differs* between the two bodies
    (``name``, ``auto_start``, ``egress_mode``, ``per_handle_home``)
    stay declared on the concrete classes; everything else lives here
    so the two request schemas cannot drift apart.
    """

    image: str | None = None
    service_command: str | None = None
    mounts: list[str] | None = None
    env: dict[str, str] | None = None
    setup_state: Literal["pending", "complete", "failed"] | None = None
    health_check: str | None = None
    allowed_domains: list[str] | None = None
    rejected_domains: list[str] | None = None
    settings: dict | None = None


class CreateWorkspaceRequest(WorkspaceBodyFields):
    name: WorkspaceName
    auto_start: bool = False
    egress_mode: Literal["static", "interactive", "allow"] = (
        EGRESS_MODE_DEFAULT
    )
    # None = store the deploy flag's value (KLANGKD_PER_HANDLE_HOME —
    # which, with the ceiling on, is true, so an untouched create gets
    # per-handle homes); the handler resolves it before the create.
    # Editable afterwards via PUT (a flip applies to the layout realized
    # on the next connect/start). #3135: the deploy flag is a ceiling —
    # an explicit true is stored as-is but inert while the ceiling is
    # off (resolve_per_handle_home clamps at start/connect, mirroring
    # allow_sudo's #3047 choice of clamp over 400).
    per_handle_home: bool | None = None
    # Classification marking rendered as the persistent banner (#2768).
    # Free text, one line. None/empty = inherit the deploy default
    # (KLANGKD_CLASSIFICATION_BANNER), resolved at display time.
    classification_banner: str | None = None


@router.post("/workspaces")
async def create_workspace(
    body: CreateWorkspaceRequest,
    user: dict = Depends(
        acl.has_permission("create-workspace", workspace_collection_resource)
    ),
    app=Depends(get_app_dep),
):
    fields = _validate_create_fields(body, app)
    try:
        ws = await app.state.workspaces.create_workspace(
            user["id"],
            body.name,
            image=body.image,
            service_command=body.service_command,
            auto_start=body.auto_start,
            mounts=body.mounts,
            env=body.env,
            setup_state=body.setup_state or "complete",
            health_check=body.health_check,
            allowed_domains=fields["allowed_domains"],
            rejected_domains=fields["rejected_domains"],
            settings=fields["settings"],
            egress_mode=body.egress_mode,
            per_handle_home=fields["per_handle_home"],
            classification_banner=fields["classification_banner"],
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {body.name!r} already exists",
        )
    except OSError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Eagerly start the container so it's running by the time the
    # user connects.  Errors are logged but don't fail the create.
    # The service command fires at the create choke point inside
    # start_container (see ContainerRegistry.bringup, #1244), gated on
    # setup_state so workspaces whose setup.sh hasn't run yet defer until
    # complete.
    await _eager_start(app, body, ws, user["id"])

    app.state.sockets.notify_user_workspaces_changed(user["id"])
    return ws


def _check_autostart(auto_start, app) -> None:
    """400 when a body asks for auto-start on a server without it."""
    if auto_start and not autostart_allowed(app):
        raise HTTPException(
            status_code=400,
            detail="Auto-start is not enabled on this server"
            " (set KLANGKD_ALLOW_AUTOSTART=1)",
        )


def _check_image(image: str | None, app) -> None:
    """400 when *image* is present but not on this instance's allow-list.

    ``None`` (absent) skips; any other value must be allowed. The create
    path calls this only for truthy images (an empty image on create
    means "the deploy default").
    """
    if image is not None and image not in (
        app.state.container_registry.allowed_images
    ):
        raise HTTPException(
            status_code=400,
            detail=f"Image {image!r} is not allowed. "
            f"Allowed: {sorted(app.state.container_registry.allowed_images)}",
        )


def _check_mounts(mounts, app) -> None:
    """400 when *mounts* is a present, non-empty list that fails this
    instance's mount validation."""
    if not mounts:
        return
    mount_err = app.state.container_registry.validate_mounts(mounts)
    if mount_err:
        raise HTTPException(status_code=400, detail=mount_err)


def _validated_settings(raw) -> dict:
    """Run :func:`validate_settings`, re-raising ``ValueError`` as 400."""
    try:
        return validate_settings(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _check_nix_optin(settings: dict, app, previous=None) -> None:
    """400 on a nix opt-in while the feature is off (#2560).

    *previous* is the already-stored bag a PUT may echo (update path);
    ``None`` on create/import where any nix=true rejects.
    """
    try:
        validate_nix_optin(
            settings,
            nix_available=app.state.nix.available,
            previous=previous,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _normalized_banner(classification_banner) -> str | None:
    """Normalize a classification marking, re-raising ``ValueError`` as
    400."""
    try:
        return normalize_classification_banner(classification_banner)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validate_create_fields(body, app) -> dict:
    """Validate the POST body (400s raise); returns the derived
    allowed/rejected domains, settings, per_handle_home, and banner."""
    _check_autostart(body.auto_start, app)
    if body.image:
        _check_image(body.image, app)
    _check_mounts(body.mounts, app)
    settings = _validated_settings(body.settings)
    # #2560: while the feature is off, a create may not opt in to nix
    # (there is no existing bag on create, so any nix=true rejects).
    _check_nix_optin(settings, app)
    return {
        "allowed_domains": _validate_allowed_domains(
            body.allowed_domains, app
        ),
        "rejected_domains": _validate_rejected_domains(
            body.rejected_domains, app
        ),
        "settings": settings,
        # #3135: an omitted field stores the deploy flag — which equals
        # the ceiling, so this is true exactly when per-handle homes are
        # permitted. An explicit value is stored verbatim; the ceiling
        # clamps at start/connect, not here (no 400 — allow_sudo's
        # #3047 clamp choice).
        "per_handle_home": (
            body.per_handle_home
            if body.per_handle_home is not None
            else app.state.settings.per_handle_home
        ),
        "classification_banner": _normalized_banner(
            body.classification_banner
        ),
    }


async def _eager_start(app, body, ws, actor_id: str | None) -> None:
    """Eagerly start the container when the body asked for it; errors are
    logged but never fail the create."""
    if not body.auto_start:
        return
    try:
        await app.state.workspaces.start_workspace(
            ws, actor_id=actor_id, cause=CAUSE_CREATE
        )
    except NodeDrainingError:
        # A graceful-restart drain raced the create (#2527): the
        # workspace row exists but no container may start. Not worth
        # failing the create — it simply won't run until the restart
        # completes (a start then succeeds).
        logger.warning(
            "Node refuses new starts mid-create: workspace %s "
            "created but not started",
            ws["id"],
        )
    except WorkspaceCapacityError as exc:
        # Admission control refused the eager start (#2525): the
        # workspace row exists (creation is not capacity-gated —
        # only starts are); it runs once capacity frees up. Logged
        # as a clear warning rather than a traceback-level failure.
        logger.warning(
            "Capacity refused eager start of workspace %s: %s",
            ws["id"],
            exc,
        )
    except Exception:
        logger.warning(
            "Eager start failed for workspace %s",
            ws["id"],
            exc_info=True,
        )


class UpdateWorkspaceRequest(WorkspaceBodyFields):
    name: WorkspaceName | None = None
    auto_start: bool | None = None
    # egress_mode (like allowed_domains) is enforced by the network
    # sidecar at container start, so a change here takes effect on the
    # next start/restart, not on the live container (PR #2248 review N3).
    egress_mode: Literal["static", "interactive", "allow"] | None = None
    # Like egress_mode, per_handle_home takes effect on the next
    # connect/start, never on a live session (#2719). Note an explicit
    # null stores 0 (shared) via the truthy coercion — same as
    # auto_start; only POST's null means "inherit the deploy default".
    # #3135: the deploy flag is a ceiling — a stored true is inert
    # while the ceiling is off (clamped at start/connect, no 400).
    per_handle_home: bool | None = None
    # Classification marking (#2768): full-replace like the other PUT
    # fields. A present-but-empty/whitespace value CLEARS the override
    # (back to inheriting the deploy default, resolved at display time).
    classification_banner: str | None = None


# PUT-updatable columns that are NOT NULL and whose null has no
# documented PUT meaning: an explicit null must be a 400, not a
# constraint violation surfacing as a fabricated 409 collision
# (#3097) or a ValueError 500 off the enum coercers. auto_start /
# per_handle_home nulls stay legal — they coerce to 0 by design
# (see UpdateWorkspaceRequest).
_NOT_NULL_UPDATE_FIELDS = frozenset({"name", "setup_state", "egress_mode"})


def _reject_null_fields(fields: dict) -> None:
    """400 when the PUT body explicitly nulls a NOT NULL column.

    ``exclude_unset=True`` keeps keys the client sent as ``null``, so
    without this check a ``{"name": null}`` reaches the UPDATE and trips
    the constraint — which the rename-collision mapping would then
    misreport as "A workspace named None already exists"."""
    for key in _NOT_NULL_UPDATE_FIELDS:
        if key in fields and fields[key] is None:
            raise HTTPException(
                status_code=400, detail=f"Field '{key}' cannot be null"
            )


def _validate_update_fields(app, fields: dict) -> None:
    """Validate the PUT body's fields (mirrors the create API); mutates
    ``fields`` in place (normalized domain lists / settings / banner)."""
    _validate_update_core(app, fields)
    _normalize_update_fields(app, fields)


def _validate_update_core(app, fields: dict) -> None:
    """The 400-raising checks: null rejection, autostart enablement,
    image allow-list, mount validity."""
    _reject_null_fields(fields)
    _check_autostart(fields.get("auto_start"), app)
    if "image" in fields:
        _check_image(fields["image"], app)
    _check_mounts(fields.get("mounts"), app)


def _normalize_update_fields(app, fields: dict) -> None:
    """Normalize in place: domain lists, settings, classification banner."""
    if "allowed_domains" in fields:
        fields["allowed_domains"] = _validate_allowed_domains(
            fields["allowed_domains"], app
        )
    if "rejected_domains" in fields:
        fields["rejected_domains"] = _validate_rejected_domains(
            fields["rejected_domains"], app
        )
    # settings is a full-replace on PUT (None = clear the whole bag).
    # ``exclude_unset=True`` means the key is present only when the client
    # sent it; a missing ``settings`` key leaves the bag untouched.
    if "settings" in fields:
        fields["settings"] = _validated_settings(fields["settings"])
    if "classification_banner" in fields:
        fields["classification_banner"] = _normalized_banner(
            fields["classification_banner"]
        )


def _notify_workspace_audience(
    app, user: dict, workspace: dict, members: list[dict]
) -> None:
    """Push workspaces-changed to the caller, the owner, and every
    shared member -- the exact audience a workspace-scoped change
    re-renders for."""
    member_ids = {m["id"] for m in members}
    member_ids.update({user["id"], workspace["user_id"]})
    for uid in member_ids:
        app.state.sockets.notify_user_workspaces_changed(uid)


async def _notify_marking_change(
    app, user: dict, workspace: dict, fields: dict
) -> None:
    """#2768: a marking change re-renders the persistent banner, so push
    the workspaces-changed notification to every client that can view
    the workspace — the owner, the editor (a shared member with the
    edit ACE may not be the owner), and every ACL-shared member (they
    view the same page via /workspaces/shared and re-resolve the
    effective marking on this push; without it they keep viewing the
    old, lower marking until a manual reload)."""
    if "classification_banner" not in fields:
        return
    members = await app.state.model.workspaces.get_workspace_members(
        workspace["id"]
    )
    _notify_workspace_audience(app, user, workspace, members)


def _reset_health_probe(live_state, health_check) -> None:
    """Apply an edited health_check to live state and reset the cached
    status so the next poll re-broadcasts (#1015)."""
    live_state.health_check = health_check or None
    live_state.health_status = None
    live_state.health_checked_at = None
    live_state.health_message = None


async def _sync_tmux_workspace_name(app, live_state, fields: dict) -> None:
    """Keep the tmux status bar in sync when the workspace is renamed
    (#1880): open terminals would otherwise keep showing the old
    name until a new terminal_start fires. Idempotent + non-fatal."""
    if "name" in fields and app.state.terminal.tmux_enabled():
        await app.state.terminal.set_workspace_name(
            live_state.container_id, fields["name"]
        )


async def _apply_live_state_updates(
    app, workspace_id: str, fields: dict
) -> None:
    """Propagate health-relevant config changes to the live container
    state (#1015) so HealthMonitor picks them up without a container
    restart: setup_state may flip to "complete" after setup finishes,
    and health_check may be edited at any time."""
    live_state = app.state.container_registry.get_state(workspace_id)
    if live_state is None:
        return
    if "setup_state" in fields:
        live_state.setup_state = fields["setup_state"]
    if "health_check" in fields:
        _reset_health_probe(live_state, fields["health_check"])
    await _sync_tmux_workspace_name(app, live_state, fields)


async def _update_workspace_fields(
    app, workspace_id: str, owner_id: str, fields: dict
) -> bool:
    """Apply a workspace field update; 409 when a rename collides with
    another name the owner already holds (UNIQUE(user_id, name)) — the
    same mapping the create, duplicate, and import paths apply."""
    try:
        return await app.state.model.workspaces.update_workspace(
            workspace_id, owner_id, **fields
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A workspace named {fields.get('name')!r} already exists"
            ),
        )


@router.put("/workspaces/{workspace_id}")
async def update_workspace(
    workspace_id: str,
    body: UpdateWorkspaceRequest,
    user: dict = Depends(
        acl.has_permission("edit-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    fields = body.model_dump(exclude_unset=True)
    _validate_update_fields(app, fields)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    workspace = await app.state.model.workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if "settings" in fields:
        # #2560: PUT settings is a full-replace bag; a new nix=true opt-in
        # rejects while the feature is off, but an echo of the workspace's
        # already-stored true is tolerated (clients merge over the bag).
        _check_nix_optin(
            fields["settings"], app, previous=workspace["settings"]
        )
    updated = await _update_workspace_fields(
        app, workspace_id, workspace["user_id"], fields
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Workspace not found")

    await _notify_marking_change(app, user, workspace, fields)
    await _apply_live_state_updates(app, workspace_id, fields)

    return {"status": "updated"}


class UpdateWorkspaceSettingsRequest(BaseModel):
    """Body for ``PATCH /workspaces/{id}/settings`` (#864).

    A flat map of setting key → value. A ``null`` value deletes that key
    (reverts it to the deploy-wide default); any other value sets/replaces
    the override. Keys not present in the patch are left untouched. This is
    a partial-merge (read-modify-write), not a full replace — use
    ``PUT /workspaces/{id}`` with a full ``settings`` dict for that.
    """

    model_config = {"extra": "allow"}


@router.patch("/workspaces/{workspace_id}/settings")
async def update_workspace_settings(
    workspace_id: str,
    body: UpdateWorkspaceSettingsRequest,
    user: dict = Depends(
        acl.has_permission("edit-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Partial-merge update of the per-workspace ``settings`` bag (#864).

    Each key in the body sets/replaces that override; a ``null`` value
    deletes the key (reverting to the deploy default). Returns the
    post-merge settings dict (or ``None`` if the bag is now empty).
    """
    try:
        patch = validate_settings_patch(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Resolve the *owner* (not the caller) — the model's settings merge is
    # owner-scoped (``WHERE id = ? AND user_id = ?``), and a shared non-owner
    # with the ``edit`` ACE must be able to patch settings just as they can
    # PUT other fields. The ``edit`` ACL dependency has already gated access
    # (and rejected nonexistent workspaces as 403), so a missing row here is
    # a race, not a normal path.
    workspace = await app.state.model.workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    # #2560: same rule as PUT — a patch that flips nix to true rejects while
    # the feature is off, unless it merely re-asserts the stored value.
    try:
        validate_nix_optin(
            patch,
            nix_available=app.state.nix.available,
            previous=workspace["settings"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    merged = await app.state.model.workspaces.update_workspace_settings(
        workspace_id, workspace["user_id"], patch
    )
    return {"settings": merged}


class DuplicateWorkspaceRequest(BaseModel):
    name: WorkspaceName


@router.post("/workspaces/{workspace_id}/duplicate")
async def duplicate_workspace(
    workspace_id: str,
    body: DuplicateWorkspaceRequest,
    user: dict = Depends(
        acl.has_permission("duplicate-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    # #2569: duplicating creates a new workspace — check collection-level
    # create permission in addition to the per-workspace create above.
    # Defense-in-depth: the Depends check above already walks to
    # /workspaces, so this branch is unreachable in practice.
    principals = await app.state.acl.get_principals(user["id"])
    if not await app.state.acl.check_permission(
        "/workspaces", principals, "create-workspace"
    ):
        raise HTTPException(
            status_code=403,
            detail="Not permitted to create workspaces",
        )
    source = await app.state.model.workspaces.get_workspace(workspace_id)
    if source is None:  # pragma: no cover — race after ACL check
        raise HTTPException(status_code=404, detail="Workspace not found")
    # #2560: the clone carries the source's settings bag verbatim — a
    # stored nix=true is persisted state, not a new opt-in, so it is not
    # rejected while the feature is off (it stays inert like the source's).
    try:
        ws = await app.state.workspaces.create_workspace(
            user["id"],
            body.name,
            image=source.get("image"),
            service_command=source.get("service_command"),
            auto_start=source.get("auto_start", False),
            mounts=source.get("mounts"),
            env=source.get("env"),
            health_check=source.get("health_check"),
            allowed_domains=source.get("allowed_domains"),
            rejected_domains=source.get("rejected_domains"),
            settings=source.get("settings"),
            egress_mode=source.get("egress_mode"),
            per_handle_home=source.get("per_handle_home"),
            classification_banner=source.get("classification_banner"),
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {body.name!r} already exists",
        )
    return ws


async def _stop_workspace_container(
    app, workspace: dict, cause: str, actor_id: str | None
) -> None:
    """Stop and remove the workspace's container.

    Prefers the live container_id from the registry (tracks the currently
    running container) over the DB value (may be stale if the container
    was already stopped by idle timeout). No-op when neither is set.

    #3154: under ``KLANGKD_AUDIT_FAIL_CLOSED`` an audit-write failure
    raises :class:`AuditWriteError` BEFORE any teardown — callers
    (/restart, /delete) map it to a 503 and leave the container running.
    """
    live_state = app.state.container_registry.get_state(workspace["id"])
    cid = (
        live_state.container_id
        if live_state
        else workspace.get("container_id")
    )
    if cid:
        await app.state.container_registry.stop_and_remove_container(
            cid,
            workspace_id=workspace["id"],
            cause=cause,
            actor_id=actor_id,
        )


async def _stop_and_broadcast(
    app, workspace_id: str, cid: str, user_id: str
) -> None:
    """Stop a running workspace container and tell live WS viewers it
    was stopped on purpose (re-homed from the retired WS
    ``shutdown_container`` handler).

    #3154: under ``KLANGKD_AUDIT_FAIL_CLOSED`` an audit-write failure
    refuses the stop with a 503 before any teardown. The terminal death
    frames fire BEFORE the stop (they need the registry state the stop
    tears down), so a refusal may follow a ``running=False`` frame;
    clients re-sync from the next status poll and the container itself
    is untouched.
    """
    await app.state.container_registry.notify_workspace_killed(
        workspace_id, container_id=cid
    )
    try:
        await app.state.container_registry.stop_and_remove_container(
            cid,
            workspace_id=workspace_id,
            cause=CAUSE_STOP,
            actor_id=user_id,
        )
    except AuditWriteError as exc:
        # Fail-closed audit refusal (#3154): the stop never started —
        # refuse the request, leave the container running for a retry.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # Notify live WS viewers that the container was stopped on purpose
    # so the UI shows "stopped" rather than "disconnected". Only when a
    # container was actually stopped — a no-op /stop on an
    # already-stopped workspace must not broadcast. Safe before
    # reset_workspace_state: a session with subscribers survives reset
    # (remove_session is a no-op while subscribers remain).
    session = app.state.sockets.get_session(workspace_id)
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


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("delete-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    workspace = await app.state.model.workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    # Capture shared members before we tear down ACL entries, so we can
    # notify them (and the owner/deleter) that the workspace is gone.
    members = await app.state.model.workspaces.get_workspace_members(
        workspace_id
    )

    # _stop_workspace_container + reset_workspace_state (below) also stop
    # the agent session and clear shared state; the agent subprocess runs
    # inside the container, so stopping the container kills it either way.
    # #3154: a fail-closed audit refusal 503s here — the workspace row and
    # its container survive for a retry (the stop never started).
    try:
        await _stop_workspace_container(
            app, workspace, CAUSE_DELETE, user["id"]
        )
    except AuditWriteError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await wshandler.reset_workspace_state(app.state.sockets, workspace_id)

    deleted = await app.state.workspaces.delete_workspace(
        workspace_id, workspace["user_id"]
    )
    if not deleted:  # pragma: no cover — race between get and delete
        raise HTTPException(status_code=404, detail="Workspace not found")
    # Clean up ACL entries for this workspace
    await app.state.model.acl.delete_acl_entries_for_resource(
        f"/workspaces/{workspace_id}"
    )
    # Notify the deleter, the owner, and any shared members so their
    # workspace list refreshes (members were fetched above, before the
    # resource's ACL entries were removed).
    _notify_workspace_audience(app, user, workspace, members)
    return {"status": "deleted"}


@router.post("/workspaces/{workspace_id}/restart")
async def restart_workspace(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("restart-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Restart a workspace container.

    Stops and removes the running container, then eagerly starts a
    fresh one with the same workspace config (#1244). The service
    command re-fires at the create choke point, so a service workspace
    recovers to healthy.

    #2527: a draining node (graceful restart in progress) refuses the
    restart up front — checking *before* the stop keeps a running
    workspace running (existing workspaces survive until the restart's
    own drain), instead of stopping it and then failing the start.
    """
    blocked = app.state.container_registry.new_starts_blocked_reason()
    if blocked:
        raise HTTPException(status_code=503, detail=blocked)
    workspace = await app.state.model.workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    # #3154: a fail-closed audit refusal on either half maps to a 503.
    # On the stop half the container is untouched (audit-before-act); on
    # the start half the stop already succeeded under its own audited row,
    # so the workspace is simply left stopped for a retry.
    try:
        await _stop_workspace_container(
            app, workspace, CAUSE_RESTART, user["id"]
        )
    except AuditWriteError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await wshandler.reset_workspace_state(app.state.sockets, workspace_id)
    # Start a fresh container; the service command fires via the
    # create choke point in start_container.
    await _start_or_http_error(app, workspace, user["id"], cause=CAUSE_RESTART)
    return {"status": "restarted"}


@router.post("/workspaces/{workspace_id}/stop")
async def stop_workspace(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("stop-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Stop a running workspace container.

    Emits the terminal status/death frames, stops and removes the
    container, and closes active terminal sessions.  Idempotent — a
    404 is returned if the workspace doesn't exist; a no-op (200) if
    it isn't running.
    """
    workspace = await app.state.model.workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    live_state = app.state.container_registry.get_state(workspace_id)
    cid = (
        live_state.container_id
        if live_state
        else workspace.get("container_id")
    )
    if cid:
        await _stop_and_broadcast(app, workspace_id, cid, user["id"])
    await wshandler.reset_workspace_state(app.state.sockets, workspace_id)
    return {"status": "stopped"}


async def _start_or_http_error(
    app, workspace: dict, actor_id, cause: str = CAUSE_API
) -> None:
    """Start a workspace container, mapping the service-layer errors to
    client-distinguishable HTTP statuses.

    On a drain/capacity refusal the stop (restart path) already
    happened, so the workspace is simply left stopped; capacity and
    drain state are re-checked on every start (#2525 / #2527).
    """
    try:
        await app.state.workspaces.start_workspace(
            workspace, actor_id=actor_id, cause=cause
        )
    except NodeDrainingError as exc:
        # Draining node (#2527): clear 503 so clients/CLI can distinguish
        # "temporarily disabled by a restart" from a config error.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except WorkspaceCapacityError as exc:
        # Capacity refusal (#2525): distinguishable 503 with an
        # actionable "stop a workspace / free memory" detail.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except AuditWriteError as exc:
        # Fail-closed audit refusal (#3154): the start never began —
        # nothing to roll back; the 503 detail carries the audit cause.
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        # User-config error (e.g. a bind-mount source path that doesn't
        # exist) — surface as a 400, not an unhandled 500 (#2157). The WS
        # start path sends an error frame for the same condition.
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/workspaces/{workspace_id}/start")
async def start_workspace(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("start-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Start a stopped workspace container.

    Creates a fresh container from the workspace config (service command
    re-fires via the create choke point).  No-op if already running.
    """
    workspace = await app.state.model.workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    if app.state.container_registry.get_state(workspace_id) is not None:
        return {"status": "already_running"}
    await _start_or_http_error(app, workspace, user["id"])
    return {"status": "started"}


@router.get("/workspaces/{workspace_id}/status")
async def workspace_status(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("monitor-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Return container status for a workspace.

    Returns running state, container health, idle timeout info,
    and allocated ports.
    """
    workspace = await app.state.model.workspaces.get_workspace(workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    live_state = app.state.container_registry.get_state(workspace_id)
    if live_state is None:
        return {
            "running": False,
            "container_id": None,
            "health": None,
            "health_message": None,
            "health_checked_at": None,
            "idle_seconds": None,
            "idle_timeout": None,
            "ports": [],
            # Crash-recovery bookkeeping (#2524): None when the workspace
            # died (or was stopped) with no restart history, else the
            # classified last death cause and the backoff / crash-loop
            # state — the visible terminal state for a crash-looping
            # workspace.
            "restart": app.state.container_registry.crash.status(workspace_id),
        }

    idle_secs = time.time() - live_state.last_activity
    idle_timeout = live_state.get_idle_timeout()
    ports = await app.state.container_registry.get_workspace_ports(
        workspace_id
    )

    # Map the internal status to the API shape.  ``health`` is None
    # until the first check completes (or when no health_check is
    # configured) (#1015).
    health = live_state.health_status
    checked_at = (
        datetime.fromtimestamp(
            live_state.health_checked_at, tz=timezone.utc
        ).isoformat()
        if live_state.health_checked_at is not None
        else None
    )

    return {
        "running": True,
        "container_id": live_state.container_id,
        "health": health,
        # Why the last check failed (bounded stderr/stdout tail), or
        # None when healthy -- so an unhealthy workspace isn't a black
        # box (#1088).
        "health_message": live_state.health_message,
        "health_checked_at": checked_at,
        "idle_seconds": round(idle_secs, 1),
        "idle_timeout": idle_timeout,
        "ports": ports,
        # #2524: present while a restarted container is still inside its
        # stability window ("recovering"), None once stable.
        "restart": app.state.container_registry.crash.status(workspace_id),
    }


# --- Workspace export/import endpoints ---


async def _exportable_workspace(app, workspace_id: str, user_id) -> dict:
    """Resolve the workspace to export; 404 when it cannot be found.

    Owner-scoped first; the caller may hold export via a group ACE (e.g.
    the owners role group) rather than a direct access row, in which case
    the bare-id lookup finds it — the permission layer already gated.
    """
    workspace = await app.state.model.workspaces.get_workspace(
        workspace_id, user_id
    )
    if workspace is None:
        workspace = await app.state.model.workspaces.get_workspace_by_id(
            workspace_id
        )
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


async def _estimate_home_size(home_dir) -> int:
    """Estimate the uncompressed home size (bytes) for the client's
    progress display; 0 when ``du`` is unavailable or fails."""
    if not home_dir.exists():
        return 0
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["du", "-sb", str(home_dir)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass  # fall back to 0
    return 0


@router.get("/workspaces/{workspace_id}/export")
async def export_workspace(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("export-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Export a workspace as a .tar.gz archive.

    Requires the ``export`` permission on ``/workspaces/{id}`` (#2707).
    The owner's wildcard ACE and the seeded ``owners-<id>`` role group
    both cover it, so owners can export their own workspaces; admins no
    longer blanket-export workspaces they hold no grant on. A deny ACE
    for ``export`` on the workspace resource (positioned ahead of the
    wildcard allows) revokes it per workspace.

    The archive contains workspace.json (metadata) and the home
    directory tree under home/.
    """
    workspace = await _exportable_workspace(app, workspace_id, user["id"])

    # Pre-flight before the response starts (#3101): once the first
    # chunk goes out the status line is already 200, so a tar binary
    # that cannot even start must fail here as a clean 500 instead of
    # an empty 200 body.
    if shutil.which("tar") is None:
        raise HTTPException(
            status_code=500,
            detail="Export failed: the tar binary is not available",
        )

    home_dir = app.state.workspaces.home_path(workspace_id)
    ws_name = workspace["name"]

    metadata = app.state.workspaces.workspace_metadata(workspace)

    # Estimate uncompressed size for client progress display.
    estimated_size = await _estimate_home_size(home_dir)

    # Stream the tarball using GNU tar piped to stdout. Uses the shared
    # build_export_tar_args (workspaces.py), same as build_workspace_archive.
    # Symlinks are stored as symlinks (not dereferenced).
    _CHUNK_SIZE = 256 * 1024  # 256 KB read chunks

    async def _stream():
        tmpdir = tempfile.mkdtemp()
        try:
            # Write workspace.json to temp dir
            meta_file = os.path.join(tmpdir, "workspace.json")
            with open(meta_file, "w") as f:
                json.dump(metadata, f, indent=2)

            tar_args = app.state.workspaces.build_export_tar_args(
                "-", tmpdir, home_dir
            )

            # stderr goes to a temp file, not DEVNULL: on a tar failure
            # the reason must reach the log, and a pipe would risk
            # deadlocking on a stderr flood (#3101).
            stderr_file = tempfile.TemporaryFile()
            try:
                proc = await asyncio.create_subprocess_exec(
                    *tar_args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=stderr_file,
                )
            except BaseException:
                stderr_file.close()
                raise
            try:
                while True:
                    chunk = await proc.stdout.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
                await proc.wait()
                if proc.returncode != 0:
                    # The status line is already sent, so the body can
                    # only be aborted — raise so the transfer breaks
                    # mid-stream instead of delivering a clean 200 with
                    # a truncated archive the user later fails to
                    # import (#3101).
                    stderr_file.seek(0)
                    stderr_text = stderr_file.read(2000).decode(
                        errors="replace"
                    )
                    logger.error(
                        "Workspace export tar failed (rc=%s) for %s: %s",
                        proc.returncode,
                        workspace_id,
                        stderr_text,
                    )
                    raise RuntimeError(
                        f"Export tar failed with rc={proc.returncode}"
                    )
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                stderr_file.close()
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


async def _stream_upload_to_tempfile(file: UploadFile, max_upload: int) -> str:
    """Stream *file* to a temp file, enforcing the upload size limit.

    Returns the path to the temp file.  Caller is responsible for
    deleting it.
    """
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
    except BaseException:
        os.unlink(tmp.name)
        raise
    return tmp.name


async def _extract_archive_metadata(
    archive_path: str, name: str | None, app
) -> dict:
    """Read workspace.json from the archive and return sanitized metadata."""
    metadata = await _read_archive_metadata(archive_path)
    ws_name = _archive_ws_name(metadata, name)
    _validate_archive_provenance(metadata, app)
    return {
        "name": ws_name,
        "image": _archive_image(metadata.get("image"), app),
        "service_command": metadata.get("service_command"),
        "auto_start": metadata.get("auto_start", False),
        "mounts": _archive_mounts(metadata.get("mounts"), app),
        "env": _sanitize_archive_env(metadata.get("env")),
        "health_check": metadata.get("health_check"),
        "allowed_domains": metadata.get("allowed_domains"),
        "rejected_domains": metadata.get("rejected_domains"),
        "settings": metadata.get("settings"),
        "egress_mode": _archive_egress_mode(metadata.get("egress_mode")),
        "per_handle_home": _archive_per_handle_home(
            metadata.get("per_handle_home")
        ),
        "classification_banner": _archive_banner(
            metadata.get("classification_banner")
        ),
    }


async def _read_archive_metadata(archive_path: str) -> dict:
    """Run the tar extract of workspace.json and parse it; 400 on a missing
    entry or corrupt JSON."""
    result = await asyncio.to_thread(
        subprocess.run,
        ["tar", "xzf", archive_path, "-O", "workspace.json"],
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=400,
            detail="Archive missing workspace.json or is corrupt",
        )
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(
            status_code=400,
            detail="workspace.json is corrupt or contains invalid JSON",
        )


def _archive_ws_name(metadata: dict, name: str | None) -> str:
    """The workspace name: an explicit request name wins, else the
    archive's. Empty, blank, or non-string candidates are a 400
    (#3110)."""
    ws_name = name or metadata.get("name")
    if not isinstance(ws_name, str) or not ws_name.strip():
        raise HTTPException(
            status_code=400,
            detail=("No usable workspace name in archive or request"),
        )
    return ws_name


def _archive_image(image, app) -> str | None:
    """The archived image, dropped when not in this instance's allow
    list."""
    if image and image not in app.state.container_registry.allowed_images:
        return None
    return image


def _archive_mounts(mounts, app):
    """The archived mounts, dropped when they fail this instance's mount
    validation."""
    if mounts and app.state.container_registry.validate_mounts(mounts):
        return None
    return mounts


def _validate_archive_provenance(metadata: dict, app) -> None:
    """Validate provenance: reject archives without instance_id or from a
    different instance."""
    archive_instance_id = metadata.get("instance_id")
    if archive_instance_id is None:
        raise HTTPException(
            status_code=400,
            detail="Archive is missing instance_id",
        )
    local_instance_id = app.state.util.instance_id()
    if archive_instance_id != local_instance_id:
        raise HTTPException(
            status_code=400,
            detail="Archive was exported from a different Klangk instance",
        )


def _sanitize_archive_env(raw_env) -> dict | None:
    """The archived env, stripped of klangk-namespaced and injection-capable
    vars (stale server/container values are re-derived for the new
    container; ``extra_env`` is appended last so a stale value would clobber
    the live injection, #1740)."""
    if not isinstance(raw_env, dict):
        return None
    blocked = {"LD_PRELOAD", "LD_LIBRARY_PATH", "PATH"}
    return {
        k: v
        for k, v in raw_env.items()
        if not k.startswith(
            ("KLANGKD_", "KLANGKWS_", "KLANGKBUILD_", "KLANGK_")
        )
        and k not in blocked
    }


def _archive_egress_mode(egress_mode) -> str:
    """Preserve egress posture across export -> import (#2402); an
    unknown/missing value falls back to the deploy default so a tampered or
    stale archive cannot smuggle in a less restrictive posture."""
    if egress_mode not in EGRESS_MODES:
        return EGRESS_MODE_DEFAULT
    return egress_mode


def _archive_per_handle_home(per_handle_home) -> bool:
    """Preserve the home layout across export -> import (#2722): only an
    explicit bool is honored, anything else imports as per-handle (every
    pre-#2169 workspace was per-user-homed)."""
    if not isinstance(per_handle_home, bool):
        return True
    return per_handle_home


def _archive_banner(classification_banner) -> str | None:
    """Preserve the classification marking across export -> import (#2768);
    a malformed marking drops to the inherit default rather than failing
    the import — the home tree is the payload; the banner is a label."""
    try:
        return normalize_classification_banner(classification_banner)
    except ValueError:
        return None


async def _extract_home_directory(
    archive_path: str, user_id: int, ws_id: int, app
) -> None:
    """Extract the ``home/`` tree from *archive_path* into the workspace home."""
    home_dir = app.state.workspaces.home_path(ws_id)
    home_dir.mkdir(parents=True, exist_ok=True)
    check = await asyncio.to_thread(
        subprocess.run,
        ["tar", "tzf", archive_path, "home/"],
        capture_output=True,
        timeout=30,
    )
    if check.returncode != 0:
        return
    result = await asyncio.to_thread(
        subprocess.run,
        [
            "tar",
            "xzf",
            archive_path,
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
        raise HTTPException(
            status_code=400,
            detail="Failed to extract home directory from archive",
        )


def _imported_settings(meta: dict, app) -> dict:
    """Re-validate imported settings; 400 on any invalid value.

    An archive from this instance is trusted, but the bag may predate a
    schema change or carry a value the current deploy rejects. Validate
    rather than persist blindly. #2560: import is a create path (no
    previous bag) — the archive is user-supplied, editable input, so a
    nix=true opt-in rejects while the feature is off, exactly like POST
    /workspaces.
    """
    try:
        settings = validate_settings(meta.get("settings"))
        validate_nix_optin(settings, nix_available=app.state.nix.available)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Archive settings are invalid: {exc}",
        ) from exc
    return settings


async def _create_from_archive(
    app,
    user: dict,
    meta: dict,
    settings: dict,
    allowed_domains,
    rejected_domains,
) -> dict:
    """Create the workspace row from the sanitized archive metadata;
    a name collision is a 409."""
    try:
        return await app.state.workspaces.create_workspace(
            user["id"],
            meta["name"],
            image=meta["image"],
            service_command=meta["service_command"],
            auto_start=meta["auto_start"],
            mounts=meta["mounts"],
            env=meta["env"],
            health_check=meta["health_check"],
            allowed_domains=allowed_domains,
            rejected_domains=rejected_domains,
            settings=settings,
            egress_mode=meta["egress_mode"],
            # The archive's explicit layout wins over the deploy
            # default (KLANGKD_PER_HANDLE_HOME): import is a creation,
            # but the exported workspace's home tree is laid out for
            # that layout (#2722). Legacy archives without the field
            # already carried True from _extract_archive_metadata.
            per_handle_home=meta["per_handle_home"],
            classification_banner=meta["classification_banner"],
        )
    except SAIntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A workspace named {meta['name']!r} already exists",
        )


@router.post("/workspaces/import")
async def import_workspace(
    file: UploadFile,
    name: str | None = None,
    user: dict = Depends(
        acl.has_permission("create-workspace", workspace_collection_resource)
    ),
    app=Depends(get_app_dep),
):
    """Import a workspace from a .tar.gz archive.

    Creates a new workspace with metadata from workspace.json and
    extracts the home directory from the archive.
    """
    archive_path = await _stream_upload_to_tempfile(
        file, app.state.settings.file_upload_size_max
    )
    ws = None
    try:
        meta = await _extract_archive_metadata(archive_path, name, app)
        allowed_domains = _validate_allowed_domains(
            meta.get("allowed_domains"), app
        )
        rejected_domains = _validate_rejected_domains(
            meta.get("rejected_domains"), app
        )
        settings = _imported_settings(meta, app)

        ws = await _create_from_archive(
            app, user, meta, settings, allowed_domains, rejected_domains
        )

        try:
            await _extract_home_directory(
                archive_path, user["id"], ws["id"], app
            )
        except HTTPException:
            await app.state.workspaces.delete_workspace(ws["id"], user["id"])
            raise

    except HTTPException:
        raise
    except (json.JSONDecodeError, subprocess.TimeoutExpired):
        if ws:
            await app.state.workspaces.delete_workspace(ws["id"], user["id"])
        raise HTTPException(
            status_code=400, detail="Invalid or corrupt archive"
        )
    finally:
        os.unlink(archive_path)

    app.state.sockets.notify_user_workspaces_changed(user["id"])
    return ws


# --- Workspace sharing endpoints ---


@router.get("/workspaces/{workspace_id}/members")
async def get_workspace_members(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("share-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    return await app.state.model.workspaces.get_workspace_members(workspace_id)


class AddMemberRequest(BaseModel):
    email: str


# What a simple share grants (#3101): one block appended atomically by
# the model layer. ``join-workspace`` is the connect gate (#2975) and
# ``monitor-workspace`` keeps health/status frames flowing (#2783).
MEMBER_SHARE_PERMISSIONS = (
    "view",
    "monitor-workspace",
    "join-workspace",
    "terminal",
    "files-view",
    "files-download",
    "files-write",
)

# Group shares mirror the member block minus the monitor grant the
# role-group layout never carried.
GROUP_SHARE_PERMISSIONS = (
    "view",
    "join-workspace",
    "terminal",
    "files-view",
    "files-download",
    "files-write",
)


@router.post("/workspaces/{workspace_id}/members")
async def add_workspace_member(
    workspace_id: str,
    body: AddMemberRequest,
    user: dict = Depends(
        acl.has_permission("share-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    target = await app.state.model.users.get_user_by_identifier(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target["id"] == user["id"]:
        raise HTTPException(
            status_code=400, detail="Cannot share with yourself"
        )
    # Append the member's whole permission block in one transaction —
    # positions allocated inside it, duplicates detected instead of
    # stacking a second block (#3101).
    shared = await app.state.model.acl.add_principal_entries(
        f"/workspaces/{workspace_id}",
        list(MEMBER_SHARE_PERMISSIONS),
        PRINCIPAL_USER,
        user_id=target["id"],
    )
    if not shared:
        raise HTTPException(
            status_code=409,
            detail="This user already has access to the workspace",
        )
    app.state.sockets.notify_user_workspaces_changed(user["id"])
    app.state.sockets.notify_user_workspaces_changed(target["id"])
    return {
        "status": "shared",
        "user_id": target["id"],
        "email": target["email"],
    }


async def _remove_principals(app, workspace_id: str, predicate) -> None:
    """Drop every ACL entry matching *predicate* and renumber the rest.

    Shared by member and group removal (#2553): fetch the workspace's
    entries, filter, re-assign sequential positions, and rewrite atomically.
    """
    resource = f"/workspaces/{workspace_id}"
    entries = await app.state.model.acl.get_acl_entries(resource)
    remaining = [e for e in entries if not predicate(e)]
    for i, entry in enumerate(remaining):
        entry["position"] = i
    await app.state.model.acl.replace_acl_entries(resource, remaining)


@router.delete("/workspaces/{workspace_id}/members/{member_id}")
async def remove_workspace_member(
    workspace_id: str,
    member_id: str,
    user: dict = Depends(
        acl.has_permission("share-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    # Remove all ACL entries for this user on this workspace
    await _remove_principals(
        app,
        workspace_id,
        lambda e: (
            e["principal_type"] == PRINCIPAL_USER and e["user_id"] == member_id
        ),
    )
    app.state.sockets.notify_user_workspaces_changed(user["id"])
    app.state.sockets.notify_user_workspaces_changed(member_id)
    return {"status": "removed"}


ROLE_GROUP_SUFFIXES = ["owners", "coders", "collaborators", "spectators"]


def _group_effective_permissions(
    resource: str, group_id: str, entries: dict[str, list[dict]]
) -> list[str]:
    """Effective permission list for a role group on ``resource``.

    Evaluates the preloaded ACE map in memory with the group as the sole
    principal. ``user_id`` is the empty-string sentinel, never ``None``:
    a malformed user-principal ACE with a NULL ``user_id`` must not
    ``None == None``-match the synthetic principal the way it would match
    no real user (#2987 review). A ``*`` grant therefore expands to the
    whole vocabulary — including the literal ``*`` — which callers can
    collapse for display. Mirrors how ``permissions_for_resources``
    computes a user's effective permissions (#2986).
    """
    principals = {
        "user_id": "",
        "group_ids": [group_id],
        "authenticated": True,
    }
    return [
        p
        for p in ALL_PERMISSIONS
        if acl.check_permission_inmemory(resource, principals, p, entries)
    ]


@router.get("/workspaces/{workspace_id}/roles")
async def get_workspace_roles(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("share-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Return the workspace's role groups with members and grants.

    Each role carries ``permissions``: the group's effective permissions
    on ``/workspaces/{id}``, read from the live ACEs on that node so
    post-seed ACL edits are reflected (#2986). Only the workspace's own
    node is preloaded — deliberately not the ancestor walk
    ``check_permission`` uses: role groups are scope-locked to their own
    workspace node (#2750), so walking up could only misattribute
    inherited everyone/authenticated grants (e.g. the seeded ``Allow view
    Authenticated`` on ``/``) to every bucket.
    """
    resource = f"/workspaces/{workspace_id}"
    entries = await app.state.model.acl.get_acl_entries_map([resource])
    roles = []
    for suffix in ROLE_GROUP_SUFFIXES:
        group_name = f"{suffix}-{workspace_id}"
        group = await app.state.model.users.get_group_by_name(group_name)
        if group is None:
            continue
        members = await app.state.model.users.get_group_members(group["id"])
        roles.append(
            {
                "role": suffix,
                "group_id": group["id"],
                "group_name": group_name,
                "members": [
                    {"id": m["id"], "email": m["email"]} for m in members
                ],
                "permissions": _group_effective_permissions(
                    resource, group["id"], entries
                ),
            }
        )
    return roles


class AddToRoleRequest(BaseModel):
    email: str


# Role-group writes carry the raw power: ``owners-<id>`` holds the ``*``
# wildcard and every group is an ACE principal, so assigning roles is an
# ACL change in effect — a bare ``share`` holder must not be able to mint
# an owner (#2764). Both permissions are required.
ROLE_WRITE_GATE = acl.has_permissions(
    ["share-workspace", "share-advanced"], workspace_resource
)


@router.post("/workspaces/{workspace_id}/roles/{role}")
async def add_to_workspace_role(
    workspace_id: str,
    role: str,
    body: AddToRoleRequest,
    user: dict = Depends(ROLE_WRITE_GATE),
    app=Depends(get_app_dep),
):
    """Add a user to a workspace role group."""
    if role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    group_name = f"{role}-{workspace_id}"
    group = await app.state.model.users.get_group_by_name(group_name)
    if group is None:
        raise HTTPException(status_code=404, detail="Role group not found")
    target = await app.state.model.users.get_user_by_identifier(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    await app.state.model.users.add_user_to_group(target["id"], group["id"])
    app.state.sockets.notify_user_workspaces_changed(user["id"])
    app.state.sockets.notify_user_workspaces_changed(target["id"])
    return {"ok": True}


@router.delete("/workspaces/{workspace_id}/roles/{role}/{member_id}")
async def remove_from_workspace_role(
    workspace_id: str,
    role: str,
    member_id: str,
    user: dict = Depends(ROLE_WRITE_GATE),
    app=Depends(get_app_dep),
):
    """Remove a user from a workspace role group."""
    if role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Invalid role: {role}")
    group_name = f"{role}-{workspace_id}"
    group = await app.state.model.users.get_group_by_name(group_name)
    if group is None:
        raise HTTPException(status_code=404, detail="Role group not found")
    await app.state.model.users.remove_user_from_group(member_id, group["id"])
    app.state.sockets.notify_user_workspaces_changed(user["id"])
    app.state.sockets.notify_user_workspaces_changed(member_id)
    return {"ok": True}


class ChangeRoleRequest(BaseModel):
    email: str
    role: str | None = None  # None = remove from all roles


async def _remove_from_all_roles(app, workspace_id: str, user_id) -> None:
    """Drop the user from every workspace role group."""
    for suffix in ROLE_GROUP_SUFFIXES:
        group_name = f"{suffix}-{workspace_id}"
        group = await app.state.model.users.get_group_by_name(group_name)
        if group is None:
            continue
        await app.state.model.users.remove_user_from_group(
            user_id, group["id"]
        )


async def _add_to_role(app, workspace_id: str, user_id, role: str) -> None:
    """Add the user to a workspace role group; 404 when the group is
    missing."""
    group = await app.state.model.users.get_group_by_name(
        f"{role}-{workspace_id}"
    )
    if group is None:
        raise HTTPException(status_code=404, detail="Role group not found")
    await app.state.model.users.add_user_to_group(user_id, group["id"])


@router.patch("/workspaces/{workspace_id}/roles")
async def change_workspace_role(
    workspace_id: str,
    body: ChangeRoleRequest,
    user: dict = Depends(ROLE_WRITE_GATE),
    app=Depends(get_app_dep),
):
    """Atomically change a user's workspace role.

    If ``role`` is set, removes the user from all other roles and adds
    them to the target role.  If ``role`` is null, removes the user
    from all roles.
    """
    target = await app.state.model.users.get_user_by_identifier(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    if body.role is not None and body.role not in ROLE_GROUP_SUFFIXES:
        raise HTTPException(
            status_code=400, detail=f"Invalid role: {body.role}"
        )

    # Remove from all current roles
    await _remove_from_all_roles(app, workspace_id, target["id"])

    # Add to target role if specified
    if body.role is not None:
        await _add_to_role(app, workspace_id, target["id"], body.role)

    app.state.sockets.notify_user_workspaces_changed(user["id"])
    app.state.sockets.notify_user_workspaces_changed(target["id"])
    return {"ok": True, "email": body.email, "role": body.role}


@router.get("/workspaces/{workspace_id}/groups")
async def get_workspace_groups(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("share-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Get groups with access to this workspace via ACL."""
    resource = f"/workspaces/{workspace_id}"
    entries = await app.state.model.acl.get_acl_entries_resolved(resource)
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
    user: dict = Depends(
        acl.has_permission("share-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Share a workspace with a group (view/join/terminal/files(+dl/ul))."""
    group = await app.state.model.users.get_group_by_id(body.group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    shared = await app.state.model.acl.add_principal_entries(
        f"/workspaces/{workspace_id}",
        list(GROUP_SHARE_PERMISSIONS),
        PRINCIPAL_GROUP,
        group_id=body.group_id,
    )
    if not shared:
        raise HTTPException(
            status_code=409,
            detail="This group already has access to the workspace",
        )
    return {"status": "shared", "group_id": group["id"], "name": group["name"]}


@router.delete("/workspaces/{workspace_id}/groups/{group_id}")
async def remove_workspace_group(
    workspace_id: str,
    group_id: str,
    user: dict = Depends(
        acl.has_permission("share-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Remove all ACL entries for a group on this workspace."""
    await _remove_principals(
        app,
        workspace_id,
        lambda e: (
            e["principal_type"] == PRINCIPAL_GROUP
            and e["group_id"] == group_id
        ),
    )
    return {"status": "removed"}


# --- Workspace ACL endpoints (for workspace owners/admins) ---


@router.get("/workspaces/{workspace_id}/acl")
async def get_workspace_acl(
    workspace_id: str,
    user: dict = Depends(
        acl.has_permission("share-advanced", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Get resolved ACL entries for a workspace.

    Gated on ``share-advanced`` (#2764, renamed #2946): the raw ACE
    list is the advanced editor's view. The simple sharing surface
    (members, group shares) stays on ``share-workspace``; role-group
    writes need ``share-advanced`` too.
    """
    resource = f"/workspaces/{workspace_id}"
    return await app.state.model.acl.get_acl_entries_resolved(resource)


@router.put("/workspaces/{workspace_id}/acl")
async def replace_workspace_acl(
    workspace_id: str,
    entries: list[WorkspaceAclEntry],
    user: dict = Depends(
        acl.has_permission("share-advanced", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Replace all ACL entries for a workspace.

    Gated on ``share-advanced`` (#2764; renamed from ``change-acls``
    by #2946), not ``share-workspace``: rewriting the raw ACE list can
    grant ``*`` and add Deny entries — a power beyond inviting
    collaborators. Owners hold it via their ``*`` wildcard; migration
    0017 backfilled it onto existing ``share`` holders.
    """
    resource = f"/workspaces/{workspace_id}"
    await app.state.model.acl.replace_acl_entries(
        resource, serialize_acl_entries(entries)
    )
    return await app.state.model.acl.get_acl_entries_resolved(resource)


# --- Ownership transfer ---


class TransferOwnershipRequest(BaseModel):
    email: str


@router.post("/workspaces/{workspace_id}/transfer")
async def transfer_workspace_ownership(
    workspace_id: str,
    body: TransferOwnershipRequest,
    user: dict = Depends(
        acl.has_permission("transfer-workspace", workspace_resource)
    ),
    app=Depends(get_app_dep),
):
    """Transfer workspace ownership to another user."""
    target = await app.state.model.users.get_user_by_identifier(body.email)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        ws = await app.state.model.workspaces.transfer_workspace(
            workspace_id, target["id"]
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if ws is None:  # pragma: no cover — ACL check rejects first
        raise HTTPException(status_code=404, detail="Workspace not found")

    app.state.sockets.notify_user_workspaces_changed(user["id"])
    app.state.sockets.notify_user_workspaces_changed(target["id"])
    return ws


# --- User search endpoint ---


@router.get("/users/search")
async def search_users(
    q: str,
    _user: dict = Depends(acl.has_permission("search-users")),
    app=Depends(get_app_dep),
):
    if len(q) < 1:
        raise HTTPException(status_code=400, detail="Query too short")
    return await app.state.model.users.search_users(q)


# --- File endpoints ---
