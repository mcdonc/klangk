"""Contract tests for the E2E Gitea harness (#3385).

``scripts/gitea-e2e.sh`` (the ``gitea-e2e`` devenv script) boots a real
nixpkgs Gitea instance for browser-flow E2E testing; the seed
(``scripts/gitea-e2e-seed.py``) provisions the admin account, an API
token, the clone-target repo, and the PKCE-capable public OAuth
application whose redirect targets the fmtk proxy origin. These
grep-style tests pin the wiring so a future edit that silently drops a
piece is loud — in the spirit of ``test_fmtk_harness.py``.
"""

from __future__ import annotations

import stat
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEVENV_NIX = _REPO_ROOT / "devenv.nix"
_UP = _REPO_ROOT / "scripts" / "gitea-e2e.sh"
_SEED = _REPO_ROOT / "scripts" / "gitea-e2e-seed.py"


def test_scripts_exist_and_are_executable():
    """The boot script needs +x and a shebang; the seed runs via
    ``python3`` (the fmtk-seed pattern), so existence suffices."""
    assert _UP.is_file() and _SEED.is_file()
    mode = _UP.stat().st_mode
    assert mode & stat.S_IXUSR, f"{_UP} must be executable"
    assert _UP.read_text().splitlines()[0].startswith("#!"), f"{_UP} needs a shebang"


def test_devenv_wires_the_harness():
    nix = _DEVENV_NIX.read_text()
    assert "scripts.gitea-e2e.exec" in nix, (
        "devenv.nix no longer defines scripts.gitea-e2e.exec"
    )
    assert "scripts/gitea-e2e.sh" in nix, "gitea-e2e must delegate to the script"
    assert "gitea # real Gitea instance for e2e testing" in nix, (
        "the nixpkgs gitea package must stay in packages (the harness "
        "runs the real binary, not a mock)"
    )


def assert_wired(source: str, needles: tuple[str, ...], why: str) -> None:
    for needle in needles:
        assert needle in source, f"{why}: {needle!r} missing"


def test_app_ini_locks_the_instance_down():
    """Fresh-boot posture: installer locked, registration closed, sqlite —
    the instance must never present an open install page or accept signups
    mid-suite. The listener binds 0.0.0.0 like the backend's own egress
    listener: workspace containers reach it through the pasta gateway
    (host.containers.internal), which maps onto the host's non-loopback
    side — a 127.0.0.1 bind is refused from inside the container while
    the browser legs stay on 127.0.0.1 via DOMAIN/ROOT_URL (#3385)."""
    up = _UP.read_text()
    assert_wired(
        up,
        (
            "INSTALL_LOCK = true",  # in [security] (env: GITEA__security__)
            "DB_TYPE = sqlite3",
            "HTTP_ADDR = 0.0.0.0",
            "DISABLE_SSH = true",
            "DISABLE_REGISTRATION = true",
        ),
        "the generated app.ini must lock down the instance",
    )


def test_port_and_state_are_overridable_and_default_off_the_dev_stack():
    """Default port 8993 / state under DEVENV_STATE, both overridable, and
    clear of the dev stack (8997/8995) and fmtk (8998/8996/8124/8125)."""
    up = _UP.read_text()
    assert 'GITEA_PORT="${GITEA_E2E_PORT:-8993}"' in up
    assert 'STATE_DIR="${GITEA_E2E_STATE:-$DEVENV_STATE/gitea}"' in up


def test_default_redirect_targets_the_fmtk_proxy_origin():
    """The SPA callback route lives on the fmtk origin-splitting proxy
    (same-origin frontend), so the OAuth app's default redirect must be
    that origin's /oauth/callback — and per-seed overridable."""
    up = _UP.read_text()
    assert 'DEFAULT_REDIRECT_URI="http://127.0.0.1:8124/"' in up, (
        "the default OAuth redirect must be the fmtk proxy origin ROOT "
        "(the SPA boots at /?code=.. and redirects, #3385)"
    )
    assert "--redirect-uri" in up, "redirect must be overridable per seed"


def test_seed_creates_a_public_oauth_client():
    """The OAuth application must be a PUBLIC client: PKCE fails for
    confidential clients on Gitea >= 1.22 (go-gitea/gitea#33956)."""
    seed = _SEED.read_text()
    assert_wired(
        seed,
        (
            '"confidential_client": False',
            "/api/v1/user/applications/oauth2",
            "generate-access-token",
        ),
        "the seed must register a PKCE-capable public OAuth app",
    )


def test_seed_creates_a_private_clone_target():
    """git only invokes the credential helper for a repo that demands
    auth, so the seed must carry a PRIVATE auto-initialized repo beside
    the public one."""
    seed = _SEED.read_text()
    assert_wired(
        seed,
        (
            'PRIVATE_REPO_NAME = "e2e-private-repo"',
            "ensure_repo(base, token, PRIVATE_REPO_NAME, True)",
        ),
        "the seed must provision the private clone target",
    )


def test_seed_is_idempotent_and_redirect_aware():
    """Re-runs reuse the token (verified live), the repo, and the app —
    and a moved redirect re-registers the app (Gitea matches redirect
    URIs exactly)."""
    seed = _SEED.read_text()
    assert_wired(
        seed,
        (
            "def ensure_token",
            "def ensure_repo",
            "def existing_app",
            'redirect in app.get("redirect_uris", [])',
            "def create_app",
        ),
        "the seed must reconcile rather than duplicate",
    )


def test_stop_targets_only_this_instance():
    """stop must match the unique config path (not every gitea on the
    host) and reset must wipe the state dir after stopping."""
    up = _UP.read_text()
    assert 'pkill -f "gitea --config $APP_INI"' in up, (
        "stop must scope the process match to this instance's config path"
    )
    assert 'rm -rf "$STATE_DIR"' in up, "reset must wipe the state dir"
