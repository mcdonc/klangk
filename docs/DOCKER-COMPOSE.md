# Running klangk with Docker/Podman Compose

This is an **additive** way to run klangk as a container. It does **not**
replace the devenv flow — `devenv up` still runs uvicorn + nginx as host
processes using the host's own podman, exactly as before. Everything here lives
in new files; no existing entrypoint, script, or backend code is changed.

## What it is

A **single container** that runs the *same* runtime as the devenv flow — the
existing `entrypoint.sh` → `supervisord.conf` → `uvicorn` + `scripts/nginx.sh`
— plus a **rootless Podman** engine so the one container can also spawn the
pi/workspace containers. No Docker socket; no second service.

Keeping nginx, uvicorn, and the podman-published workspace ports in **one
network namespace** is deliberate: it's what lets `nginx.sh`'s
`127.0.0.1:<port>` hosted-app proxy and the `host.containers.internal`
workspace→nginx hop ([container.py](../src/backend/klangk_backend/container.py))
work **unchanged** from the devenv/host-process model. That's why this is one
container reusing the existing entrypoints, rather than a split nginx+backend
stack.

```
 host :8995 ── compose ─────────────────────────────────────┐
   │  klangk-daemon container                                │
   │    supervisord ── nginx (:8995)  ── uvicorn (:8997)      │
   │                      └── rootless podman ──▶ workspace   │
   │                            containers (pi sandboxes)     │
   │    volumes: klangk-data, klangk-podman-storage           │
   └─────────────────────────────────────────────────────────┘
   (only external dependency: the LLM endpoint, via nginx /llm-proxy)
```

## Files (all new / additive)

| File | Purpose |
|---|---|
| `Dockerfile` (repo root) | Self-contained daemon image: builds Flutter web + the venv in-image, reuses the existing entrypoints, adds rootless podman. At the root (engine default name) because podman-compose on Windows won't forward a `dockerfile:` sub-path. |
| `src/containers/host/{containers,storage,registries}.conf` | Rootless-in-a-container podman config (used only by this image). |
| `docker-compose.yml` | Single `klangk` service + the rootless-nesting run profile + volumes. |
| `scripts/compose-seed-workspace.sh` | Build the `klangk` workspace image and load it into the in-container store. |

## Outer engine: Docker (production) vs rootless podman (local)

The intended **production** outer engine is **Docker** (a root daemon). There
the nested recipe works as-is: the daemon container gets a full uid range and
the setuid `newuidmap` has real privilege, so the standard
`/etc/subuid = 100000:65536` lets the in-container podman spawn workspace
containers (the proven podman-in-Docker recipe).

**Verified locally so far** (Windows, where `docker` is a shim to **podman**,
`docker compose` runs **podman-compose**, engine in the `podman-machine` WSL2
VM): the image builds, the stack comes up healthy, `GET :8995/` serves the
Flutter SPA + API, the admin user is created, and login works. The **one piece
that does not work under a rootless outer engine** is the inner (nested) podman:
this container then only has uids `0 + 1..65536` mapped, so a `100000`-based
subuid range points at uids that don't exist and `newuidmap` fails. Spawning
workspaces therefore needs the production Docker host (or a rootless-in-rootless
subuid range that fits `1..65536` and skips uid 1000 — not configured here).

Commands below use `docker` because that's what's on PATH; substitute
`podman` / `podman compose` if you prefer.

## Bring-up

```bash
cp -n .env.example .env        # set KLANGK_LLM_*, KLANGK_JWT_SECRET, admin creds
docker compose build           # builds the daemon image (Flutter + venv + podman)
docker compose up -d
scripts/compose-seed-workspace.sh   # build + load the workspace image
# open http://localhost:8995  and log in as KLANGK_DEFAULT_USER
```

If `KLANGK_DEFAULT_PASSWORD` is unset, a random password is logged on first
start: `docker compose logs klangk | grep -i password`.

## Host prerequisites (the engine's Linux VM)

The rootless-nesting profile in `docker-compose.yml` passes through `/dev/fuse`
and `/dev/net/tun`, adds `cap_add: SYS_ADMIN`, and sets `label=disable`. The VM
that runs the outer engine must therefore provide:

- the **tun** module (Fedora CoreOS, the podman-machine OS, has it; otherwise
  `modprobe tun`);
- **unprivileged user namespaces** permitted (enabled on Fedora CoreOS). On a
  hardened host where `newuidmap` EPERMs, `cap_add: SYS_ADMIN` is the dependable
  lever and is kept on by default.

The masked-`/proc/sys` problem (podman's default `ping_group_range` sysctl
write failing on a read-only `/proc`) is handled **in-image** via
`default_sysctls = []` in `containers.conf`, so no engine-specific
`systempaths=unconfined` / `unmask=ALL` flag is needed.

## Workspace image

The backend creates workspace containers with `--pull=never`, so the `klangk`
image must be in the in-container rootless store.
`scripts/compose-seed-workspace.sh` builds it on the outer engine (pulling the
public `klangk-base`) and streams it in via `save | podman load`; it persists
in the `klangk-podman-storage` volume. Alternatively set
`KLANGK_IMAGE_PULL_POLICY=missing` in `.env` to pull a published workspace image
from a registry instead.

## Persistence

State lives in two named volumes: `klangk-data` (SQLite DB + workspace home
dirs) and `klangk-podman-storage` (engine image/container layers). `docker
compose down` keeps them; `docker compose down -v` removes them.

## Relation to the devenv flow

| | devenv (`devenv up`) | compose (this doc) |
|---|---|---|
| uvicorn + nginx | host processes | one container (same configs) |
| podman | host's podman | rootless, in the container |
| workspace images | host podman store | in-container store (seeded) |
| Flutter web / venv | built by devenv tasks | built inside the image |
| backend / entrypoints | unchanged | unchanged (reused) |
