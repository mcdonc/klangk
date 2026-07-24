"""Tests for the transport resolver (#1399)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from klangk.cli.transport import (
    ServerTransport,
    http_request,
    http_stream,
    is_valid_server_spec,
    resolve_transport,
    ws_connect,
)


class TestIsValidServerSpec:
    def test_valid_specs(self):
        assert is_valid_server_spec("https://host:8995")
        assert is_valid_server_spec("http://host")
        assert is_valid_server_spec("/run/klangk.sock")

    def test_invalid_specs(self):
        assert not is_valid_server_spec("sdfsdf")
        assert not is_valid_server_spec("relative/path")
        assert not is_valid_server_spec("host:8995")  # no scheme


class TestResolveTransport:
    def test_http_url(self):
        t = resolve_transport("http://localhost:8995")
        assert t == ServerTransport(
            is_uds=False,
            uds_path=None,
            base_url="http://localhost:8995",
            ws_uri="ws://localhost:8995/ws",
            server_spec="http://localhost:8995",
        )

    def test_https_url(self):
        t = resolve_transport("https://example.com")
        assert t == ServerTransport(
            is_uds=False,
            uds_path=None,
            base_url="https://example.com",
            ws_uri="wss://example.com/ws",
            server_spec="https://example.com",
        )

    def test_absolute_socket_path(self):
        t = resolve_transport("/tmp/klangk.sock")
        assert t == ServerTransport(
            is_uds=True,
            uds_path="/tmp/klangk.sock",
            base_url="http://localhost",
            ws_uri="ws://localhost/ws",
            server_spec="/tmp/klangk.sock",
        )

    def test_relative_path_raises(self):
        with pytest.raises(ValueError, match="socket path must be absolute"):
            resolve_transport("klangk.sock")

    def test_bare_hostname_raises(self):
        with pytest.raises(ValueError, match="socket path must be absolute"):
            resolve_transport("example.com")


class TestHttpRequest:
    def test_tcp_delegates_to_httpx_request(self):
        mock_resp = MagicMock()
        with patch(
            "klangk.cli.transport.httpx.request", return_value=mock_resp
        ):
            resp = http_request(
                "http://localhost:8995", "GET", "/api/v1/config", timeout=5.0
            )
        assert resp is mock_resp

    def test_uds_uses_client_transport(self):
        mock_resp = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.request.return_value = mock_resp

        with (
            patch(
                "klangk.cli.transport.httpx.HTTPTransport"
            ) as mock_transport,
            patch(
                "klangk.cli.transport.httpx.Client", return_value=mock_client
            ),
        ):
            resp = http_request(
                "/tmp/klangk.sock",
                "POST",
                "/api/v1/auth/login",
                json={"identifier": "a"},
            )

        mock_transport.assert_called_once_with(uds="/tmp/klangk.sock")
        mock_client.request.assert_called_once_with(
            "POST", "/api/v1/auth/login", json={"identifier": "a"}
        )
        assert resp is mock_resp


class TestHttpStream:
    def test_tcp_delegates_to_httpx_stream(self):
        mock_cm = MagicMock()
        with patch("klangk.cli.transport.httpx.stream", return_value=mock_cm):
            result = http_stream(
                "http://localhost:8995",
                "GET",
                "/api/v1/workspaces/ws1/export",
                timeout=300.0,
            )
        assert result is mock_cm

    def test_uds_uses_client_stream(self):
        mock_resp = MagicMock()
        mock_stream_cm = MagicMock()
        mock_stream_cm.__enter__ = MagicMock(return_value=mock_resp)
        mock_stream_cm.__exit__ = MagicMock(return_value=False)
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.stream.return_value = mock_stream_cm

        with (
            patch("klangk.cli.transport.httpx.HTTPTransport"),
            patch(
                "klangk.cli.transport.httpx.Client", return_value=mock_client
            ),
        ):
            with http_stream(
                "/tmp/klangk.sock",
                "GET",
                "/api/v1/export",
                timeout=300.0,
            ) as resp:
                assert resp is mock_resp

        mock_client.stream.assert_called_once_with(
            "GET", "/api/v1/export", timeout=300.0
        )


class TestWsConnect:
    async def test_tcp_delegates_to_websockets_connect(self):
        mock_ws = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "klangk.cli.transport.websockets.connect", return_value=mock_cm
        ) as mock_connect:
            async with ws_connect(
                "http://localhost:8995", token="tok", max_size=1024
            ) as ws:
                assert ws is mock_ws

        mock_connect.assert_called_once_with(
            "ws://localhost:8995/ws?token=tok", max_size=1024
        )

    async def test_uds_opens_unix_socket(self):
        mock_ws = MagicMock()
        mock_cm = MagicMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        mock_sock = MagicMock()

        with (
            patch(
                "klangk.cli.transport.websockets.connect", return_value=mock_cm
            ) as mock_connect,
            patch(
                "klangk.cli.transport._socket.socket", return_value=mock_sock
            ),
        ):
            async with ws_connect(
                "/tmp/klangk.sock", token="tok", max_size=2048
            ) as ws:
                assert ws is mock_ws

        mock_sock.connect.assert_called_once_with("/tmp/klangk.sock")
        mock_connect.assert_called_once_with(
            "ws://localhost/ws?token=tok", sock=mock_sock, max_size=2048
        )
        mock_sock.close.assert_called_once()

    async def test_uds_closes_socket_on_error(self):
        mock_sock = MagicMock()
        mock_sock.connect.side_effect = OSError("connection refused")

        with (
            patch(
                "klangk.cli.transport._socket.socket", return_value=mock_sock
            ),
            pytest.raises(OSError, match="connection refused"),
        ):
            async with ws_connect("/tmp/klangk.sock", token="tok"):
                pass  # pragma: no cover

        mock_sock.close.assert_called_once()
