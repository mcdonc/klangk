"""Engine-agnostic parity tests: nginx and Caddy renderers make the same
structural decisions (#1559 Phase 2).

The two proxy renderers (:mod:`klangk.proxy` / nginx, :mod:`klangk.caddy` /
Caddy) are independently unit-tested to 100% in ``test_proxy.py`` and
``test_caddy.py``. During the migration window both engines ship together
(until the Phase 4 cutover, #1634), so a change to one renderer's
**structural** logic must be mirrored in the other — otherwise the two
silently diverge and the runtime parity the e2e suite proves
(``test_caddy_*_e2e.py``) is built on configs that no longer match.

This file asserts the **shape-agnostic** invariants: given identical
settings, both renderers emit configs with the same number of listeners,
the same LLM-block gating, the same body-size directive, the same auth
gate, the same ``/auth/local`` and ``/hosted/`` blocks, and the same
headless-vs-full template selection. Engine-specific *string* assertions
(``set_real_ip_from`` vs ``trusted_proxies static``, ``geo`` vs
``remote_ip``, exact ``proxy_pass`` vs ``reverse_proxy`` forms) stay in
the per-engine suites — only the structural decisions are paired here.

The e2e suites prove these structures **behave** identically at runtime;
this file proves they are **generated** identically, catching a one-sided
refactor before it reaches CI's e2e job.
"""

import os
import re
import tempfile
import types

import pytest

from klangk.caddy import CaddyRenderer
from klangk.proxy import ProxyRenderer
from klangk.settings import KlangkSettings


def _nginx_conf(settings):
    """Render the nginx config for a TCP upstream (engine adapter)."""
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    return ProxyRenderer(app).render_config("http://127.0.0.1:19998")


def _caddy_conf(settings):
    """Render the Caddyfile for a TCP upstream (engine adapter).

    The admin_socket arg is structurally irrelevant to these parity checks
    (it only re-declares the admin UDS in the global block); a throwaway
    path keeps the Caddyfile valid.
    """
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    admin = os.path.join(tempfile.gettempdir(), "parity-admin.sock")
    return CaddyRenderer(app).render_config("127.0.0.1:19998", admin)


# Engine adapters parametrize every test: each runs against both renderers
# and the test asserts the two produce the SAME structural decision.
ENGINES = pytest.mark.parametrize(
    "render", [_nginx_conf, _caddy_conf], ids=["nginx", "caddy"]
)


def _settings(**extra):
    """Build settings with a stable base (both dirs, egress, container set)."""
    d = tempfile.mkdtemp(prefix="klangk-parity-")
    env = {
        "KLANGK_DATA_DIR": d,
        "KLANGK_STATE_DIR": d,
        "KLANGK_EGRESS_PORT": "19999",
        "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24",
    }
    env.update(extra)
    return KlangkSettings(env=env)


# Each feature is a pair: (predicate, description). A predicate takes a
# config string and returns the feature value (count or bool). Both engines
# share the same predicate EXCEPT where the marker token differs
# (``server {`` vs ``http://:<port> {`` for listeners); those use
# engine-aware predicates passed as a dict keyed by engine id.


def _nginx_listener_count(conf):
    return conf.count("server {")


def _caddy_listener_count(conf):
    # Site blocks: ``http://:<port> {`` (one per listener).
    return len(re.findall(r"http://:\d+ \{", conf))


class TestStructuralParity:
    """Both renderers emit the same listener count for each template mode."""

    @pytest.mark.parametrize(
        "render,count_fn",
        [
            (_nginx_conf, _nginx_listener_count),
            (_caddy_conf, _caddy_listener_count),
        ],
        ids=["nginx", "caddy"],
    )
    def test_full_mode_two_listeners(self, render, count_fn):
        """KLANGK_PORT set ⇒ browser + egress = 2 listeners (both engines)."""
        conf = render(_settings(KLANGK_PORT="19998"))
        assert count_fn(conf) == 2

    @pytest.mark.parametrize(
        "render,count_fn",
        [
            (_nginx_conf, _nginx_listener_count),
            (_caddy_conf, _caddy_listener_count),
        ],
        ids=["nginx", "caddy"],
    )
    def test_headless_mode_one_listener(self, render, count_fn):
        """KLANGK_PORT unset ⇒ egress only = 1 listener (both engines)."""
        conf = render(_settings())  # no KLANGK_PORT
        assert count_fn(conf) == 1


class TestFeatureParity:
    """Both renderers gate the same optional blocks on the same settings.

    These use engine-agnostic substrings (``/llm-proxy/``, ``auth/local``,
    ``/hosted/``) that appear identically in both configs, plus an
    engine-aware auth-gate token (``auth_request`` vs ``forward_auth``).
    """

    @ENGINES
    def test_llm_block_present_when_url_set(self, render):
        conf = render(
            _settings(
                KLANGK_LLM_BASE_URL="http://127.0.0.1:11434",
                KLANGK_LLM_API_KEY="k",
            )
        )
        assert "/llm-proxy/" in conf

    @ENGINES
    def test_llm_block_absent_when_url_unset(self, render):
        conf = render(_settings())
        assert "/llm-proxy/" not in conf

    @ENGINES
    def test_hosted_block_present_in_full_mode(self, render):
        conf = render(_settings(KLANGK_PORT="19998"))
        assert "/hosted/" in conf

    @ENGINES
    def test_auth_local_block_present_in_full_mode(self, render):
        conf = render(_settings(KLANGK_PORT="19998"))
        assert "auth/local" in conf

    @ENGINES
    def test_body_size_directive_present(self, render):
        conf = render(_settings(KLANGK_PORT="19998"))
        # nginx: client_max_body_size; Caddy: max_size.
        assert "client_max_body_size" in conf or "max_size" in conf

    @pytest.mark.parametrize(
        "render,token",
        [(_nginx_conf, "auth_request"), (_caddy_conf, "forward_auth")],
        ids=["nginx", "caddy"],
    )
    def test_auth_gate_present(self, render, token):
        """The token-gate directive (auth_request / forward_auth) is present."""
        conf = render(_settings(KLANGK_PORT="19998"))
        assert token in conf


class TestHostedCapParity:
    """KLANGK_HOSTED_PORTS_PER_WORKSPACE=0 disables /hosted/ in both engines."""

    @ENGINES
    def test_hosted_disabled_when_cap_zero(self, render):
        conf = render(
            _settings(
                KLANGK_PORT="19998", KLANGK_HOSTED_PORTS_PER_WORKSPACE="0"
            )
        )
        # Both engines collapse /hosted/ to a 404 catch when the cap is 0;
        # the proxy locations disappear. nginx: ``return 404``; Caddy:
        # ``respond 404``.
        assert "return 404" in conf or "respond 404" in conf
        assert "?<hosted_port>" not in conf  # nginx capture gone
        # The Caddy regex capture (path_regexp hosted) is also gone.
        assert "path_regexp hosted" not in conf
