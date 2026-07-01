"""Tests for emailsvc: SMTP and sendmail sending."""

from email.message import EmailMessage
from unittest.mock import AsyncMock, patch

import pytest

from klangk_backend import emailsvc
from klangk_backend.exceptions import SendmailError


class TestBuildMessage:
    def test_builds_message(self):
        msg = emailsvc.build_message("to@example.com", "Subject", "Body")
        assert isinstance(msg, EmailMessage)
        assert msg["To"] == "to@example.com"
        assert msg["Subject"] == "Subject"
        assert msg.get_content().strip() == "Body"

    def test_from_uses_smtp_from(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_FROM", "custom@example.com")
        msg = emailsvc.build_message("to@example.com", "Hi", "Body")
        assert msg["From"] == "custom@example.com"

    def test_from_falls_back_to_smtp_user(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_FROM", raising=False)
        monkeypatch.setenv("KLANGK_SMTP_USER", "user@example.com")
        msg = emailsvc.build_message("to@example.com", "Hi", "Body")
        assert msg["From"] == "user@example.com"

    def test_from_falls_back_to_noreply(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_FROM", raising=False)
        monkeypatch.delenv("KLANGK_SMTP_USER", raising=False)
        msg = emailsvc.build_message("to@example.com", "Hi", "Body")
        assert msg["From"] == "noreply@localhost"


class TestResolvePassword:
    def test_plain_password(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_PASSWORD", "secret123")
        assert emailsvc._resolve_password() == "secret123"

    def test_file_prefix_reads_file(self, monkeypatch, tmp_path):
        pw_file = tmp_path / "smtp_pass"
        pw_file.write_text("file-secret\n")
        monkeypatch.setenv("KLANGK_SMTP_PASSWORD", f"file:{pw_file}")
        assert emailsvc._resolve_password() == "file-secret"

    def test_file_missing_returns_none(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_PASSWORD", "file:/nonexistent/file")
        assert emailsvc._resolve_password() is None

    def test_no_password(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_PASSWORD", raising=False)
        assert emailsvc._resolve_password() is None


class TestUseSmtp:
    def test_uses_smtp_when_host_set(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_HOST", "mail.example.com")
        assert emailsvc.use_smtp() is True

    def test_uses_sendmail_when_no_host(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_HOST", raising=False)
        assert emailsvc.use_smtp() is False


class TestSendViaSmtp:
    async def test_calls_aiosmtplib_send(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("KLANGK_SMTP_PORT", "587")
        monkeypatch.setenv("KLANGK_SMTP_USER", "user")
        monkeypatch.setenv("KLANGK_SMTP_PASSWORD", "pass")
        monkeypatch.setenv("KLANGK_SMTP_USE_TLS", "true")

        mock_send = AsyncMock()
        with patch.object(emailsvc.aiosmtplib, "send", mock_send):
            msg = emailsvc.build_message("to@example.com", "Hi", "Body")
            await emailsvc.send_via_smtp(msg)

        mock_send.assert_awaited_once()
        kwargs = mock_send.call_args[1]
        assert kwargs["hostname"] == "smtp.example.com"
        assert kwargs["port"] == 587
        assert kwargs["username"] == "user"
        assert kwargs["password"] == "pass"
        assert kwargs["start_tls"] is True

    async def test_no_auth_when_no_credentials(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("KLANGK_SMTP_PORT", "25")
        monkeypatch.delenv("KLANGK_SMTP_USER", raising=False)
        monkeypatch.delenv("KLANGK_SMTP_PASSWORD", raising=False)
        monkeypatch.setenv("KLANGK_SMTP_USE_TLS", "false")

        mock_send = AsyncMock()
        with patch.object(emailsvc.aiosmtplib, "send", mock_send):
            msg = emailsvc.build_message("to@example.com", "Hi", "Body")
            await emailsvc.send_via_smtp(msg)

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
            msg = emailsvc.build_message("to@example.com", "Hi", "Body")
            await emailsvc.send_via_sendmail(msg)

        mock_exec.assert_awaited_once()
        assert mock_exec.call_args[0][0] == "sendmail"

    async def test_custom_sendmail_path(self, monkeypatch):
        monkeypatch.setenv(
            "KLANGK_SENDMAIL_PATH", "/run/current-system/sw/bin/sendmail"
        )
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0

        with patch(
            "asyncio.create_subprocess_exec", return_value=mock_proc
        ) as mock_exec:
            msg = emailsvc.build_message("to@example.com", "Hi", "Body")
            await emailsvc.send_via_sendmail(msg)

        assert (
            mock_exec.call_args[0][0] == "/run/current-system/sw/bin/sendmail"
        )

    async def test_raises_on_sendmail_failure(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"sendmail error")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            msg = emailsvc.build_message("to@example.com", "Hi", "Body")
            with pytest.raises(SendmailError, match="exited with code 1"):
                await emailsvc.send_via_sendmail(msg)


class TestSendEmail:
    async def test_uses_smtp_when_configured(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_HOST", "smtp.example.com")
        mock_smtp = AsyncMock()
        with patch.object(emailsvc, "send_via_smtp", mock_smtp):
            await emailsvc.send_email("to@example.com", "Hi", "Body")
        mock_smtp.assert_awaited_once()

    async def test_uses_sendmail_when_no_smtp(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_HOST", raising=False)
        mock_sendmail = AsyncMock()
        with patch.object(emailsvc, "send_via_sendmail", mock_sendmail):
            await emailsvc.send_email("to@example.com", "Hi", "Body")
        mock_sendmail.assert_awaited_once()


class TestSendVerificationEmail:
    async def test_sends_verification_email(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_HOST", raising=False)
        mock_sendmail = AsyncMock()
        with patch.object(emailsvc, "send_via_sendmail", mock_sendmail):
            await emailsvc.send_verification_email(
                "user@example.com",
                "https://klangk.example.com/#/verify?token=abc123",
            )
        mock_sendmail.assert_awaited_once()
        msg = mock_sendmail.call_args[0][0]
        assert msg["To"] == "user@example.com"
        assert "Verify" in msg["Subject"]
        # Multipart: plain text + HTML
        parts = list(msg.iter_parts())
        assert len(parts) == 2
        text_part = parts[0].get_content()
        assert "https://klangk.example.com/#/verify?token=abc123" in text_part
        assert "72 hours" in text_part
        html_part = parts[1].get_content()
        assert (
            'href="https://klangk.example.com/#/verify?token=abc123"'
            in html_part
        )
        assert "Verify my account" in html_part
        assert "Klangk" in html_part

    async def test_sends_via_smtp_when_configured(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("KLANGK_SMTP_USER", "user")
        monkeypatch.setenv("KLANGK_SMTP_PASSWORD", "pass")
        mock_smtp = AsyncMock()
        with patch.object(emailsvc, "send_via_smtp", mock_smtp):
            await emailsvc.send_verification_email(
                "user@example.com",
                "https://klangk.example.com/#/verify?token=abc",
            )
        mock_smtp.assert_awaited_once()

    async def test_uses_configured_product_name(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_HOST", raising=False)
        monkeypatch.setenv("KLANGK_PRODUCT_NAME", "Acme Labs")
        mock_sendmail = AsyncMock()
        with patch.object(emailsvc, "send_via_sendmail", mock_sendmail):
            await emailsvc.send_verification_email(
                "user@example.com",
                "https://klangk.example.com/#/verify?token=abc",
            )
        msg = mock_sendmail.call_args[0][0]
        assert msg["Subject"] == "Verify your Acme Labs account"
        parts = list(msg.iter_parts())
        assert "Acme Labs" in parts[0].get_content()
        assert "Acme Labs" in parts[1].get_content()
        # The default wordmark must not appear when a name is configured.
        assert "Klangk" not in parts[1].get_content()


class TestSendPasswordResetEmail:
    async def test_sends_reset_email(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_HOST", raising=False)
        mock_sendmail = AsyncMock()
        with patch.object(emailsvc, "send_via_sendmail", mock_sendmail):
            await emailsvc.send_password_reset_email(
                "user@example.com",
                "https://klangk.example.com/#/reset-password?token=xyz",
            )
        mock_sendmail.assert_awaited_once()
        msg = mock_sendmail.call_args[0][0]
        assert msg["To"] == "user@example.com"
        assert "Reset" in msg["Subject"]
        parts = list(msg.iter_parts())
        assert len(parts) == 2
        text_part = parts[0].get_content()
        assert "reset-password?token=xyz" in text_part
        assert "1 hour" in text_part
        html_part = parts[1].get_content()
        assert 'href="https://klangk.example.com/#/reset-password' in html_part
        assert "Reset my password" in html_part

    async def test_sends_via_smtp_when_configured(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("KLANGK_SMTP_USER", "user")
        monkeypatch.setenv("KLANGK_SMTP_PASSWORD", "pass")
        mock_smtp = AsyncMock()
        with patch.object(emailsvc, "send_via_smtp", mock_smtp):
            await emailsvc.send_password_reset_email(
                "user@example.com",
                "https://klangk.example.com/#/reset-password?token=xyz",
            )
        mock_smtp.assert_awaited_once()


class TestSendInvitationEmail:
    async def test_sends_invitation_email(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_HOST", raising=False)
        mock_sendmail = AsyncMock()
        with patch.object(emailsvc, "send_via_sendmail", mock_sendmail):
            await emailsvc.send_invitation_email(
                "invited@example.com",
                "https://klangk.example.com/#/accept-invite?token=abc123",
                "admin@example.com",
            )
        mock_sendmail.assert_awaited_once()
        msg = mock_sendmail.call_args[0][0]
        assert msg["To"] == "invited@example.com"
        assert "invited" in msg["Subject"].lower()
        parts = list(msg.iter_parts())
        assert len(parts) == 2
        text_part = parts[0].get_content()
        assert "admin@example.com" in text_part
        assert "accept-invite?token=abc123" in text_part
        assert "72 hours" in text_part
        html_part = parts[1].get_content()
        assert 'href="https://klangk.example.com/#/accept-invite' in html_part
        assert "admin@example.com" in html_part
        assert "Accept invitation" in html_part

    async def test_sends_via_smtp_when_configured(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("KLANGK_SMTP_USER", "user")
        monkeypatch.setenv("KLANGK_SMTP_PASSWORD", "pass")
        mock_smtp = AsyncMock()
        with patch.object(emailsvc, "send_via_smtp", mock_smtp):
            await emailsvc.send_invitation_email(
                "invited@example.com",
                "https://klangk.example.com/#/accept-invite?token=abc",
                "admin@example.com",
            )
        mock_smtp.assert_awaited_once()

    async def test_invited_by_email_html_escaped(self, monkeypatch):
        # A crafted inviter email with HTML/marker characters must be
        # escaped in the HTML body so it cannot inject markup or script.
        # See https://github.com/mcdonc/klangk/issues/878
        monkeypatch.delenv("KLANGK_SMTP_HOST", raising=False)
        mock_sendmail = AsyncMock()
        crafted = 'admin"><img/src=x onerror=alert(1)>@example.com'
        with patch.object(emailsvc, "send_via_sendmail", mock_sendmail):
            await emailsvc.send_invitation_email(
                "invited@example.com",
                "https://klangk.example.com/#/accept-invite?token=abc",
                crafted,
            )
        msg = mock_sendmail.call_args[0][0]
        parts = list(msg.iter_parts())
        html_part = parts[1].get_content()
        # The crafted payload must not form a live HTML element; angle
        # brackets and quotes must be escaped so the email renders as
        # inert text rather than injecting markup/script.
        assert "<img" not in html_part
        assert "&lt;img" in html_part
        assert 'admin"&gt;' not in html_part
        assert "admin&quot;&gt;" in html_part
