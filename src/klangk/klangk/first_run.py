"""First-run config generation so a bare ``klangkd`` boots (#1645 / #1607).

When ``klangkd`` is invoked with no ``--config`` and the resolved config file
doesn't exist, this generates a default ``klangkd.yaml`` at that path: a
near-empty template pointing at the solo docs (#1629) with commented examples
for the state transitions (enabling the browser, switching to multiuser).

No ``default_user`` or ``default_password`` is emitted — the admin identity
is derived from the invoking Unix user at runtime (settings default), and a
password is only needed (and fail-fast required) when the operator explicitly
switches to ``auth_modes: password`` / ``both``. The generated file's purpose
is discoverability (``this is where your config lives``) + a quick-reference
for the mode transitions, not carrying any seeded identity.

The resolved config-file path lives under ``KLANGK_CONFIG_DIR`` (default
``$XDG_CONFIG_HOME/klangk``, #1649). ``KLANGK_CONFIG_DIR`` is read from the
env **before** any ``KlangkSettings`` construction — it can't come from
``klangkd.yaml`` because ``klangkd.yaml`` is what we're locating (the
bootstrap rule from #1649).

Constraints:

- **Never overwrite an existing file** — generation is gated on the file's
  absence (the caller checks; the ``open("x")`` mode also refuses a race).
- **Never emit a ``config_dir:`` key** — it would be ignored with a warning
  per #1649 (``klangkd.yaml`` can't relocate the config tree it lives in).
- **No plugin seeding** — the runtime reads a shipped manifest (#1655) and
  the wheel bakes the default set's UIs (#1656); no runtime declaration
  needed.
- **#1622 admin-seeding gate unchanged** — ``seed_default_user`` still
  refuses to re-seed once an admin exists.

Cross-platform note (#1607): the XDG fallback applies on macOS too — we
deliberately do not switch to ``~/Library/Application Support``.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from klangk.settings import _XDG_SUBDIR, _xdg_config_home

# The filename inside ``config_dir``. Renamed from ``klangkd.conf`` in #1654.
_CONFIG_FILENAME = "klangkd.yaml"


def default_config_path() -> str:
    """Return the config-file path a bare ``klangkd`` resolves to.

    ``$KLANGK_CONFIG_DIR/klangkd.yaml`` when the env var is set (the
    operator's explicit override), else ``$XDG_CONFIG_HOME/klangk/klangkd.yaml``
    (XDG fallback to ``~/.config`` — Linux *and* macOS, per #1607).

    Resolved purely from the env (no ``KlangkSettings`` construction) per
    #1649's bootstrap rule: ``klangkd.yaml`` can't relocate the config tree
    it lives in, so the config-tree root has to be computable before we
    locate the file.
    """
    config_dir = os.environ.get("KLANGK_CONFIG_DIR") or os.path.join(
        _xdg_config_home(), _XDG_SUBDIR
    )
    return os.path.join(config_dir, _CONFIG_FILENAME)


def _render_config() -> str:
    """Render the generated ``klangkd.yaml`` body.

    Intentionally a near-empty template — the admin identity is derived at
    runtime from the Unix user (settings default), and a password is only
    required when the operator switches to password mode. The file carries
    discoverability + commented examples for the mode transitions, nothing
    more. Settings not listed here use the in-code defaults.
    """
    timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return f"""# klangkd configuration — generated on first run ({timestamp}).
#
# This file was auto-created because no klangkd.yaml was found at its
# expected location (${{KLANGK_CONFIG_DIR:-$XDG_CONFIG_HOME/klangk}}/klangkd.yaml,
# overridable via KLANGK_CONFIG_DIR). Edit it to customize your deployment.
#
# By default klangkd runs in solo mode: headless (UDS, no browser listener),
# auth_modes=none (loopback trust, no password). The admin identity is
# derived from your Unix user. See the docs for what to do next:
#   https://mcdonc.github.io/klangk/         (full docs index)
#   https://mcdonc.github.io/klangk/features/auth-modes/  (auth modes +
#                                              the first-run seeding tables)
#
# --- Uncomment to change modes, then restart klangkd ---
#
# port: "8997"             # browser/UI port (loopback by default; enables
#                          # the web UI at http://localhost:8997)
# listen: "127.0.0.1"      # browser interface address (rendered when port
#                          # is set; must be loopback unless you override)
# auth_modes: password     # password | oidc | both | none (default: none)
#                          # password/both require default_password (below)
# default_user: admin@example.com   # override the derived Unix-user identity
# default_password: "..."  # required when auth_modes is password/both
# jwt_secret: change-me    # default is an insecure placeholder; mint a real
#                          # secret for any non-local deployment
#
# Full settings reference:
#   https://mcdonc.github.io/klangk/reference/klangkd-config/
"""


def generate_default_config(path: str) -> None:
    """Write a default ``klangkd.yaml`` template at *path*.

    The file is a near-empty template (see :func:`_render_config`): no admin
    identity or password is emitted. The admin row is seeded at runtime
    (derived from the Unix user; null password in ``none``/``oidc`` mode,
    or ``KLANGK_DEFAULT_PASSWORD`` in ``password``/``both`` mode — fail-fast
    if unset). See ``main.seed_default_user``.

    The parent directory is created (0700) if missing. **Does not overwrite**
    — ``open("x")`` mode fails loudly if the file appeared between the
    caller's existence check and now (a concurrent ``klangkd``). We do NOT
    silently clobber: an existing file is the operator's config.

    Does not emit ``config_dir:`` / ``state_dir:`` / ``data_dir:`` keys
    (#1649 — they would be ignored with a warning).
    """
    body = _render_config()
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(path, "x", encoding="utf-8") as f:
        f.write(body)
