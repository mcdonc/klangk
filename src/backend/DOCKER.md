# Dockerizing the klangk daemon

A plan to simplify klangk's build/run process and package the **klangk
daemon** as a self-contained container stack, with no dependency on the
local server except the LLM endpoint.

## 0. Current status

**Phase 1 — backend code migration (aiodocker → podman CLI): DONE and
verified on Linux** (full unit suite `pytest -n auto`: 867 passed, statement
coverage gate 100%, `ruff check`/`format --check` clean). The Linux run
surfaced one residual gap — `ShellProcess.__init__` + `_make_shell_process()`
were uncovered (the rest of the OS glue is `# pragma: no cover`); both are
pure-Python and are now covered by `test_factory_returns_unstarted_shell`.

**Phase 2 — §13.1 (env-parameterize pi→nginx addressing): DONE** (see §8 /
§13 remaining-work list).

**Phase 3 — §6 (daemon image + nginx) and §7 (compose + rootless profile):
DONE; image builds and the backend + nested rootless podman are smoke-tested.
Only the full two-service stack smoke test (§5) remains.** The rootless
Podman-in-Docker recipe is lifted from the working example in
`/home/bryan/src/podman_test`.

New files under `src/daemon/`:
- `Dockerfile` — multi-stage. `web`: Flutter `3.41.9` → prod `flutter build web
  --wasm --release` (dev-only flags + source-map inlining dropped). `runtime`:
  **`debian:trixie-slim`** (matches podman_test's podman 5.x/passt; its python3
  is 3.13, what the tests already run on) + podman/crun/fuse-overlayfs/uidmap/
  passt/slirp4netns/catatonit/git/rsync. Runs as a **non-root `klangk` user**
  with subuid/subgid + the `chmod 4755 newuidmap/newgidmap` fix + `fuse.conf`.
  Backend installed **editable** into a `uv` venv so main.py's
  `frontend/build/web` lookup resolves; web assets copied there;
  `catatonit`→`uvicorn`.
- `nginx.conf.template` — production config from `scripts/nginx.sh`, upstreams
  repointed `127.0.0.1`→`backend`; rendered by nginx:alpine envsubst (needs
  `NGINX_ENVSUBST_OUTPUT_DIR=/etc/nginx`).
- `containers.conf` / `storage.conf` / `registries.conf` — rootless
  fuse-overlayfs (overlay mountopt), crun/cgroupfs/`events_logger=file`,
  `netns=private`, `init_path=catatonit`; `storage.conf` has the §9/B
  `additionalimagestores` hook (commented until the store volume is wired).

New file at repo root:
- `docker-compose.yml` — `nginx` (host-published, `edge`+`internal`) + `backend`
  (`internal` only). Backend carries the rootless profile: `devices: /dev/fuse,
  /dev/net/tun`; `security_opt: seccomp/apparmor/label=disable + systempaths=
  unconfined`; `cap_add: SYS_ADMIN`; `podman info` healthcheck. Volumes:
  `klangk-data` (app DB + homes) and `klangk-podman-storage` (engine layers —
  a *second* volume beyond §4's single one, since engine storage is separate
  plumbing), plus a commented RO `klangk-images` for §9/B. **`KLANGK_LLM_API_KEY`
  /`_BASE_URL` go to nginx only** (§10); the backend gets an explicit
  `environment:` allow-list (no full `env_file`) so the key never enters its
  container env — the backend only ever uses the key's *name* to strip it from
  workspace shells.

Verified: **full `docker build` succeeds** (Flutter wasm compile + plugin stub +
git `klangk-plugin-api` all resolve; image `klangk-daemon`, 696 MB, podman
5.4.2). **Backend boot smoke-tested**: the container serves HTTP 200 on `/`
(Flutter app) and `/docs`, creates the admin user, and `main.py`'s editable
install resolves the web assets at `/app/src/frontend/build/web`. **Rootless
Podman-in-Docker validated** with the compose security profile (`/dev/fuse`,
`/dev/net/tun`, `systempaths=unconfined`, `cap_add: SYS_ADMIN`): `podman info`
reports `overlay`/fuse-overlayfs + `rootless=true`, and a nested
`podman run --rm alpine` pulled and executed successfully.
**§5 stack smoke test — front HALF DONE.** `docker compose up` brings up both
services healthy; through nginx on `:8995`: the Flutter SPA (`GET /` → 200),
`/docs` → 200, the auth round-trip (`POST /auth/login` → 200 + JWT, then
`GET /workspaces` with the token → 200), and **volume persistence** (after
`docker compose down` + `up`, the admin user is not recreated and re-login
succeeds — the DB survived in `klangk-data`). The backend `podman info`
healthcheck passes.

