"""Print the CSP the fmtk proxy serves the driven app under.

The fmtk stack (``fmtk-up`` / the pytest harness in
``src/frontend/e2e-tests/fmtk``) fronts the flutter dev run with an
origin-splitting caddy. That proxy also serves the app a
Content-Security-Policy so the e2e suites exercise the browser-site
features under a real policy — a harness with no CSP at all let a
``connect-src`` regression (the drag-and-drop upload's ``blob:`` object
URL fetch) ship silently.

The base policy is the production one (``klangk.caddy.csp_policy`` —
``connect-src 'self' blob:`` included) with exactly two fmtk-only
relaxations the dev-server bootstrap needs:

- ``'unsafe-inline'`` in ``script-src`` (and no hash sources) — the
  production policy hashes the built ``index.html``'s inline scripts,
  but the dev server assembles the page at runtime and injects its own
  inline scripts (dwds), which no pre-computed hash covers. Note CSP
  ignores ``'unsafe-inline'`` entirely when hash sources are present,
  so the two postures are mutually exclusive — the fmtk policy keeps
  none of the hashes. Inline-script hashing stays a production posture
  (unit-tested in ``test_caddy.py``, console-checked by the Playwright
  ``csp-console.spec.ts``).
- ``require-trusted-types-for 'script'`` is dropped — dwds's injected
  debugging client (``/dwds/src/injected/client.js``) assigns raw
  ``innerHTML``, which production's TT default policy (``createScriptURL``
  only, by design) blocks, and without that client a debug run never
  boots. Trusted Types stays a production posture.

Everything else stays at production strength — first-party-only
``connect-src``/``script-src`` included, so the dev run loads CanvasKit
from this origin (the app's bootstrap pins ``canvasKitBaseUrl`` to the
relative ``canvaskit/`` and the proxy serves the flutter SDK's copy at
that path; the deployment contract serves every asset from local
services, the flutter CDN included). A policy-gated regression (like
the upload fetch) fails loudly in the e2e suite.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from klangk.caddy import csp_policy

REPO_ROOT = Path(__file__).resolve().parents[1]


def fmtk_csp() -> str:
    """The policy string the fmtk proxy serves (see module docstring)."""
    # csp_policy() hashes <frontend_dir>/index.html. The dev-server
    # page does not exist on disk and dwds injects its own inline
    # scripts, so stage a hash-free placeholder — 'unsafe-inline' above
    # is then the effective (and intended) inline-script posture.
    with tempfile.TemporaryDirectory(prefix="fmtk-csp-") as tmp:
        staging = Path(tmp)
        (staging / "index.html").write_text(
            "<!-- fmtk: hash-free staging for the dev-server policy -->\n"
        )
        policy = csp_policy(staging)
    policy = policy.replace(
        "script-src 'self' 'wasm-unsafe-eval'",
        "script-src 'self' 'wasm-unsafe-eval' 'unsafe-inline'",
        1,
    )
    return policy.replace("; require-trusted-types-for 'script'", "", 1)


if __name__ == "__main__":
    sys.stdout.write(fmtk_csp() + "\n")
