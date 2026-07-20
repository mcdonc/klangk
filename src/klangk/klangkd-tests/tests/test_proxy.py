"""Unit tests for the proxy config renderer (#1396).

These exercise the pure rendering logic (config generation, into an
``nginx.conf``) without running a real nginx — the runtime ACL enforcement
is covered by the e2e suite (``test_proxy_acl_e2e.py``).
"""

import asyncio
import re
from unittest.mock import Mock

import pytest

import types

from klangk.proxy import (
    ProxyRenderer,
    detect_host_ipv4s,
    tcp_upstream,
    uds_upstream,
)
from _helpers import make_settings
from klangk.settings import KlangkSettings


def _renderer(settings):
    """Wrap settings in a minimal mock app and build a ProxyRenderer (#1469)."""
    return ProxyRenderer(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    )


def _wd(settings):
    """Build a ProxyWatchdog from settings (wrapped in a minimal mock app)."""
    from klangk.proxy import ProxyWatchdog

    return ProxyWatchdog(
        types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    )


class TestUpstreams:
    def test_tcp_upstream(self):
        assert tcp_upstream("127.0.0.1", "8997") == "http://127.0.0.1:8997"

    def test_uds_upstream(self):
        assert uds_upstream("/tmp/sock") == "http://unix:/tmp/sock:"


class TestClientMaxBodySize:
    def test_default_500mb(self):
        s = make_settings({})
        assert _renderer(s).compute_client_max_body_size() == "500m"

    def test_custom(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "10485760"})
        assert _renderer(s).compute_client_max_body_size() == "10m"

    def test_minimum_1m(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "100"})
        assert _renderer(s).compute_client_max_body_size() == "1m"

    def test_garbage_falls_back(self):
        s = make_settings({"KLANGK_FILE_UPLOAD_SIZE_MAX": "not-a-number"})
        assert _renderer(s).compute_client_max_body_size() == "500m"


class TestContainerAcls:
    def test_explicit_subnets(self):
        s = make_settings(
            env={"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24,172.30.0.0/16"}
        )
        acl, deny = _renderer(s).compute_container_acls()
        assert "allow 10.89.0.0/24;" in acl
        assert "allow 172.30.0.0/16;" in acl
        assert "deny all;" in acl
        # 127.0.0.1 NOT implicitly added on explicit override.
        assert "allow 127.0.0.1;" not in acl
        # The browser catch-all guard now references the geo flag
        # ($container_source), keyed on the pre-realip peer — see
        # TestContainerGeo for the source-set assertions.
        assert "if ($container_source) { return 403; }" in deny

    def test_all_loopback_warns(self, caplog):
        s = make_settings({"KLANGK_CONTAINER_SUBNETS": "127.0.0.1"})
        _renderer(s)._container_source_entries()
        assert "no non-loopback" in caplog.text

    def test_auto_detect_or_fallback(self):
        """Without explicit subnets: either host IPs or fallback RFC1918."""
        s = make_settings({})
        acl, deny = _renderer(s).compute_container_acls()
        # Must produce *something* (host IPs or fallback).
        assert "deny all;" in acl
        assert "if ($container_source)" in deny