Two nginx-template bugs were found + fixed during this run:
- removed `daemon off;` (the official nginx image already passes
  `-g 'daemon off;'` → duplicate-directive fatal error);
- the LLM upstream was resolved at config-load, so an unresolvable/down LLM host
  crashed the *whole* front (login + UI). Fixed with a `resolver 127.0.0.11`
  (Docker DNS) + variable `proxy_pass` so the LLM (and the hosted-app
  `backend:$port`, which I'd put behind a variable) resolve at request time and
  only 502 when actually called.

**Still not verified (needs the `klangk` workspace image + a real LLM):**
opening a workspace, the interactive terminal, the hosted-app proxy, and real
LLM calls. That's §9/B + the true end-to-end.

Two knobs to confirm at smoke-test time: the Flutter tag `3.41.9` (vs. the dev
toolchain) and `KLANGK_HOST_GATEWAY` (the §8 pi→nginx reachability value).

HOST prereqs for the backend (same as podman_test): `modprobe tun`, and either
the `cap_add: SYS_ADMIN` (kept by default) or relaxed unprivileged-userns
sysctls.

Done in the backend (`src/backend`):

- **`aiodocker` removed entirely** — dropped from
  [pyproject.toml](src/backend/pyproject.toml). Replaced by
  [`klangk_backend/podman.py`](src/backend/klangk_backend/podman.py): a thin
  async wrapper over the `podman` CLI (binary via `KLANGK_PODMAN_BIN`,
  default `podman`). `PodmanError.status` reproduces aiodocker's 404/409
  semantics for the HTTP layer.
- **Migrated call sites:** `container.py` (lifecycle/volumes), `api.py`
  (`/volumes` routes), `dockerexec.py` (binary swap), and **`terminal.py`**
  (rewritten: interactive shell via `podman exec -t -i` over a
  `pty.openpty()` slave in raw mode; resize = set master winsize + send
  `SIGWINCH` to podman; OS glue isolated in `ShellProcess`, `# pragma: no
  cover`; lifecycle logic in `TerminalSession`, unit-tested via an injected
  fake shell).
- **Airgap hardening:** `podman create --pull=never` (use only the local /
  additional image store, never attempt a registry pull); the stale
  `/var/run/docker.sock` bind on workspace containers was **removed**.
- **Tooling:** `ruff` added to deps so it installs into the venv.
- **Pre-existing CLI test hangs fixed** (unrelated to the migration, but they
  block serial test runs): `test_cli.py::TestRunShell::test_stdout_loop_writes_data`
  and `test_cli_integration.py::TestRunShell::test_stdin_loop_broken_pipe`
  patched `select.select` "ready" but let `_run_shell` do a blocking
  `os.read(0,1)` on real stdin. Fixed by also patching
  `klangk_backend.cli.client.os.read`. (Running `pytest -n auto` avoids these
  entirely — xdist detaches worker stdin.)

Verified on Windows/Git-Bash (modules without `termios`): `podman.py` 100%,
`container.py` 0 statement misses, `dockerexec.py` 100%, `api.py` volume
routes covered, `ruff` clean. On Linux: `terminal.py` imports clean + `ruff`
clean. **The full unit suite + coverage gate must be confirmed on Linux**
(see §13) — `terminal.py`/`cli/` import `pty`/`termios` and can't run on
Windows at all.

Decision locked since the original plan: **workspace image store = (B) a
read-only additional image store** (§9), not a live registry.

**Not yet started:** wiring the §9/B `additionalimagestores` store volume +
the offline build/seed of the `klangk` workspace image, the end-to-end smoke
test (which is what first exercises a real `docker build` of §6/§7), and
README/HACKING docs. (§8 env-parameterization is **done**; the §6 daemon image
+ nginx template and the §7 compose stack are **written** but not yet
build-run.)

> Note: the coverage gate is **statement** coverage
> (`--cov-fail-under=100`, no `branch` config in `pyproject.toml`), despite
> CLAUDE.md mentioning "branch coverage". All migrated modules meet the
> statement gate; a handful of pre-existing partial *branches* are not
> enforced.

## 1. Scope & terminology

**The klangk daemon** = the FastAPI/uvicorn backend
(`klangk_backend.main:app`) + its **nginx** front (reverse proxy,
LLM-key-injecting proxy, hosted-app proxy) + the built **Flutter web
assets** it serves.

This is distinct from the **workspace image** (`src/docker/Dockerfile` on
`klangk-base`) — the per-user "pi" agent sandbox the daemon *spawns*. We are
**not** folding the workspace image into the daemon image; we are giving the
daemon its own container engine to launch those sandboxes into.

## 2. Decisions taken

| Topic | Decision |
|---|---|
| Outer runtime | **Docker** runs the compose stack (`docker compose up`). |
| Inner engine | **Rootless Podman**, launching the pi/workspace containers (podman-in-docker). |
| Engine placement | **Merged into the backend container** (Podman is daemonless, so no separate engine service). |
| Service count | **Two services**: `nginx` + `backend`. |
| Network isolation | Docker networks isolate components; only `nginx` is published to the host. |
| Engine API | **Remove `aiodocker`** and drive Podman via the CLI — no socket, no `podman system service`, single process. |
| External deps | **LLM endpoint only** (eventually rolled into the stack too). |

## 3. Why these choices

- **Podman, daemonless.** Unlike `dockerd`, Podman fork-execs `crun` directly,
  so "merge the engine into the backend" is clean rather than an anti-pattern
  (no second daemon behind a supervisor). It also gives the rootless story the
  user is targeting.
- **Merging the engine** collapses the stack to two services *and* eliminates
  the bind-mount path-alignment problem: because Podman shares the backend's
  filesystem, the `home_path` strings the backend computes
  ([workspaces.py:229](src/backend/klangk_backend/workspaces.py#L229)) resolve
  correctly for the pi containers with no shared-volume-at-identical-path trick.
  The `klangk-data` volume mounts into the one backend container.
- **Removing aiodocker** is what lets the backend stay a single process. The
  backend talks to the engine two ways; only one needs a socket:
  - `docker exec -i …` shell-outs
    ([dockerexec.py:27](src/backend/klangk_backend/dockerexec.py#L27)) — fork-exec,
    no socket.
  - **aiodocker** (Docker REST API) in
    [container.py](src/backend/klangk_backend/container.py) /
    [terminal.py](src/backend/klangk_backend/terminal.py) — needs a socket,
    which is the *only* reason `podman system service` would be required.
  Replace aiodocker with `podman` CLI calls and the socket/service disappears
  entirely.

## 4. Target architecture

```
 host :8995 ── docker compose (outer) ───────────────────────┐
   ┌─────────┐  edge net   ┌──────────────────────────────────┐
   │  nginx  │────────────▶│  backend (uvicorn)                │
   └─────────┘  internal   │   + rootless podman ──▶ pi/        │
        ▲                  │     workspace containers          │
        │                  │   klangk-data volume (one mount)  │
   host:8995               └──────────────────────────────────┘
   (only external dep: the LLM endpoint, reached via nginx llm-proxy)
```

**Services**

- **`nginx`** — the only host-published service (`8995`). On `edge` +
  `internal` networks. Reaches the backend at `backend:8997`. Keeps the
  LLM-proxy (injects `KLANGK_LLM_API_KEY`, so pi containers never see the key)
  and the hosted-app proxy.
- **`backend`** — uvicorn + rootless Podman, **not** published to the host;
  only `nginx` can reach it. Mounts the `klangk-data` volume. Carries the
  rootless-nesting profile (see §7).

**Networks** — `edge` (host-facing, nginx only) and `internal` (nginx ↔
backend). The backend is unreachable except via nginx.

**Volume** — a single named `klangk-data` (SQLite DB + workspace homes), mounted
into the backend at `KLANGK_DATA_DIR=/var/lib/klangk/data`.

## 5. Removing aiodocker (the core refactor)

The aiodocker surface is small and bounded. Replace it with a thin async
wrapper module `klangk_backend/podman.py` that shells `podman` via
`asyncio.create_subprocess_exec`, parses `--format json`, and maps non-zero
exits to a small `PodmanError(status, msg)` so the HTTP 404/409 control flow in
[api.py](src/backend/klangk_backend/api.py) survives unchanged.

### Operation inventory

| # | aiodocker call | Where | podman CLI replacement | Risk |
|---|---|---|---|---|
| 1 | `containers.get` + `.show()` | [container.py:297](src/backend/klangk_backend/container.py#L297), [468](src/backend/klangk_backend/container.py#L468) | `podman inspect <id>` → JSON (`.State.Running`, `.Config.Labels`) | low |
| 2 | `containers.create_or_replace(name, config)` + `.start()` | [container.py:446](src/backend/klangk_backend/container.py#L446) | `podman rm -f` (if exists) then `podman create <flags>` + `podman start` | med |
| 3 | `container.delete(force=True)` | [container.py:469](src/backend/klangk_backend/container.py#L469), [615](src/backend/klangk_backend/container.py#L615) | `podman rm -f <id>` | low |
| 4 | `containers.list(filters={label})` | [container.py:568](src/backend/klangk_backend/container.py#L568), [606](src/backend/klangk_backend/container.py#L606) | `podman ps -a --filter label=… --format json` | low |
| 5 | `volumes.get/create/list/delete` + `.show()` | [container.py:379](src/backend/klangk_backend/container.py#L379), [api.py:431-489](src/backend/klangk_backend/api.py#L431) | `podman volume inspect/create/ls/rm` | low |
| 6 | `DockerError.status` (404/409) for control flow | api.py volume routes | parse exit code + stderr → map to 404/409 | med |
| 7 | **`container.exec(tty=True…)` + `Stream`** | [terminal.py:69-107](src/backend/klangk_backend/terminal.py#L69) | `podman exec -t` over a `pty.openpty()` in raw mode; resize via `TIOCSWINSZ` ioctl | **HIGH** |
| 8 | `docker exec -i …` (already CLI) | [dockerexec.py:27](src/backend/klangk_backend/dockerexec.py#L27) | swap binary `docker` → `podman` | trivial |

### Container spec translation (#2)

The `config` dict at
[container.py:403](src/backend/klangk_backend/container.py#L403) becomes
`podman create` flags:

- `Binds` → `-v host:container[:opts]`
- `Tmpfs` → `--tmpfs /tmp:rw,exec,nosuid,size=2g` (etc.)
- `PortBindings` / `ExposedPorts` → `-p host:container`
- `ExtraHosts` → `--add-host host.docker.internal:host-gateway`
  (or Podman's native `host.containers.internal` — pin during the §8 spike)
- DNS config → `--dns …`
- `Env` → `-e KEY=VAL`
- `Labels` → `--label k=v`
- `Init: True` → `--init`
- `OpenStdin` / `AttachStdin` → `-i`
- name → `--name klangk-<instance>-<wsid>`

### The hard part: the interactive terminal (#7)

The original author deliberately moved *off* subprocess to the API exec — see
the comment at [terminal.py:20](src/backend/klangk_backend/terminal.py#L20):

> "avoiding the double-PTY issue that occurs with `docker exec -it` as a
> subprocess (which consumed ESC bytes from arrow key sequences)."

The API exec gives a **single** container-side PTY with bytes flowing raw over
the socket. Naively shelling `podman exec -it` reintroduces a second (local)
PTY in series whose line discipline eats escape sequences — the exact bug they
fixed. The correct single-PTY CLI equivalent:

- `podman exec -t -i <id> …` for the **container-side** PTY (vim/arrow keys
  work),
- local side on a `pty.openpty()` master/slave with the slave in **raw mode**
  (`termios.cfmakeraw`) so the local line discipline passes bytes through
  untouched,
- **resize** done locally via `fcntl.ioctl(master, TIOCSWINSZ, winsize)` — no
  engine round-trip,
- proxy master ↔ websocket via `loop.add_reader` / `os.read` / `os.write`.

This pattern is well-trodden but **must be manually tested** against arrow keys,
vim, tmux, and ctrl-sequences before it is trusted.

### Cost

- **Test rewrite.** Four test files mock aiodocker
  ([test_container.py](src/backend/tests/test_container.py),
  [test_terminal.py](src/backend/tests/test_terminal.py),
  [test_api.py](src/backend/tests/test_api.py),
  [test_wshandler.py](src/backend/tests/test_wshandler.py)). They must switch to
  mocking subprocess / `podman.py`, and CLAUDE.md mandates **100% branch
  coverage** — every error branch (404/409 mapping, PTY failure paths) needs a
  test. Likely larger than the production change.
- **CLI JSON verification.** `podman inspect` / `volume ls` field names are
  Docker-compatible but verify `.State.Running`, `.Config.Labels`, and volume
  `CreatedAt` against the target Podman version rather than assuming.

### Sequencing (two passes)

1. Introduce `podman.py`; migrate the non-streaming calls (#1–6, 8) + their
   tests. Removes most of aiodocker; surface shrinks to one file.
2. Rewrite the terminal PTY (#7) with manual interactive testing. Only then
   drop `aiodocker` from [pyproject.toml](src/backend/pyproject.toml).

## 6. Build simplification (decouple from Nix for shipping)

Add a **multi-stage Dockerfile for the daemon** (`src/daemon/Dockerfile`),
independent of devenv. Keep `devenv.nix` as-is for local dev — this *adds* a
path, it does not remove one.

- **Stage `web`** — `FROM` a pinned Flutter SDK image matching devenv's
  toolchain. Run `stub_dart_plugins.sh` (or `import_dart_plugins.py` if plugins
  are baked), then `flutter build web --wasm --release`. **Drop** the
  dev-only flags (`--source-maps`, `--no-minify-*`, `--no-strip-wasm`) and the
  `inline_sources_in_map.py` step ([flutterbuildweb.sh](scripts/flutterbuildweb.sh)) —
  this removes the heaviest, most fragile part of the build and shrinks the
  assets. Output `build/web`.
- **Stage `runtime`** — `FROM python:3.12-slim`. Install `podman` + `crun` +
  `fuse-overlayfs`, `bash`, `git`, `rsync` (needed by the CLI shell-outs and
  workspace export/import), then `uv pip install` the backend from
  `src/backend`. Copy `build/web` from the `web` stage to the path
  [main.py:141](src/backend/klangk_backend/main.py#L141) expects. `tini` as
  PID 1; `exec uvicorn …` as CMD (same flags as
  [devenv.nix:89](devenv.nix#L89)).

**nginx** stays an off-the-shelf `nginx:alpine`; lift the config out of
[nginx.sh](scripts/nginx.sh) into a templated `nginx.conf` (env-substituted at
start), with `127.0.0.1` → `backend` and the `/hosted/` upstream pointed at the
address resolved by the §8 spike.

## 7. Rootless Podman-in-Docker profile

The backend container runs rootless Podman to launch pi containers. **Done** —
the recipe was lifted from the working `/home/bryan/src/podman_test` example and
split across the image (in-image half) and compose (host-side half):

- **`fuse-overlayfs` + `/dev/fuse`** — driver set in `src/daemon/storage.conf`
  (`overlay` + `mount_program`), `/dev/fuse` passed through in
  `docker-compose.yml` (`devices:`).
- **setuid `newuidmap`/`newgidmap` + `/etc/subuid`/`/etc/subgid`** — the
  `Dockerfile` creates the non-root `klangk` user with both ID ranges and
  `chmod 4755`s the helpers (file-cap xattrs don't survive the overlay mount).
- **caps / devices / userns** — `cap_add: SYS_ADMIN`, `security_opt`
  (`seccomp`/`apparmor`/`label=disable` + `systempaths=unconfined`), and
  `/dev/net/tun` for passt/slirp4netns, all on the backend service.
- **healthcheck** — `podman info` confirms the in-process engine initialises
  (`podman create` of a real container is the §5 smoke test, since it needs the
  workspace image present).

Host prerequisites (same as the example): `sudo modprobe tun`, and unprivileged
user namespaces — covered by the `SYS_ADMIN` cap, or relax the host sysctls and
drop the cap (see the comment block at the top of `docker-compose.yml`).

## 8. Open item — the one spike to run first

**Workspace-container → nginx reachability.** The pi containers must reach the
nginx LLM-proxy and the bridge. Today the backend injects
`KLANGK_LLM_PROXY_URL` / `KLANGK_BRIDGE_URL` pointing at
`host.docker.internal:{nginx_port}`
([container.py:333](src/backend/klangk_backend/container.py#L333),
[355](src/backend/klangk_backend/container.py#L355)).

Under Podman-in-the-backend with isolated networks, validate how a pi container
reaches nginx (likely Podman's native `host.containers.internal`, or an
`--add-host` alias), and pin the exact injected URLs + the nginx `/hosted/`
upstream from the result.

> **Status: DONE.** Both strings (and the workspace container's `--add-host`)
> now derive from `KLANGK_HOST_GATEWAY`
> ([container.py:329](src/backend/klangk_backend/container.py#L329)), default
> `host.docker.internal` — byte-for-byte preserving the devenv/host-process
> behavior. Under the Podman-in-Docker stack, set
> `KLANGK_HOST_GATEWAY=host.containers.internal` (podman's native alias). The
> backend always registers `--add-host <gateway>:host-gateway`, which resolves
> on both Docker and Podman (both honor the `host-gateway` magic value), so the
> name is reachable from inside the pi container either way.

## 9. Workspace image availability

Podman in the backend starts with no `klangk` workspace image, and the target
deployment is **network-restricted** (no registry pull at runtime).

**Decision: (B) a read-only additional image store.** Ship the prebuilt image
store as an RO artifact/volume and register it in the backend's
`storage.conf`:

```
[storage.options]
additionalimagestores = ["/var/lib/klangk-images"]
```

podman reads the `klangk` base image from the RO store; writable container
layers go to the normal rw store. The store is seeded offline in CI
(`podman build`/`pull` the image, export the store dir). Lets the sandbox
image be refreshed by swapping one volume without rebuilding the backend.

**Pull policy is selectable** via `KLANGK_IMAGE_PULL_POLICY`
([container.py `image_pull_policy()`](src/backend/klangk_backend/container.py)),
mapped onto `podman create --pull=<policy>`:

- `never` (**default**) — offline only: read the image from the local store +
  the additional RO store, never contact a registry (fails fast if absent).
  This is the §9/B airgap behavior.
- `missing` — pull from a registry only if the image isn't present locally
  (i.e. registry-with-store-fallback). Use this on a connected host.
- `always` / `newer` — also accepted; check the registry every start.

An unrecognized value logs a warning and falls back to `never`. So the same
image binary supports both the airgapped "offline store" deployment and a
"pull from registry" deployment with one env var — answering the
pull-vs-store question without code changes.

### Populating the image — local, nothing pushed

The `klangk` image is built **locally** by
[scripts/dockerbuild.sh](scripts/dockerbuild.sh) (`docker build -t klangk
src/docker/`); its only upstream dependency is `klangk-base`, which is *pulled*
from public GHCR (a pull of a public image — no credentials, no push). Getting
that locally-built image into the daemon's rootless store has two targets:

1. **Live rootless store load (the simple path, verified).** With the stack
   running, transfer the image into the backend container's rootless store with
   `docker save | podman load`; it persists in the `klangk-podman-storage`
   volume and `podman create --pull=never klangk` resolves the bare short name.
   Wrapped in [scripts/seed-workspace-image.sh](scripts/seed-workspace-image.sh):

   ```
   scripts/dockerbuild.sh             # build locally (pulls public base, no creds)
   docker compose up -d               # stack must be running
   scripts/seed-workspace-image.sh    # docker save -> podman load into the store
   ```

   Verified end-to-end against the running stack: load succeeds and
   `--pull=never <name>` resolves it. (Gotcha confirmed: podman *container*
   `--name`s must start `[a-zA-Z0-9]`, but the *image* short name resolves
   fine — no special `localhost/` re-tag needed.)

2. **Read-only additional image store (§9/B, for immutable / swap-to-refresh
   deployments) — not yet wired.** Build/load the image into a *separate*
   containers-storage root offline, ship that dir as an RO volume mounted at
   `/var/lib/klangk-images`, and uncomment `additionalimagestores` in
   `src/daemon/storage.conf` + the `klangk-images` volume in `docker-compose.yml`.
   Lets the sandbox image be refreshed by swapping one volume without touching
   the backend. Path 1 is sufficient for a single running deployment; this is
   the airgapped-artifact story.

Rejected alternatives: baking the image into the backend image (couples image
updates to backend rebuilds); a live in-stack `registry:2` (more moving parts,
still needs an offline seed).

## 10. Configuration & secrets

- Single `.env` consumed by compose (`env_file:`), mapping the existing
  `KLANGK_*` vars ([.env.example](.env.example)). Set
  `KLANGK_DATA_DIR=/var/lib/klangk/data` and the nginx/backend ports.
- `KLANGK_LLM_API_KEY` lives **only** in nginx's environment — it is injected by
  the proxy and deliberately never reaches pi containers. Preserve this.
- `KLANGK_JWT_SECRET`, default admin creds, SMTP, etc. unchanged.

## 11. Deliverables & order

1. **Spike** the §8 reachability question; pin the proxy/bridge addressing.
2. **aiodocker removal, pass 1** — `podman.py` wrapper + container/volume
   migration (#1–6, 8) + test rewrite.
3. **aiodocker removal, pass 2** — terminal PTY (#7) + manual interactive
   testing; drop `aiodocker` from `pyproject.toml`.
4. Parameterize the two `host.docker.internal:{nginx_port}` strings via env
   (default preserves host-process behavior for devenv).
5. `src/daemon/Dockerfile` (multi-stage) + production `nginx.conf` template.
6. `docker-compose.yml` — `nginx` + `backend`; `edge`/`internal` networks;
   `klangk-data` volume; `.env`; rootless-nesting placeholder block.
7. Workspace-image availability in Podman (registry pull or init build).
8. Smoke test: `docker compose up` → log in at `:8995` → open a workspace →
   confirm terminal, hosted-app proxy, and LLM calls; restart the stack and
   confirm persistence via the volume.
9. Update [README.md](README.md) / [HACKING.md](HACKING.md) with the
   `docker compose up` path alongside the existing devenv flow.

## 12. Trade-offs to keep in view

- The backend container takes the rootless-nesting privileges — the engine is no
  longer isolated from the app process. Acceptable given the simplicity and
  rootless goals.
- Switching the engine from Docker to Podman moves the backend onto Podman's
  CLI/behaviour surface; the `create`-flag translation and the 404/409 error
  mapping are the parts most worth testing.
- The interactive terminal (§5 #7) is the single highest-risk change and gates
  dropping aiodocker entirely.

## 13. Continuing on Linux

Phase 1 (backend code) is done; the rest must be built/verified on Linux with
`podman` installed. This captures the environment gotchas hit during Phase 1.

### Environment

- Develop/test on **Linux** (or WSL). The repo's dev `.venv` is a Linux venv
  (has a `lib64` symlink); **Windows `uv` corrupts it** (`failed to remove
  .venv/lib64`). `terminal.py` and `cli/client.py` import `pty`/`termios`/
  `fcntl`, so those modules and their tests **cannot be imported on Windows**.
- If the venv is missing/broken: `cd src/backend && uv sync` (recreates it;
  now includes `ruff`).

### Running the tests

```bash
cd src/backend
uv run pytest tests -n auto                 # unit suite + 100% statement gate
uv run ruff check && uv run ruff format --check
```

- **Always use `-n auto`** (pytest-xdist). It detaches worker stdin, which is
  how the repo's `test-backend` runs and what avoids the `TestRunShell`
  `os.read(0)` stdin hangs. Serial runs (`pytest` without `-n auto`, attached
  to a tty) hang on those CLI tests — pre-existing, partly fixed at source.
- **`e2e-tests/` are expected to fail** until the `klangk` workspace image is
  present in podman's store: `podman create klangk` hits
  `--pull=never` (or, before that change, an unqualified-registry pull that's
  denied). They need a real LLM too. Run them separately, not as the gate.

### Verifying the terminal rewrite (the one piece written blind)

`terminal.py`'s `ShellProcess` (the `podman exec -t` + PTY glue) is
`# pragma: no cover` and needs **interactive** validation against a real
podman + `klangk` container:

1. Arrow keys / shell history, `vim`, `tmux`, Ctrl-C/Ctrl-D, wide-unicode →
   confirm raw mode passes escape sequences through (no double-PTY mangling).
2. Resize the browser terminal → `tput cols` / vim redraw → confirm the
   `SIGWINCH`-to-podman path actually resizes the container PTY. **This is the
   most likely thing to need adjustment** if podman's tty handling differs
   from the assumption (alternative: drive resize via `podman` differently).
3. `exit` the shell → session closes cleanly (read returns `b""`/EIO).

### Remaining work (was §11 steps 4–9; steps 1–4 are done)

1. ~~**§8 — env-parameterize** the two `host.docker.internal:{nginx_port}`
   strings (LLM-proxy + bridge URLs) and pin pi→nginx reachability.~~ **DONE** —
   `KLANGK_HOST_GATEWAY` env var (default `host.docker.internal`) drives both
   URLs and the `--add-host` alias; set to `host.containers.internal` for the
   podman stack. See §8. Covered by `test_host_gateway_override`.
2. ~~**§6 — daemon `Dockerfile`** (multi-stage) + templated `nginx.conf`.~~
   **WRITTEN** in `src/daemon/` (`Dockerfile`, `nginx.conf.template`,
   `storage.conf`, `containers.conf`); lints clean (`docker build --check`).
   Full `docker build` + runtime still to be exercised by the smoke test (§5).
3. ~~**§7 — `docker-compose.yml`** with the rootless Podman-in-Docker profile.~~
   **WRITTEN** (`docker-compose.yml` at repo root + the rootless plumbing in the
   `runtime` stage / `src/daemon/*.conf`), modeled on `/home/bryan/src/podman_test`.
   `docker compose config` valid. Full build/run is the §5 smoke test.
4. **§9 — image population:** the **live-store load is DONE** —
   [scripts/seed-workspace-image.sh](scripts/seed-workspace-image.sh)
   (`docker save | podman load` into the running backend's rootless store,
   no registry push; verified end-to-end). Pull policy is also selectable
   (`KLANGK_IMAGE_PULL_POLICY`). **Remaining (§9/B):** the read-only
   `additionalimagestores` artifact path for immutable/swap deployments — wire
   the RO `klangk-images` volume + the `storage.conf`/compose entries (both
   stubbed) and an offline build-into-separate-root step.
5. **Smoke test** the stack — **front half done** (login at `:8995`, SPA, API,
   auth, volume persistence all verified; see §0). Remaining: open a workspace →
   terminal + hosted-app proxy + LLM calls (needs the §9/B workspace image + LLM).
6. ~~**Docs** — README/HACKING `docker compose up` path.~~ **DONE** —
   [HACKING.md](HACKING.md) has a "Running with Docker Compose (without Nix)"
   section (services, host prereqs, bring-up, workspace-image seeding, compose
   vs. devenv differences) + the new env vars (`KLANGK_IMAGE_PULL_POLICY`,
   `KLANGK_PODMAN_BIN`, `KLANGK_HOST_GATEWAY`) in the table; [README.md](README.md)
   has a Quick-Start pointer.

### Notable behavior changes from the migration (review when wiring the stack)

- Workspace containers no longer get `/var/run/docker.sock` (removed in Phase 1;
  originally added by commit 9d2d584). **Resolved — no replacement socket
  needed:** a repo-wide audit found nothing in the workspace image or pi agent
  that talks to a container-engine socket (no `docker`/`podman` daemon calls, no
  `DOCKER_HOST`, no dind). The only residual mentions are stale "docker exec"
  comments in `src/docker/entrypoint.sh` / `bash.bashrc` (the backend now
  attaches via `podman exec`) and `scripts/run-pi-container.sh`, a dev-only
  manual-run helper — none bind the socket.
- `podman create --pull=<policy>` defaults to `never` (a missing image fails
  immediately rather than pulling — the airgapped target). Set
  `KLANGK_IMAGE_PULL_POLICY=missing` to pull from a registry when the image is
  absent locally; see §9.
- `KLANGK_PODMAN_BIN` env var overrides the `podman` binary (default `podman`);
  set to `docker` to run the existing behavior on a docker host for comparison.

