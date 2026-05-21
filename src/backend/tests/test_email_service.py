"""Tests for email_service: SMTP and sendmail sending."""

from email.message import EmailMessage
from unittest.mock import AsyncMock, patch

import pytest

from bark_backend import email_service


class TestBuildMessage:
    def test_builds_message(self):
        msg = email_service._build_message("to@example.com", "Subject", "Body")
        assert isinstance(msg, EmailMessage)
        assert msg["To"] == "to@example.com"
        assert msg["Subject"] == "Subject"
        assert msg.get_content().strip() == "Body"

    def test_from_uses_smtp_from(self, monkeypatch):
        monkeypatch.setenv("BARK_SMTP_FROM", "custom@example.com")
        msg = email_service._build_message("to@example.com", "Hi", "Body")
        assert msg["From"] == "custom@example.com"

    def test_from_falls_back_to_smtp_user(self, monkeypatch):
        monkeypatch.delenv("BARK_SMTP_FROM", raising=False)
        monkeypatch.setenv("BARK_SMTP_USER", "user@example.com")
        msg = email_service._build_message("to@example.com", "Hi", "Body")
        assert msg["From"] == "user@example.com"

    def test_from_falls_back_to_noreply(self, monkeypatch):
        monkeypatch.delenv("BARK_SMTP_FROM", raising=False)
        monkeypatch.delenv("BARK_SMTP_USER", raising=False)
        msg = email_service._build_message("to@example.com", "Hi", "Body")
        assert msg["From"] == "noreply@localhost"


class TestUseSmtp:
    def test_uses_smtp_when_host_set(self, monkeypatch):
        monkeypatch.setenv("BARK_SMTP_HOST", "mail.example.com")
        assert email_service._use_smtp() is True

    def test_uses_sendmail_when_no_host(self, monkeypatch):
        monkeypatch.delenv("BARK_SMTP_HOST", raising=False)
        assert email_service._use_smtp() is False


class TestSendViaSmtp:
    async def test_calls_aiosmtplib_send(self, monkeypatch):
        monkeypatch.setenv("BARK_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("BARK_SMTP_PORT", "587")
        monkeypatch.setenv("BARK_SMTP_USER", "user")
        monkeypatch.setenv("BARK_SMTP_PASSWORD", "pass")
        monkeypatch.setenv("BARK_SMTP_USE_TLS", "true")

        mock_send = AsyncMock()
        with patch.object(email_service.aiosmtplib, "send", mock_send):
            msg = email_service._build_message("to@example.com", "Hi", "Body")
            await email_service._send_via_smtp(msg)

        mock_send.assert_awaited_once()
        kwargs = mock_send.call_args[1]
        assert kwargs["hostname"] == "smtp.example.com"
        assert kwargs["port"] == 587
        assert kwargs["username"] == "user"
        assert kwargs["password"] == "pass"
        assert kwargs["start_tls"] is True

    async def test_no_auth_when_no_credentials(self, monkeypatch):
        monkeypatch.setenv("BARK_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("BARK_SMTP_PORT", "25")
        monkeypatch.delenv("BARK_SMTP_USER", raising=False)
        monkeypatch.delenv("BARK_SMTP_PASSWORD", raising=False)
        monkeypatch.setenv("BARK_SMTP_USE_TLS", "false")

        mock_send = AsyncMock()
        with patch.object(email_service.aiosmtplib, "send", mock_send):
            msg = email_service._build_message("to@example.com", "Hi", "Body")
            await email_service._send_via_smtp(msg)

        kwargs = mock_send.call_args[1]
        assert "username" not in kwargs
        assert "password" not in kwargs
        assert "start_tls" not in kwargs


class TestSendViaSendmail:
    async def test_calls_sendmail_subprocess(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            msg = email_service._build_message("to@example.com", "Hi", "Body")
            await email_service._send_via_sendmail(msg)

        mock_exec.assert_awaited_once()
        assert mock_exec.call_args[0][0] == "sendmail"

    async def test_custom_sendmail_path(self, monkeypatch):
        monkeypatch.setenv(
            "BARK_SENDMAIL_PATH", "/run/current-system/sw/bin/sendmail"
        )
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            msg = email_service._build_message("to@example.com", "Hi", "Body")
            await email_service._send_via_sendmail(msg)

        assert (
            mock_exec.call_args[0][0] == "/run/current-system/sw/bin/sendmail"
        )

    async def test_raises_on_sendmail_failure(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"sendmail error")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            msg = email_service._build_message("to@example.com", "Hi", "Body")
            with pytest.raises(RuntimeError, match="exited with code 1"):
                await email_service._send_via_sendmail(msg)


class TestSendEmail:
    async def test_uses_smtp_when_configured(self, monkeypatch):
        monkeypatch.setenv("BARK_SMTP_HOST", "smtp.example.com")
        mock_smtp = AsyncMock()
        with patch.object(email_service, "_send_via_smtp", mock_smtp):
            await email_service.send_email("to@example.com", "Hi", "Body")
        mock_smtp.assert_awaited_once()

    async def test_uses_sendmail_when_no_smtp(self, monkeypatch):
        monkeypatch.delenv("BARK_SMTP_HOST", raising=False)
        mock_sendmail = AsyncMock()
        with patch.object(email_service, "_send_via_sendmail", mock_sendmail):
            await email_service.send_email("to@example.com", "Hi", "Body")
        mock_sendmail.assert_awaited_once()


class TestSendVerificationEmail:
    async def test_sends_verification_email(self, monkeypatch):
        monkeypatch.delenv("BARK_SMTP_HOST", raising=False)
        mock_sendmail = AsyncMock()
        with patch.object(email_service, "_send_via_sendmail", mock_sendmail):
            await email_service.send_verification_email(
                "user@example.com",
                "https://bark.example.com/#/verify?token=abc123",
            )
        mock_sendmail.assert_awaited_once()
        msg = mock_sendmail.call_args[0][0]
        assert msg["To"] == "user@example.com"
        assert "Verify" in msg["Subject"]
        body = msg.get_content()
        assert "https://bark.example.com/#/verify?token=abc123" in body
        assert "72 hours" in body
