# Customizing Klangk

This directory is a **ready-to-adapt template** for a downstream klangk
deployment: a `docker-compose.yml` showcasing runtime configuration, plus
example runtime-customization files under `custom/`.

Most klangk customization — branding, legal links, email templates, CA certs,
OIDC login hooks — happens at **runtime** via env vars and bind mounts, using
the stock image with no rebuild.

**The only reason to build a custom image is features** (Dart UI features need a
Flutter web rebuild; TypeScript workspace features need a workspace image
rebuild). For that, **fork the repo and edit the checked-in `features.yaml`** at
the repository root — see
[Building a Custom Image (Features)](https://mcdonc.github.io/klangk/deployment/customizing/#building-a-custom-image-features)
in the deployment docs.

See the [Customizing a Deployment](https://mcdonc.github.io/klangk/deployment/customizing/)
docs for full details.

## Directory Layout

```text
customize/
  docker-compose.yml   # Runtime config — all the runtime knobs in one place
  custom/              # Mounted as KLANGKD_CUSTOMIZE_DIR at runtime
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

## Runtime

```bash
docker compose up
```

Edit `docker-compose.yml` for branding, product name, CA certs, OIDC, and the
LLM backend. The `custom/` directory is mounted as `KLANGKD_CUSTOMIZE_DIR` and
contains `branding/`, `certs/`, `email-templates/`, and `oidc/`.
