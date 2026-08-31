"""Admin flows + export/import against the appliance (#2561).

Admin user management (list/create/delete) and the workspace
export → import roundtrip, through the public API exactly as an
operator or the shipped UI would drive it.
"""

import io
import tarfile
import uuid


from _ws import connect_workspace


def test_admin_users_crud(api, auth):
    headers = auth["headers"]
    resp = api.get("/api/v1/admin/users?page_size=200", headers=headers)
    assert resp.status_code == 200
    assert any(u["email"] == "admin@example.com" for u in resp.json()["users"])

    email = f"admin-made-{uuid.uuid4().hex[:8]}@example.com"
    resp = api.post(
        "/api/v1/admin/users",
        headers=headers,
        json={"email": email, "password": "made-by-admin"},
    )
    assert resp.status_code == 200, resp.text
    user_id = resp.json().get("id") or resp.json()["user"]["id"]

    # The new user can log in.
    resp = api.post(
        "/api/v1/auth/login",
        json={"identifier": email, "password": "made-by-admin"},
    )
    assert resp.status_code == 200

    # Delete cascades cleanly.
    resp = api.delete(f"/api/v1/admin/users/{user_id}", headers=headers)
    assert resp.status_code == 200


async def test_export_import_roundtrip(appliance, api, auth):
    headers = auth["headers"]
    resp = api.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": f"super-export-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["id"]

    marker = f"exported-{uuid.uuid4().hex}"
    conn = await connect_workspace(appliance, auth["token"], ws_id)
    try:
        # Put a recognizable file in the workspace home.
        resp = api.post(
            f"/api/v1/workspaces/{ws_id}/files/upload",
            headers=headers,
            params={"path": f"/home/klangk/{marker}.txt"},
            files={"file": (f"{marker}.txt", marker.encode(), "text/plain")},
        )
        assert resp.status_code == 200, resp.text
    finally:
        await conn.close()

    # Export: a tar.gz containing workspace.json + home/.
    resp = api.get(f"/api/v1/workspaces/{ws_id}/export", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/gzip"
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tar:
        names = tar.getnames()
    assert "workspace.json" in names, names
    assert any(marker in n for n in names), names

    # Import under a new name.
    new_name = f"super-import-{uuid.uuid4().hex[:8]}"
    resp = api.post(
        "/api/v1/workspaces/import",
        headers=headers,
        params={"name": new_name},
        files={
            "file": (
                "export.tar.gz",
                resp.content,
                "application/gzip",
            )
        },
    )
    assert resp.status_code == 200, resp.text
    imported_id = resp.json()["id"]

    # The imported workspace is visible with the new name.
    resp = api.get("/api/v1/workspaces", headers=headers)
    match = [w for w in resp.json() if w["id"] == imported_id]
    assert match and match[0]["name"] == new_name, match

    api.delete(f"/api/v1/workspaces/{imported_id}", headers=headers)
    api.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
