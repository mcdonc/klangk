# Custom klangk-host image with plugins baked in.
#
# A custom image is only needed when you have Dart UI plugins (which
# require a Flutter web rebuild) or TypeScript workspace plugins (which
# require a workspace image rebuild).  All other customization —
# branding, CA certs, email templates, OIDC hooks, legal links — is
# handled at runtime via env vars and bind mounts; see the README.
#
# This cannot be built standalone — use ./build.sh which clones klangk,
# fetches plugins, rebuilds Flutter web and the workspace image, then
# builds this Dockerfile.
#
# Usage:
#   ./build.sh
#
FROM klangk-host:latest

# Replace Flutter web build (rebuilt with Dart plugins)
COPY --chown=klangk:klangk web /home/klangk/src/frontend/build/web

# Replace workspace tarball (rebuilt with plugin extensions/tools)
COPY --from=workspace-tar --chown=klangk:klangk workspace.tar /home/klangk/workspace.tar
