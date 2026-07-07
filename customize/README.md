# Customizing Klangk

This directory is a **ready-to-adapt template** for a downstream klangk
deployment. Copy it, edit the two files that define your build, and you're done.

Most klangk customization — branding, legal links, email templates, CA certs,
OIDC login hooks — happens at **runtime** via env vars and bind mounts, using
the stock image with no rebuild.

**The only reason to build a custom image is plugins** (Dart UI plugins need a
Flutter web rebuild; TypeScript workspace plugins need a workspace image
rebuild).

See the [Customizing a Deployment](https://mcdonc.github.io/klangk/deployment/customizing/)
docs for full details.

## Directory Layout

```text
customize/
  docker-compose.yml   # Runtime config — all the runtime knobs in one place
  build/
    build.sh           # The build script (run this to build a custom image)
    plugins.yaml       # ← EDIT THIS: the plugin list for your build
  custom/              # Mounted as KLANGK_CUSTOMIZE_DIR at runtime
    oidc/              # OIDC config + login hook
      oidc.yaml        # ← EDIT THIS: your identity-provider config
      login_hook.py    # Example login hook (restricts logins to invited users)
    certs/             # Custom CA certificates
      cacert.pem       # ← EDIT THIS: your private CA cert (example provided)
    branding/          # Logo + assets served at /branding (no Flutter rebuild)
      logo.png         # ← EDIT THIS: your logo (example provided)
    email-templates/   # Jinja2 email template overrides
  data/                # Persistent database/state (bind-mounted, gitignored)
  mount/               # Workspace bind-mount root (bind-mounted, gitignored)
```

## The Two-File Workflow

For a plugin build, you edit exactly two files:

1. **`build/plugins.yaml`** — your plugin list.
2. **`build/build.sh`** — one default to set: `VARIANT` (the build identity
   string surfaced in `version.json` and the debug pane; defaults to `custom`).

Everything else is runtime config in `docker-compose.yml`.

### Building

```bash
cd customize
./build/build.sh

# Pin to a specific klangk release:
KLANGK_REF=v1.0.1 ./build/build.sh

# Set a variant name for your build:
KLANGK_VARIANT="Acme 1.0.0" ./build/build.sh
```

### Runtime

```bash
docker compose up
```

Edit `docker-compose.yml` for branding, product name, CA certs, OIDC, and the
LLM backend. The `custom/` directory is mounted as `KLANGK_CUSTOMIZE_DIR` and
contains `branding/`, `certs/`, `email-templates/`, and `oidc/`.
