"""Workspace-created hook: customize every new workspace (and its ACL).

Runs after a workspace is created — on **every** creation path: a fresh
``POST /workspaces``, an import (``POST /workspaces/import``), and a
duplicate. The workspace row, the owner ACE, and the four role groups
are already committed when the hook runs.

Failures here never block or roll back the create: klangkd logs a
WARNING (hook source, workspace id, exception) and returns the
workspace as it was seeded.

Usage (mount the file anywhere, point the env var at it):

    docker run -v ./workspace_created.py:/etc/klangk/workspace_created.py:ro \\
               -e KLANGKD_WORKSPACE_CREATED_HOOK=/etc/klangk/workspace_created.py \\
               ...

The file is loaded directly by path — it does not need to be on
PYTHONPATH, and no image rebuild is needed. To call a function other
than the default ``on_workspace_created``, append ``:func_name`` to the
path. Edit the file and send klangkd a SIGHUP to reload the hook
without a restart.

Both sync and async forms are supported (``def`` or ``async def``); the
async form is required for ACL rewrites, since the ACL model API is
awaitable.
"""

# Role groups seeded on every workspace, for reference (see the example
# below): owners get ``*``; coders/collaborators/spectators get fixed
# permission lists on ``/workspaces/{id}``.
#
#   owners         -> *
#   coders         -> terminal, code-in-isolation, exec-and-sync,
#                     spectate-on-shared-terminals, files,
#                     files-download, files-write
#   collaborators  -> terminal, code-in-isolation, exec-and-sync,
#                     code-in-shared-terminals,
#                     spectate-on-shared-terminals, share-terminals,
#                     files, files-download, files-write
#   spectators     -> terminal, spectate-on-shared-terminals


async def on_workspace_created(workspace, actor):
    """Mutate the new workspace and/or rewrite its ACL.

    ``workspace`` is the workspace's row dict plus two async helpers:
    ``await workspace.acl_entries()`` reads the workspace's ACL
    (resolved like ``GET /api/v1/workspaces/{id}/acl``) and
    ``await workspace.rewrite_acl(entries)`` replaces it wholesale —
    the list order becomes the ACL order. ``actor`` is the creating
    user's row dict (``actor['id']``, ``actor['email']``, ...).

    Attribute edits are made by assigning keys on ``workspace``; they
    are persisted after this function returns, with the normal
    validation applied (e.g. ``egress_mode`` must be one of static /
    interactive / allow). An invalid edit is logged and dropped — the
    create still succeeds.
    """
    # --- Example 1: attribute mutation --------------------------------
    # Force every new workspace into a stricter egress posture (the
    # create-time default is 'interactive'; deployments that want
    # silent deny + record instead set 'static'). Any mutable row
    # field works the same way: name, image, mounts, env,
    # allowed_domains, rejected_domains, settings, per_handle_home, ...
    workspace["egress_mode"] = "static"

    # --- Example 2: ACL rewrite ---------------------------------------
    # Browse-without-download posture: keep the coders and collaborators
    # groups' ``files`` grant (browse/read in the file viewer) but drop
    # ``files-download`` and ``files-write`` (edit/uncomment to match your
    # deployment's stance). Entries whose ``principal`` is the group name
    # identify the role groups — they are named ``<role>-<workspace id>``.
    entries = await workspace.acl_entries()
    kept = [
        entry
        for entry in entries
        if not (
            entry["principal_type"] == 2  # PRINCIPAL_GROUP
            and entry["principal"].startswith(("coders-", "collaborators-"))
            and entry["permission"] in ("files-download", "files-write")
        )
    ]
    # Adding entries works the same way — append a dict with the
    # action / principal_type / permission and the principal field,
    # e.g. granting a user 'view':
    #
    #     kept.append({
    #         "action": 1,              # ACTION_ALLOW
    #         "principal_type": 1,      # PRINCIPAL_USER
    #         "permission": "view",
    #         "user_id": actor["id"],
    #     })
    #
    # Caution: rewrite_acl replaces the WHOLE list — keep the entry
    # granting the owner access (the filter above does), or every new
    # workspace starts fully locked out.
    await workspace.rewrite_acl(kept)
