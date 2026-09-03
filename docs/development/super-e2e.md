# Super-E2E (host appliance)

The super-E2E suite exercises klangk **end-to-end inside the real Docker
host container** — the appliance self-hosted users run — rather than the
devenv-shell deployment the other e2e suites target. It proves
the shipped image works feature-by-feature: supervisord as PID 1, klangkd
under it, caddy fronting the UDS, and workspaces running inside **nested
rootless podman** with the image's own storage config and uid mappings.

```bash
# once (builds flutter web, the wheel, workspace + sidecar images, the
# host image — tagged klangk-host:latest)
build-host-image

# run the suite (~10 minutes)
test-super-e2e
```

`KLANGK_SUPER_E2E_IMAGE` selects a different image (default
`klangk-host:latest`). Docker and the built host image are required —
the suite fails loudly when either is missing (a silent skip would make
CI green without testing anything, the gap closed).

## What it covers

Everything is **black-box**: the public HTTP API and WebSocket over the
appliance's published browser port, plus `docker exec` as the control
channel for service-state checks (process tree, nested podman, SIGHUP
delivery to klangkd's PID).

| Area                  | Module                                                                                                                                                                                                  |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Boot shape            | `test_boot_e2e.py` — supervisord PID 1, klangkd + caddy children, `/health`, the Flutter bundle served through caddy, supervisord crash-restart of klangkd, WS through the proxy                        |
| Auth                  | `test_auth_e2e.py` — password login, 401s, registration, `/api/v1/config`                                                                                                                               |
| Workspace lifecycle   | `test_workspace_lifecycle_e2e.py` — create → connect (bringup in nested podman) → exec → terminal → stop → start → delete                                                                               |
| Files                 | `test_files_e2e.py` — upload / list / read / download / rename / delete                                                                                                                                 |
| Egress filtering      | `test_egress_e2e.py` — static deny (off-list DNS is NXDOMAIN'd by the sidecar), sidecar bringup inside the appliance, interactive consent (hold → `egress_request` → deny verdict → forged-RST refusal) |
| Shared terminals      | `test_shared_terminal_e2e.py` — share a window, a second user lists + joins it                                                                                                                          |
| Health + idle         | `test_health_idle_e2e.py` — failing health check surfaces its stderr via the status API; the idle sweep stops an abandoned workspace (per-workspace `idle_timeout` override)                            |
| SIGHUP                | `test_sighup_reload_e2e.py` — WS clients closed with 1012 + recycle phases, listener stays up, double-SIGHUP survived, `klangkd.yaml` edit applies after reload                                         |
| Admin + export/import | `test_admin_export_e2e.py` — admin user CRUD; workspace export → import roundtrip                                                                                                                       |

The appliance boots in **password auth** with a seeded admin user, test
mode on, a fast health poll, and a bounded consent hold (see
`_appliance.py`). No monkeypatching and no in-process app: the point is
the deployed artifact.

## When it runs

Not a per-PR gate — the image build is too expensive. The
[super-e2e workflow](https://github.com/mcdonc/klangk/actions/workflows/super-e2e.yml)
runs:

- on demand (`workflow_dispatch`, stock or the self-hosted nix runner),
- on every push to a `release/**` branch, before a release is cut.

Run it locally when you touch anything in the appliance's deployment
shape: `src/containers/host/**`, the wheel packaging, the caddy engine,
or nested-podman behavior. The suite is serial by design (one appliance
per session).
