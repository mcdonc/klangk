# Customizing Klangk

Most deployment customization — branding, legal links, email templates, CA certs, OIDC login hooks — is done at **runtime** via env vars and bind mounts. No custom image build is needed.

**The only reason to build a custom host image is plugins** (Dart UI plugins require a Flutter web rebuild; TypeScript workspace plugins require a workspace image rebuild).

See the [Customizing a Deployment](https://mcdonc.github.io/klangk/deployment/customizing/) documentation for full details.

- `docker-compose.yml` — example runtime configuration (uses the stock image)
- `build.sh` / `build-inner.sh` / `Dockerfile` — plugin-only custom image build
- `plugins.yaml` — plugin list for the build
- `login_hook.py` — example OIDC login hook (bind-mounted at runtime, no rebuild)
