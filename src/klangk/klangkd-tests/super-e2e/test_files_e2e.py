"""File operations against the appliance (#2561).

The files API runs ``podman exec`` inside the nested workspace container
(``klangk/files.py``) — here it is exercised end-to-end through the
published port against a container the appliance itself brought up:
upload → list → read → download → rename → delete.
"""

import uuid


from _ws import connect_workspace


async def test_file_roundtrip(appliance, api, auth):
    headers = auth["headers"]
    resp = api.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": f"super-files-{uuid.uuid4().hex[:8]}"},
    )
    assert resp.status_code == 200, resp.text
    ws_id = resp.json()["id"]
    conn = await connect_workspace(appliance, auth["token"], ws_id)
    marker = f"super-e2e file payload {uuid.uuid4().hex}"
    path = f"/home/klangk/super-{uuid.uuid4().hex[:8]}.txt"
    try:
        # upload
        resp = api.post(
            f"/api/v1/workspaces/{ws_id}/files/upload",
            headers=headers,
            params={"path": path},
            files={"file": ("payload.txt", marker.encode(), "text/plain")},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["path"] == path

        # list: the file appears in its directory
        resp = api.get(
            f"/api/v1/workspaces/{ws_id}/files",
            headers=headers,
            params={"path": "/home/klangk"},
        )
        assert resp.status_code == 200, resp.text
        names = [e["name"] for e in resp.json()]
        assert path.rsplit("/", 1)[1] in names

        # read via the content API
        resp = api.get(
            f"/api/v1/workspaces/{ws_id}/files/content",
            headers=headers,
            params={"path": path},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["content"] == marker

        # download streams the raw bytes
        resp = api.get(
            f"/api/v1/workspaces/{ws_id}/files/download",
            headers=headers,
            params={"path": path},
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == marker.encode()

        # rename
        new_path = path + ".renamed"
        resp = api.post(
            f"/api/v1/workspaces/{ws_id}/files/rename",
            headers=headers,
            json={"old_path": path, "new_path": new_path},
        )
        assert resp.status_code == 200, resp.text

        # delete
        resp = api.delete(
            f"/api/v1/workspaces/{ws_id}/files",
            headers=headers,
            params={"path": new_path},
        )
        assert resp.status_code == 200, resp.text

        # gone: the content API 404s
        resp = api.get(
            f"/api/v1/workspaces/{ws_id}/files/content",
            headers=headers,
            params={"path": new_path},
        )
        assert resp.status_code == 404
    finally:
        await conn.close()
        api.delete(f"/api/v1/workspaces/{ws_id}", headers=headers)
