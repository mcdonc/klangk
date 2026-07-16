"""Container-resource listing routes: workspace images and named volumes."""

import logging

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
)
from pydantic import BaseModel

from .. import (
    auth,
)
from ..podman import PodmanError as PodmanError
from ._common import get_app_dep

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/images")
async def list_images(
    _user: dict = Depends(auth.get_current_user),
    app=Depends(get_app_dep),
):
    return {
        "default": app.state.container_registry.image_name,
        "allowed": sorted(app.state.container_registry.allowed_images),
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
