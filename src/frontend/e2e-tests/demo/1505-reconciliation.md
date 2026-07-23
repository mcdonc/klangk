# #1505 — Demo reconciliation with latest main

Plain-language log of what changed and why. Only lists things that actually
needed changing.

## Backend config the demo pins (run-demo-backend.sh → .demo-env)

The demo backend is isolated from the main repo's backend on a dedicated port
pair + instance + short state dir. `run-demo-backend.sh` writes these into a
managed block at the end of `.demo-env`, then sources `.demo-env` inside the
devenv shell at launch (devenv.nix does not enable dotenv, so `.demo-env` is
not auto-loaded). The values:

- `_KLANGK_INSTANCE_ID=video` — own pid file + container labels, no port clash.
- `KLANGKD_PORT=8998` — uvicorn's TCP port (only referenced for teardown; the
  bind is a UDS, not TCP).
- `KLANGKD_EGRESS_PORT=8996` — the port the browser/CLI hit (the demo's public
  port).
- `KLANGKD_LISTEN=127.0.0.1` — selects the full (browser) proxy template.
- `KLANGKD_STATE_DIR=/tmp/klangk-demo` — **short path** so the UDS
  (`<state_dir>/klangk.sock`) fits under AF_UNIX's 108-byte `sun_path` limit
  (#1531). The worktree-relative path devenv.nix sets is too long for a deep
  worktree. Sourced inside the devenv shell, this wins over devenv.nix.
- `KLANGKD_DEFAULT_USER=admin@plope.com` / `KLANGKD_DEFAULT_PASSWORD=admin` —
  the bootstrap admin `demo-seed.ts` logs in as to create the hero + cast.
- `KLANGKD_AUTH_MODES=both` — the login screen shows the OIDC button above the
  password fields. The login-card click coordinates in `demo-helpers.ts` were
  measured for that layout (the button shifts the fields down ~0.07).
- `KLANGKD_OIDC_CONFIG=<demo>/demo-oidc.yaml` — fake OIDC provider so `both`
  mode boots (the demo never actually authenticates via OIDC).
- `KLANGKD_HOSTING_HOSTNAME=localhost:8996` — hosted-app URLs resolve through
  the proxy on the public port.

## Changes made

### 1. Stale hardcoded worktree path (run-demo-backend.sh, record-cli.sh)

Both scripts had
`WT=/home/chrism/projects/klangk/.worktrees/demo-video-scripts` hardcoded — a
worktree that no longer exists. Replaced with `git rev-parse --show-toplevel`
resolved from the script's own location, so they work from any worktree.

### 2. Launch klangkd directly, not via `devenv processes up` (run-demo-backend.sh)

`run-demo-backend.sh` used to start the backend via
`devenv processes up --detach`, which runs the full task chain under devenv's
process manager. Two problems on current main:

- The process manager spawns its children in a freshly nix-evaluated
  environment that ignores the current shell's exports, so the demo's config
  never reaches klangkd.
- devenv.nix does not enable dotenv, so `.demo-env` is not auto-loaded either.

Switched to launching `klangkd` directly: `nohup devenv shell -- bash -c 'set
-a; . ./.demo-env; set +a; exec python3 -m klangk.launcher --config=none'`.
Sourcing `.demo-env` _inside_ the devenv shell (after devenv's env setup) makes
`.demo-env`'s values win over devenv.nix's `env.` block. Teardown was simplified
(removed the devenv-manager-discovery logic — there's no manager to fight
anymore; just kill the klangkd + nginx procs and whatever holds the ports).

### 3. Short `KLANGKD_STATE_DIR` + bootstrap creds + OIDC config (run-demo-backend.sh)

The managed block now sets everything klangkd needs (it runs `--config=none`,
so env is the only source):

- `KLANGKD_STATE_DIR=/tmp/klangk-demo` — the worktree default overflows the
  UDS path (#1531).
- `KLANGKD_DEFAULT_USER` / `KLANGKD_DEFAULT_PASSWORD` — the seed's bootstrap
  login.
- `KLANGKD_AUTH_MODES=both` + `KLANGKD_OIDC_CONFIG` — see above.

### 4. New `demo-oidc.yaml`

A fake OIDC provider (`demo-sso`) so `both` mode boots. The issuer is never
contacted (every scene logs in with a password). Format is a bare YAML list
of providers (the external-file format `KLANGKD_OIDC_CONFIG` expects), not the
`oidc_providers:`-keyed inline format.

### 5. Pre-existing type errors in collab-choreography.ts (used by scene 08/08b)

- The `tab` geometry object assigned a `terminal2` key not declared in its
  type. Added `terminal2: TabTarget` to the type.
- A `recvUntil` predicate returned `boolean | undefined`. Wrapped in
  `Boolean(...)` to coerce.

### 6. tsconfig module mode too old for seed-demo-pdf.ts

`tsconfig.json` used `module: commonjs`, but `seed-demo-pdf.ts` (run via
`node --experimental-strip-types`) uses `import.meta.url` and top-level
`await`. Changed `commonjs` → `es2022`.

### 7. README port refs (8995 → 8996)

The demo backend's public port is `8996` (the proxy), not `8995` (the main
repo's default). Updated the README's references.

### 8. CLI scenes use UDS transport (record-cli.sh, cli_demo.py)

CLI scenes now connect over the UDS (`/tmp/klangk-demo/klangk.sock`) instead
of the TCP URL. Both listeners are up simultaneously (uvicorn on the UDS,
the proxy on TCP :8996), so CLI and browser scenes share one backend with no
config change between them. `record-cli.sh` exports `KLANGKBUILD_DEMO_SERVER` so
`cli_demo.py` uses the same transport as the prep helpers.

### 9. LLM creds in .demo-env (run-demo-backend.sh)

Added `KLANGKD_LLM_BASE_URL`, `KLANGKD_LLM_API_KEY`, and `KLANGKD_LLM_MODEL` to
the managed `.demo-env` block. The API key uses `cmd:` indirection
(`cmd:cat /run/agenix/zai-authtoken-chrism2`) so klangkd resolves the secret
at boot — the literal token is never stored in `.demo-env`. Values are
single-quoted because `.demo-env` is `source`d by bash: unquoted
`VAR=cmd:cat /path` parses as `VAR=cmd:cat` + execute `/path`.

### 10. KLANGKD_ALLOW_AUTOSTART=1 in .demo-env (run-demo-backend.sh)

Scene 3 (`klangk sandbox`) creates a workspace with `auto_start: true` from the
sandbox config. The server rejects this with 400 unless `KLANGKD_ALLOW_AUTOSTART=1`
is set. Added to the managed `.demo-env` block and the idempotency guard.

### 11. Scene 3 Setup complete timeout (cli_demo.py)

The openclaw `setup.sh` can take longer than the original 180s timeout on first
run. Bumped to 360s.

## Verification

- `tsc --noEmit -p tsconfig.json` passes clean (exit 0) for the whole demo
  directory.
- `run-demo-backend.sh start` → backend boots (the proxy on :8996, UDS at
  `/tmp/klangk-demo/klangk.sock`, `auth=both`, OIDC `demo-sso` loaded).
- `demo-seed.ts --reset` → bootstrap login, hero + cast creation, workspace +
  role grants all succeed.
- `run-demo-backend.sh status` / `stop` work.
