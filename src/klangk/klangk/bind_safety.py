"""Bind-safety enforcement for no-auth (``none``) mode.

Moved out of ``main.py`` in the #2738 module split; behavior is
unchanged. The gate refuses to start the browser listener in ``none``
auth mode unless it binds loopback — ``POST /api/v1/auth/local`` freely
issues an admin token in that mode, so the bind is the identity
boundary (#1374).
"""

import ipaddress
import logging

from .exceptions import ConfigurationError

logger = logging.getLogger(__name__)


# Addresses that are safe for no-auth single-user (``none``) mode: only the
# loopback interface is reachable from the host browser and not from other
# machines or from workspace containers (which appear via pasta NAT as the
# host's non-loopback IP). ``0.0.0.0`` / ``::`` bind every interface and are
# NOT loopback. The full IPv4 loopback range (127.0.0.0/8) and IPv6 ``::1``
# are admitted via :func:`ipaddress.is_loopback`; the bare hostname
# ``localhost`` is admitted as a special case (it resolves to loopback but is
# not itself an IP literal). A UNIX socket path is also safe — ``klangkd``
# creates the parent directory with mode 0700, so only the same uid can
# connect (the same trust boundary as loopback). See #1374.
def is_loopback_bind(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def enforce_no_auth_bind_safety(app) -> None:
    """Refuse to start in ``none`` auth mode unless the browser bind is loopback.

    ``KLANGKD_AUTH_MODES=none`` freely issues a token for the seeded default
    user (``POST /api/v1/auth/local``); anyone who can reach that endpoint is
    effectively logged in as admin. In full/browser mode (`KLANGKD_PORT` set),
    the loopback browser bind (`KLANGKD_LISTEN`) is the identity boundary — it
    keeps the endpoint reachable from the operator's own browser but not from
    the network or from workspace containers. Override the gate explicitly
    with ``KLANGKD_ALLOW_INSECURE_NO_AUTH=1`` when you knowingly expose a
    no-auth server (e.g. a throwaway VM on an isolated network). #1374.

    In headless mode (`KLANGKD_PORT` unset) there is no browser listener at
    all — the backend serves only the UDS (same-uid trust boundary), and
    ``/auth/local`` is never exposed over TCP — so the gate is a no-op (#1542).
    """
    if app.state.oidc.auth_modes() != "none":
        return
    # Headless: no browser listener rendered → /auth/local not exposed on TCP.
    if app.state.settings.port is None:
        return
    host = app.state.settings.listen
    if is_loopback_bind(host):
        return
    if app.state.settings.allow_insecure_no_auth.strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        logger.warning(
            "KLANGKD_AUTH_MODES=none with non-loopback bind %r — allowed "
            "because KLANGKD_ALLOW_INSECURE_NO_AUTH=1. Anyone who can reach "
            "this address is effectively logged in as the default admin user.",
            host,
        )
        return
    raise ConfigurationError(
        "Refusing to start: KLANGKD_AUTH_MODES=none but KLANGKD_LISTEN=%r "
        "is not a loopback address. no-auth mode freely issues an admin "
        "token, so it must bind loopback (127.0.0.0/8, ::1, or localhost). "
        "Set KLANGKD_LISTEN=127.0.0.1, or set KLANGKD_ALLOW_INSECURE_NO_AUTH=1 "
        "to override if you understand the risk. See #1374." % host
    )