class TestContainerGeo:
    """The http-scope ``geo`` block that flags container-source peers (#1376,
    #1546).

    The browser catch-all ``location /`` denies requests whose *immediate*
    TCP peer is a container source (pasta NAT) — capping brute-force surface
    — but must NOT deny requests that only *look* like a container source
    after #1560's realip rewrite (a trusted proxy co-located on the host,
    whose forwarded real client is a host IP). So the geo is keyed on
    ``$realip_remote_addr`` (the pre-realip peer), never ``$remote_addr``.
    """

    def test_explicit_subnets_listed_loopback_excluded(self):
        s = make_settings(
            env={"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24,172.30.0.0/16"}
        )
        geo = _renderer(s).compute_container_geo()
        assert "geo $realip_remote_addr $container_source {" in geo
        assert "10.89.0.0/24 1;" in geo
        assert "172.30.0.0/16 1;" in geo
        assert "default 0;" in geo

    def test_loopback_never_flagged(self):
        """Loopback maps to default 0 (allowed) even when listed in the set —
        local browsers keep full UI/API access."""
        s = make_settings(
            env={"KLANGK_CONTAINER_SUBNETS": "127.0.0.1,10.89.0.0/24"}
        )
        geo = _renderer(s).compute_container_geo()
        assert "10.89.0.0/24 1;" in geo
        assert "127.0.0.1 1;" not in geo

    def test_empty_or_all_loopback_still_emits_block(self):
        """Even with no non-loopback sources, the ``$container_source`` variable
        is declared (default 0) so the ``location /`` guard references a
        defined variable."""
        s = make_settings({"KLANGK_CONTAINER_SUBNETS": "127.0.0.1"})
        geo = _renderer(s).compute_container_geo()
        assert "geo $realip_remote_addr $container_source {" in geo
        assert "default 0;" in geo
        # No source lines beyond the default.
        assert geo.count(" 1;") == 0

    def test_uses_realip_remote_addr_not_remote_addr(self):
        """Regression guard (#1546): the geo must key on the *immediate* peer
        (``$realip_remote_addr``), not the realip-rewritten real client
        (``$remote_addr``) — otherwise a co-located trusted proxy whose real
        client is a host IP is denied on every browser request."""
        s = make_settings(env={"KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24"})
        geo = _renderer(s).compute_container_geo()
        assert "$realip_remote_addr" in geo
        assert "$remote_addr $container_source" not in geo

    def test_geo_emitted_in_full_config_at_http_scope(self):
        s = make_settings(
            {
                "KLANGK_PORT": "8997",
                "KLANGK_EGRESS_PORT": "8995",
                "KLANGK_CONTAINER_SUBNETS": "10.89.0.0/24",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # geo block present, before the first server block (http scope).
        geo_pos = conf.index("geo $realip_remote_addr $container_source")
        first_server = conf.index("server {")
        assert geo_pos < first_server
        # The catch-all guard references the geo's variable.
        catch_all = re.search(r"location / \{(.*?)\n    \}", conf, re.DOTALL)
        assert catch_all, "location / not found"
        assert "if ($container_source) { return 403; }" in catch_all.group(1)


class TestDnsResolvers:
    def test_explicit_servers(self):
        s = make_settings({"KLANGK_DNS_SERVERS": "1.2.3.4,5.6.7.8"})
        result = _renderer(s).detect_dns_resolvers()
        assert "1.2.3.4" in result
        assert "5.6.7.8" in result

    def test_ipv6_bracketed(self):
        s = make_settings({"KLANGK_DNS_SERVERS": "::1"})
        assert "[::1]" in _renderer(s).detect_dns_resolvers()

    def test_empty_tokens_skipped(self):
        """Trailing commas / empty entries in KLANGK_DNS_SERVERS are skipped."""
        s = make_settings({"KLANGK_DNS_SERVERS": "1.2.3.4,,5.6.7.8,"})
        result = _renderer(s).detect_dns_resolvers()
        assert "1.2.3.4" in result
        assert "5.6.7.8" in result

    def test_fallback(self):
        s = make_settings({})
        result = _renderer(s).detect_dns_resolvers()
        assert len(result) > 0  # from resolv.conf or 8.8.8.8


class TestRenderConfig:
    def test_basic_structure(self):
        # KLANGK_PORT set ⇒ full/browser template.
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_EGRESS_PORT": "8995"}
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "daemon off;" in conf
        # Browser listener (listen {listen}:{port}) + egress listener.
        assert "listen 127.0.0.1:8997;" in conf
        assert "listen 0.0.0.0:8995;" in conf
        assert "proxy_pass http://127.0.0.1:8997" in conf
        # Core locations present (split across the two server blocks).
        assert "location /api/v1/browser-delegate" in conf
        assert "location = /api/v1/auth/local" in conf
        assert "location /" in conf

    def test_uds_upstream_in_conf(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_EGRESS_PORT": "8995"}
        )
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        assert "proxy_pass http://unix:/tmp/klangk.sock:" in conf

    def test_no_llm_block_without_url(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "llm-proxy" not in conf

    def test_llm_block_with_url(self):
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "llm-proxy" in conf

    def test_llm_api_key_resolved(self):
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": "cmd:printf %s resolved-key",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert 'Authorization "Bearer resolved-key"' in conf
        assert "cmd:" not in conf

    def test_llm_block_preserves_path_bearing_base_url(self):
        """Regression: path-bearing ``llm_base_url`` (z.ai
        ``https://api.z.ai/api/coding/paas/v4``, OpenRouter
        ``https://openrouter.ai/api/v1``, etc.) must round-trip intact —
        the runtime ``set $llm_backend {base_url}/$1`` resolves at request
        time and never structural-validates the URL, so the path survives
        without splitting. Pinning this so a refactor toward the caddy-style
        split (upstream + rewrite) keeps nginx's permissive behavior (#1681)."""
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "https://api.z.ai/api/coding/paas/v4",
                "KLANGK_LLM_API_KEY": "k",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # The full base URL survives in the runtime variable assignment.
        # A client POST to /llm-proxy/chat/completions -> $1="chat/completions"
        # -> upstream https://api.z.ai/api/coding/paas/v4/chat/completions.
        assert (
            "set $llm_backend https://api.z.ai/api/coding/paas/v4/$1;" in conf
        )

    def test_llm_block_streams_request_body_and_disables_ipv6(self):
        """Regression for #1682: the /llm-proxy/ location must stream the
        request body (``proxy_request_buffering off``) so a body larger than
        ``client_body_buffer_size`` never spills to ``client_body_temp_path``
        (EACCES under keep-id userns → 500), and must disable IPv6 upstream
        resolution (``resolver ... ipv6=off``) so hosts without IPv6 egress
        don't log ``Network is unreachable`` per request. Asserted in both
        the headless and full renders."""
        env = {
            "KLANGK_LLM_BASE_URL": "https://api.z.ai/api/coding/paas/v4",
            "KLANGK_LLM_API_KEY": "k",
        }
        # Headless (no KLANGK_PORT).
        s_headless = make_settings(env=env)
        headless = _renderer(s_headless).render_config(
            tcp_upstream("127.0.0.1", "8997")
        )
        # Full/browser mode (KLANGK_PORT set).
        s_full = make_settings(env={**env, "KLANGK_PORT": "8997"})
        full = _renderer(s_full).render_config(
            tcp_upstream("127.0.0.1", "8997")
        )
        for label, conf in (("headless", headless), ("full", full)):
            assert "proxy_request_buffering off;" in conf, label
            assert "resolver" in conf and "ipv6=off" in conf, label

    def test_auth_local_loopback_acl(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # Find the auth/local block.
        import re

        m = re.search(
            r"location = /api/v1/auth/local \{(.*?)\}", conf, re.DOTALL
        )
        assert m, "auth/local block not found"
        block = m.group(1)
        assert "allow 127.0.0.1;" in block
        assert "allow ::1;" in block
        assert "deny all;" in block

    def test_hosted_disabled(self):
        s = make_settings(
            {
                "KLANGK_PORT": "8997",
                "KLANGK_HOSTED_PORTS_PER_WORKSPACE": "0",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location ^~ /hosted/ {" in conf
        assert "return 404;" in conf

    def test_hosted_enabled_default(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location ~ ^/hosted/[^/]+/(?<hosted_port>" in conf

    def test_trust_outer_proxy(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_TRUST_OUTER_PROXY": "1"}
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # When trusting outer proxy, X-Forwarded-* come from client headers.
        assert "http_x_forwarded_proto" in conf
        assert "http_x_forwarded_host" in conf

    def test_no_trust_outer_proxy(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # Default: X-Forwarded-* derived from trusted values.
        assert "proxy_set_header X-Forwarded-Proto $scheme;" in conf
        assert "proxy_set_header X-Forwarded-Host $http_host;" in conf


class TestRealipBlock:
    """The realip module directives so ``$remote_addr`` is the real client (#1558)."""

    def test_default_loopback_trust(self):
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "set_real_ip_from 127.0.0.1;" in conf
        assert "set_real_ip_from ::1;" in conf
        assert "real_ip_header X-Forwarded-For;" in conf
        assert "real_ip_recursive on;" in conf

    def test_custom_cidrs(self):
        s = make_settings(
            {
                "KLANGK_PORT": "8997",
                "KLANGK_TRUSTED_PROXY_CIDRS": "10.100.0.0/24,127.0.0.1",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "set_real_ip_from 10.100.0.0/24;" in conf
        assert "set_real_ip_from 127.0.0.1;" in conf

    def test_suppressed_when_reject_proxy_headers(self):
        s = make_settings(
            {"KLANGK_PORT": "8997", "KLANGK_REJECT_PROXY_HEADERS": "1"}
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "set_real_ip_from" not in conf
        assert "real_ip_header" not in conf

    def test_present_in_headless(self):
        s = make_settings(env={"KLANGK_EGRESS_PORT": "8995"})
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        assert "set_real_ip_from 127.0.0.1;" in conf
        assert "real_ip_header X-Forwarded-For;" in conf

    def test_invalid_entries_skipped(self):
        s = make_settings(
            {
                "KLANGK_PORT": "8997",
                "KLANGK_TRUSTED_PROXY_CIDRS": "127.0.0.1,not-a-cidr,10.0.0.0/8",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "set_real_ip_from 127.0.0.1;" in conf
        assert "set_real_ip_from 10.0.0.0/8;" in conf
        assert "not-a-cidr" not in conf

    def test_empty_and_all_invalid_falls_back_to_loopback(self):
        """Empty tokens are skipped; if every entry is invalid, loopback is used."""
        s = make_settings(
            {
                "KLANGK_PORT": "8997",
                "KLANGK_TRUSTED_PROXY_CIDRS": ",not-valid,",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "set_real_ip_from 127.0.0.1;" in conf
        assert "set_real_ip_from ::1;" in conf
        assert "not-valid" not in conf

    def test_block_at_http_scope(self):
        """The realip directives sit at ``http {}`` scope, outside any server block."""
        s = make_settings({"KLANGK_PORT": "8997"})
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        # set_real_ip_from must appear before the first 'server {' block.
        realip_pos = conf.index("set_real_ip_from")
        first_server = conf.index("server {")
        assert realip_pos < first_server

    def test_file_cmd_resolution_from_yaml(self, tmp_path):
        """file:/cmd: values resolve in the renderer."""
        secret = tmp_path / "llm.key"
        secret.write_text("file-based-key\n")
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_LLM_API_KEY": f"file:{secret}",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert 'Authorization "Bearer file-based-key"' in conf


class TestHeadlessTemplate:
    """``KLANGK_PORT`` unset ⇒ headless template (#1542).

    Headless emits a single container-egress listener on
    ``KLANGK_EGRESS_PORT`` serving the container→backend paths
    (``/llm-proxy``, ``/api/v1/browser-delegate``,
    ``/api/v1/workspaces/post-chat-message`` + their ``auth_request`` gate).
    No browser listener, no UI, no ``/hosted/``, no ``/auth/local``.
    Setting ``KLANGK_PORT`` ⇒ full template; ``KLANGK_AUTH_MODES`` never
    changes which template renders.
    """

    def test_headless_emits_egress_with_llm(self):
        """Headless + LLM ⇒ /llm-proxy + egress paths, no browser surface."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_EGRESS_PORT": "8995",
            }
        )
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        # /llm-proxy container-egress location is present, token-gated.
        assert "location ~ ^/llm-proxy/" in conf
        assert "auth_request /api/v1/auth/verify-workspace-token;" in conf
        # The auth_request subrequest target + 401 page ride along.
        assert "location = /api/v1/auth/verify-workspace-token" in conf
        assert "location @token_auth_failed" in conf
        # The other egress locations are served too.
        assert "/api/v1/browser-delegate" in conf
        assert "post-chat-message" in conf
        # UDS upstream lands in the proxied locations.
        assert "proxy_pass http://unix:/tmp/klangk.sock:" in conf
        # No browser surface whatsoever.
        assert "location / {" not in conf  # no catch-all
        assert "/api/v1/auth/local" not in conf
        assert "/hosted/" not in conf  # no hosted/static UI

    def test_headless_no_llm_still_serves_egress(self):
        """Headless + no LLM ⇒ no /llm-proxy, but browser-delegate /
        post-chat-message + their auth_request infra remain."""
        s = make_settings(env={"KLANGK_EGRESS_PORT": "8995"})
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        assert "location ~ ^/llm-proxy/" not in conf
        # The other egress locations persist (they don't depend on LLM).
        assert "/api/v1/browser-delegate" in conf
        assert "post-chat-message" in conf
        assert "verify-workspace-token" in conf
        assert "@token_auth_failed" in conf
        # Still a valid server block with the container-egress listener.
        assert "listen 0.0.0.0:8995;" in conf
        assert "daemon off;" in conf

    def test_headless_single_container_egress_listener(self):
        """No browser listener: exactly one listen (container-egress), no
        browser catch-all location."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_EGRESS_PORT": "8995",
            }
        )
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        # Exactly one listen directive — the container-egress port.
        assert conf.count("\n    listen ") == 1
        assert "listen 0.0.0.0:8995;" in conf
        # No browser catch-all is served off it.
        assert "location / {" not in conf

    def test_egress_listen_override_flows_into_directive(self):
        """KLANGK_EGRESS_LISTEN pins the egress listener interface (#1542)."""
        s = make_settings(
            env={
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                "KLANGK_EGRESS_PORT": "8995",
                "KLANGK_EGRESS_LISTEN": "192.168.1.5",
            }
        )
        conf = _renderer(s).render_config(uds_upstream("/tmp/klangk.sock"))
        assert "listen 192.168.1.5:8995;" in conf
        assert "listen 0.0.0.0:8995;" not in conf

    def test_port_set_emits_full_template(self):
        """Regression guard: KLANGK_PORT set ⇒ full browser template."""
        s = make_settings(
            env={
                "KLANGK_PORT": "8997",
                "KLANGK_LISTEN": "127.0.0.1",
                "KLANGK_EGRESS_PORT": "8995",
            }
        )
        conf = _renderer(s).render_config(tcp_upstream("127.0.0.1", "8997"))
        assert "location / {" in conf
        assert "/api/v1/auth/local" in conf
        # Two listeners: browser (listen {listen}:{port}) + egress.
        assert "listen 127.0.0.1:8997;" in conf
        assert "listen 0.0.0.0:8995;" in conf

    def test_template_keys_off_port_not_auth(self):
        """AUTH value does not change which template is rendered: unset PORT
        ⇒ headless and set PORT ⇒ full across auth values."""
        for auth in ("none", "password", "both"):
            s_headless = make_settings(
                env={
                    "KLANGK_AUTH_MODES": auth,
                    "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
                    "KLANGK_EGRESS_PORT": "8995",
                }
            )
            headless = _renderer(s_headless).render_config(
                uds_upstream("/tmp/klangk.sock")
            )
            assert "location / {" not in headless
            assert "location ~ ^/llm-proxy/" in headless

            s_full = make_settings(
                env={
                    "KLANGK_PORT": "8997",
                    "KLANGK_AUTH_MODES": auth,
                    "KLANGK_EGRESS_PORT": "8995",
                }
            )
            full = _renderer(s_full).render_config(
                tcp_upstream("127.0.0.1", "8997")
            )
            assert "location / {" in full


class TestFindProxyBin:
    def test_configured(self):
        s = make_settings({"KLANGK_PROXY_BIN": "/custom/nginx"})
        assert _renderer(s).find_proxy_bin() == "/custom/nginx"

    def test_fallback_to_which(self):
        s = make_settings({})
        result = _renderer(s).find_proxy_bin()
        # Either found on PATH or falls back to /usr/sbin/nginx.
        assert len(result) > 0

    def test_fallback_to_usr_sbin(self, monkeypatch):
        """When shutil.which returns None, fall back to /usr/sbin/nginx."""
        import klangk.proxy as proxy_mod

        s = make_settings({})
        monkeypatch.setattr(proxy_mod.shutil, "which", lambda _: None)
        assert _renderer(s).find_proxy_bin() == "/usr/sbin/nginx"


class TestDetectHostIPv4s:
    def test_subprocess_failure_returns_empty(self, monkeypatch):
        """When the ip command fails, returns [] (caller uses fallback)."""
        import klangk.proxy as proxy_mod

        def _raise(*a, **kw):
            raise FileNotFoundError("no ip")

        monkeypatch.setattr(proxy_mod.subprocess, "check_output", _raise)
        assert detect_host_ipv4s() == []


class TestDnsResolversFromResolvConf:
    def test_parses_resolv_conf(self, monkeypatch):
        """When KLANGK_DNS_SERVERS is unset, nameservers come from resolv.conf."""
        import klangk.proxy as proxy_mod

        s = make_settings({})
        content = "nameserver 1.1.1.1\nnameserver ::1\n"
        monkeypatch.setattr(
            proxy_mod.Path,
            "read_text",
            lambda self: content,
        )
        result = _renderer(s).detect_dns_resolvers()
        assert "1.1.1.1" in result
        assert "[::1]" in result

    def test_resolv_conf_read_error(self, monkeypatch):
        """OSError reading resolv.conf -> fall back to 8.8.8.8."""
        import klangk.proxy as proxy_mod

        s = make_settings({})

        def _raise(self):
            raise OSError("no resolv.conf")

        monkeypatch.setattr(proxy_mod.Path, "read_text", _raise)
        assert _renderer(s).detect_dns_resolvers() == "8.8.8.8"


class TestContainerAclFallback:
    def test_fallback_when_no_host_ips(self, monkeypatch):
        """When auto-detect yields nothing, fallback RFC1918 ranges are used."""
        import klangk.proxy as proxy_mod

        s = make_settings({})
        monkeypatch.setattr(proxy_mod, "detect_host_ipv4s", lambda: [])
        acl, _ = _renderer(s).compute_container_acls()
        assert "allow 172.16.0.0/12;" in acl
        assert "allow 10.0.0.0/8;" in acl
        # The fallback non-loopback ranges land in the geo block (the
        # container-source flag), not as inline `deny` lines anymore.
        geo = _renderer(s).compute_container_geo()
        assert "172.16.0.0/12 1;" in geo
        assert "10.0.0.0/8 1;" in geo


class TestWriteConfig:
    def test_writes_file(self, tmp_path):
        s = make_settings({"KLANGK_EGRESS_PORT": "8995"})
        r = _renderer(s)
        conf_path = tmp_path / "nginx.conf"
        text = r.render_config(tcp_upstream("127.0.0.1", "8997"))
        written = r.write_config(tcp_upstream("127.0.0.1", "8997"), conf_path)
        assert conf_path.read_text() == text
        assert written == text


# ---------------------------------------------------------------------------
# klangkd helpers + watchdog no-op paths (#1396)
# ---------------------------------------------------------------------------


class TestKlangkdHelpers:
    def test_state_dir_required_when_home_unset(self, monkeypatch):
        # #1459/#1461/#1644: state_dir now defaults to $XDG_STATE_HOME/klangk
        # when unset, so the fail-fast intent only survives the genuinely
        # unconfigured case — no home path computable ($HOME and
        # $XDG_STATE_HOME both unset).
        from pydantic import ValidationError

        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.delenv("XDG_STATE_HOME", raising=False)
        with pytest.raises(ValidationError) as exc_info:
            KlangkSettings(env={"KLANGK_DATA_DIR": "/tmp/data"})
        assert "KLANGK_STATE_DIR" in str(exc_info.value)

    def test_state_dir_env_sets_value(self):
        s = make_settings({"KLANGK_STATE_DIR": "/custom/state"})
        assert s.state_dir == "/custom/state"

    def test_state_dir_config_file_sets_value(self, tmp_path):
        # Config file provides state_dir; not shadowed by a seeded env value
        # (klangkd no longer mutates os.environ, #1459).
        cfg = tmp_path / "config.yaml"
        cfg.write_text('state_dir: "/from/config"\n')
        s = KlangkSettings(
            env={"KLANGK_DATA_DIR": str(tmp_path)},
            config_file=str(cfg),
        )
        assert s.state_dir == "/from/config"


# The watchdog is gated only by the internal _KLANGK_DISABLE_PROXY kill
# switch (test-only); nginx is owned unconditionally in real runs. Covered
# below.


class TestWatchdogGate:
    """ProxyWatchdog.start() respects the test kill switch; otherwise prepares+spawns."""

    @pytest.mark.asyncio
    async def test_start_noop_when_disabled(self, monkeypatch):
        """No-op when the test-only _KLANGK_DISABLE_PROXY is set."""

        monkeypatch.setenv("_KLANGK_DISABLE_PROXY", "1")
        wd = _wd(make_settings({}))
        await wd.start()
        assert wd._task is None

    @pytest.mark.asyncio
    async def test_start_runs_prepare_when_enabled(
        self, monkeypatch, tmp_path
    ):
        """When not disabled, start() runs _prepare then spawns (a stubbed)
        watchdog. The real nginx spawn is e2e-covered; here _watch is stubbed
        so the orchestration (prepare, set _stopping=False, create_task) is
        unit-tested."""
        from klangk.proxy import ProxyWatchdog

        sock = str(tmp_path / "klangk.sock")
        s = make_settings(
            env={
                "KLANGK_STATE_DIR": str(tmp_path),
                "KLANGK_SOCKET": sock,
                "KLANGK_EGRESS_PORT": "19999",
            }
        )
        monkeypatch.delenv("_KLANGK_DISABLE_PROXY", raising=False)
        monkeypatch.setattr(
            "klangk.proxy.ProxyRenderer.find_proxy_bin",
            lambda self: "/fake/nginx",
        )

        spawned = {}

        async def _fake_watch(self_wd, bin_path, conf_path):
            spawned["bin"] = bin_path
            spawned["conf"] = conf_path

        monkeypatch.setattr(ProxyWatchdog, "_watch", _fake_watch)
        wd = _wd(s)
        await wd.start()
        try:
            assert wd._task is not None
            assert wd._stopping is False
            assert (tmp_path / "nginx.conf").is_file()
            await wd._task
            assert spawned["bin"] == "/fake/nginx"
            assert spawned["conf"] == str(tmp_path / "nginx.conf")
        finally:
            # UDS mode is now per-Util-instance (_wd builds a fresh one each
            # call), so there's no module global to reset (#1503).
            pass


class TestPrepareProxy:
    """ProxyWatchdog._prepare() renders nginx.conf with UDS upstream (#1400)."""

    def test_renders_config_and_returns_paths(self, monkeypatch, tmp_path):

        s = make_settings(
            env={
                "KLANGK_STATE_DIR": str(tmp_path),
                "KLANGK_EGRESS_PORT": "19999",
                "KLANGK_LLM_BASE_URL": "http://127.0.0.1:11434",
            }
        )
        monkeypatch.setattr(
            "klangk.proxy.ProxyRenderer.find_proxy_bin",
            lambda self: "/fake/nginx",
        )
        wd = _wd(s)
        bin_path, conf_path = wd._prepare()
        assert bin_path == "/fake/nginx"
        assert conf_path == str(tmp_path / "nginx.conf")
        assert (tmp_path / "nginx.conf").is_file()
        conf = (tmp_path / "nginx.conf").read_text()
        uds_path = str(tmp_path / "klangk.sock")
        assert f"proxy_pass http://unix:{uds_path}:" in conf


class TestStopWatchdog:
    """ProxyWatchdog.stop() teardown when a proc/task were injected."""

    @pytest.mark.asyncio
    async def test_stops_no_proc_no_task(self):
        """Nothing spawned: just clears state (the no-op path)."""

        wd = _wd(make_settings({}))
        await wd.stop()
        assert wd._proc is None
        assert wd._task is None
        assert wd._stopping is True

    @pytest.mark.asyncio
    async def test_stops_terminates_running_proc(self, monkeypatch):
        """A still-running nginx proc is killed via os.killpg (SIGTERM)."""
        import signal

        killpg_calls = []
        monkeypatch.setattr(
            "os.killpg", lambda pgid, sig: killpg_calls.append((pgid, sig))
        )

        class FakeProc:
            pid = 12345
            returncode = None

            def terminate(self):
                pass  # pragma: no cover

            def kill(self):
                pass  # pragma: no cover

            async def wait(self):
                return 0

        wd = _wd(make_settings({}))
        wd._proc = FakeProc()
        await wd.stop()
        assert killpg_calls == [(12345, signal.SIGTERM)]
        assert wd._proc is None

    @pytest.mark.asyncio
    async def test_stops_falls_back_to_terminate(self, monkeypatch):
        """Falls back to proc.terminate() when killpg raises."""

        terminated = []
        monkeypatch.setattr("os.killpg", Mock(side_effect=ProcessLookupError))

        class FakeProc:
            pid = 12345
            returncode = None

            def terminate(self):
                terminated.append(True)

            def kill(self):
                pass  # pragma: no cover

            async def wait(self):
                return 0

        wd = _wd(make_settings({}))
        wd._proc = FakeProc()
        await wd.stop()
        assert terminated == [True]
        assert wd._proc is None

    @pytest.mark.asyncio
    async def test_stops_cancels_task(self):
        """A watchdog task is cancelled and awaited. Covers the task branch."""

        async def _long_running():
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                raise

        wd = _wd(make_settings({}))
        wd._task = asyncio.create_task(_long_running())
        await wd.stop()
        assert wd._task is None

    @pytest.mark.asyncio
    async def test_stops_kills_on_timeout(self, monkeypatch):
        """If the proc doesn't exit within the timeout, SIGKILL via killpg."""
        import signal

        import klangk.proxy as proxy_mod

        actions = []

        def fake_killpg(pgid, sig):
            actions.append(("killpg", sig))

        monkeypatch.setattr("os.killpg", fake_killpg)

        class HungProc:
            pid = 99999
            returncode = None

            def terminate(self):
                actions.append("terminate")  # pragma: no cover

            def kill(self):
                actions.append("kill")  # pragma: no cover

            async def wait(self):
                await asyncio.sleep(100)
                return 0

        async def _fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(proxy_mod.asyncio, "wait_for", _fake_wait_for)
        wd = _wd(make_settings({}))
        wd._proc = HungProc()
        await wd.stop()
        assert actions == [
            ("killpg", signal.SIGTERM),
            ("killpg", signal.SIGKILL),
        ]
        assert wd._proc is None

    @pytest.mark.asyncio
    async def test_stops_kills_fallback_on_timeout(self, monkeypatch):
        """Falls back to proc.kill() when killpg SIGKILL raises."""
        import signal

        import klangk.proxy as proxy_mod

        actions = []
        call_count = [0]

        def fake_killpg(pgid, sig):
            call_count[0] += 1
            if call_count[0] == 1:
                actions.append(("killpg", sig))
            else:
                raise ProcessLookupError

        monkeypatch.setattr("os.killpg", fake_killpg)

        class HungProc:
            pid = 99999
            returncode = None

            def terminate(self):
                actions.append("terminate")  # pragma: no cover

            def kill(self):
                actions.append("kill")

            async def wait(self):
                await asyncio.sleep(100)
                return 0

        async def _fake_wait_for(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError()

        monkeypatch.setattr(proxy_mod.asyncio, "wait_for", _fake_wait_for)
        wd = _wd(make_settings({}))
        wd._proc = HungProc()
        await wd.stop()
        assert actions == [("killpg", signal.SIGTERM), "kill"]
        assert wd._proc is None
