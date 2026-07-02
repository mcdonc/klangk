"""Tests for emailsvc: SMTP and sendmail sending."""

from email.message import EmailMessage
from unittest.mock import AsyncMock, patch

import pytest

from klangk_backend import emailsvc
from klangk_backend.exceptions import SendmailError


# The Jinja environment is built once and cached at module level, so a
# KLANGK_EMAIL_TEMPLATES_DIR set in one test would leak into the next.
# Rebuild it fresh for every case (env-var state itself is handled by
# monkeypatch). See #1165.
@pytest.fixture(autouse=True)
def _reset_email_template_env():
    emailsvc.reset_template_env()
    yield
    emailsvc.reset_template_env()


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


def _plain_msg(to="to@example.com", subject="Hi", body="Body"):
    """Build a minimal EmailMessage for transport-layer tests."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = "test@example.com"
    msg["To"] = to
    msg.set_content(body)
    return msg


class TestSendViaSmtp:
    async def test_calls_aiosmtplib_send(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_HOST", "smtp.example.com")
        monkeypatch.setenv("KLANGK_SMTP_PORT", "587")
        monkeypatch.setenv("KLANGK_SMTP_USER", "user")
        monkeypatch.setenv("KLANGK_SMTP_PASSWORD", "pass")
        monkeypatch.setenv("KLANGK_SMTP_USE_TLS", "true")

        mock_send = AsyncMock()
        with patch.object(emailsvc.aiosmtplib, "send", mock_send):
            await emailsvc.send_via_smtp(_plain_msg())

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
            await emailsvc.send_via_smtp(_plain_msg())

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
            await emailsvc.send_via_sendmail(_plain_msg())

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
            await emailsvc.send_via_sendmail(_plain_msg())

        assert (
            mock_exec.call_args[0][0] == "/run/current-system/sw/bin/sendmail"
        )

    async def test_raises_on_sendmail_failure(self):
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"sendmail error")
        mock_proc.returncode = 1

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(SendmailError, match="exited with code 1"):
                await emailsvc.send_via_sendmail(_plain_msg())


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
        # inert text rather than injecting markup/script. Autoescape now
        # runs through Jinja/markupsafe (numeric entities &#34;/&#39;)
        # rather than the old html.escape named forms (&quot;/&#x27;), so
        # assert the security property, not a specific entity spelling.
        assert "<img" not in html_part
        assert "&lt;img" in html_part
        assert 'admin"><img' not in html_part
        # the quote and '>' are escaped (named or numeric form)
        assert "&gt;" in html_part
        assert ("&quot;" in html_part) or ("&#34;" in html_part)


class TestRenderEmail:
    def test_verify_event_renders_all_three_parts(self):
        r = emailsvc.render_email(
            "verify", link="https://x/v?t=1", expiry_hours=72
        )
        assert r.subject == "Verify your Klangk account"
        assert "https://x/v?t=1" in r.text
        assert "expires in 72 hours" in r.text
        assert 'href="https://x/v?t=1"' in r.html

    def test_unknown_event_raises(self):
        # Guards against a typo in a caller wiring up a new event.
        with pytest.raises(ValueError, match="unknown email event"):
            emailsvc.render_email("bogus", link="https://x", expiry_hours=1)

    def test_expiry_hours_interpolated_not_hardcoded(self):
        # Proves the drift fix (#1165): a non-default TTL is reflected
        # in BOTH text and html, instead of the old hardcoded "72 hours".
        r = emailsvc.render_email("verify", link="https://x", expiry_hours=48)
        assert "expires in 48 hours" in r.text
        assert "expires in 48 hours" in r.html

    def test_singular_hour_when_expiry_is_one(self):
        r = emailsvc.render_email("reset", link="https://x", expiry_hours=1)
        assert "expires in 1 hour." in r.text
        assert "1 hours" not in r.text

    def test_brand_color_in_badge(self, monkeypatch):
        monkeypatch.setenv("KLANGK_BRAND_COLOR", "#abcdef")
        r = emailsvc.render_email("verify", link="https://x", expiry_hours=72)
        assert "#abcdef" in r.html

    def test_logo_url_replaces_badge(self, monkeypatch):
        monkeypatch.setenv("KLANGK_LOGO_URL", "https://logo/test.png")
        r = emailsvc.render_email("verify", link="https://x", expiry_hours=72)
        assert '<img src="https://logo/test.png"' in r.html
        # The paw badge is hidden when a logo override is set.
        assert "&#128062;" not in r.html

    def test_product_name_flows_through(self, monkeypatch):
        monkeypatch.setenv("KLANGK_PRODUCT_NAME", "Acme Labs")
        r = emailsvc.render_email("verify", link="https://x", expiry_hours=72)
        assert r.subject == "Verify your Acme Labs account"
        assert "Acme Labs" in r.html
        assert "Klangk" not in r.html

    def test_legal_links_in_footer_when_set(self, monkeypatch):
        # The legal footer renders only the configured links, joined.
        monkeypatch.setenv("KLANGK_TERMS_URL", "https://corp/t")
        monkeypatch.setenv("KLANGK_PRIVACY_URL", "https://corp/p")
        monkeypatch.setenv("KLANGK_AUP_URL", "https://corp/a")
        r = emailsvc.render_email("verify", link="https://x", expiry_hours=72)
        assert 'href="https://corp/t">Terms' in r.html
        assert 'href="https://corp/p">Privacy' in r.html
        assert 'href="https://corp/a">Acceptable Use' in r.html
        # Joined by a middot separator, not run together.
        assert "Terms</a> &middot; <a" in r.html

    def test_support_link_and_email_in_footer(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SUPPORT_URL", "https://help")
        monkeypatch.setenv("KLANGK_SUPPORT_EMAIL", "help@corp")
        r = emailsvc.render_email("verify", link="https://x", expiry_hours=72)
        assert 'href="https://help">Support' in r.html
        assert 'href="mailto:help@corp">help@corp' in r.html

    def test_no_legal_footer_when_none_set(self, monkeypatch):
        # When no legal/support vars are set, the footer block is hidden.
        for k in (
            "KLANGK_TERMS_URL",
            "KLANGK_PRIVACY_URL",
            "KLANGK_AUP_URL",
            "KLANGK_SUPPORT_URL",
            "KLANGK_SUPPORT_EMAIL",
        ):
            monkeypatch.delenv(k, raising=False)
        r = emailsvc.render_email("verify", link="https://x", expiry_hours=72)
        assert ">Terms<" not in r.html
        assert ">Privacy<" not in r.html
        assert "mailto:" not in r.html


class TestTemplateOverlay:
    def test_user_dir_shadows_builtin_per_file(self, tmp_path, monkeypatch):
        # A deployer dir with only an overriding subject shadows that one
        # file; everything else falls through to the built-ins.
        d = tmp_path / "templates"
        (d / "verify").mkdir(parents=True)
        (d / "verify" / "subject.txt").write_text("CUSTOM VERIFY SUBJECT\n")
        monkeypatch.setenv("KLANGK_EMAIL_TEMPLATES_DIR", str(d))
        r = emailsvc.render_email(
            "verify", link="https://x/v?t=1", expiry_hours=72
        )
        assert r.subject == "CUSTOM VERIFY SUBJECT"
        # The body was NOT overridden -> built-in still used.
        assert "verify your email address" in r.text
        # A different event (not overridden) keeps its built-in subject.
        rr = emailsvc.render_email(
            "reset", link="https://x/r?t=1", expiry_hours=1
        )
        assert rr.subject == "Reset your Klangk password"

    def test_overriding_base_rebrands_all_events(self, tmp_path, monkeypatch):
        # Editing base.html alone re-brands every email, because each
        # child does {% extends "base.html" %} and the ChoiceLoader
        # resolves the override first.
        d = tmp_path / "templates"
        d.mkdir()
        (d / "base.html").write_text(
            "<div>BRANDED {{ product_name }}</div>"
            "{% block content %}{% endblock %}"
        )
        monkeypatch.setenv("KLANGK_EMAIL_TEMPLATES_DIR", str(d))
        rv = emailsvc.render_email("verify", link="https://x", expiry_hours=72)
        ri = emailsvc.render_email(
            "invite", link="https://x", expiry_hours=72, invited_by="a@b.com"
        )
        assert "BRANDED Klangk" in rv.html
        assert "BRANDED Klangk" in ri.html

    def test_nonexistent_user_dir_is_ignored(self, tmp_path, monkeypatch):
        # A bad path doesn't crash; built-ins are used.
        monkeypatch.setenv(
            "KLANGK_EMAIL_TEMPLATES_DIR", str(tmp_path / "does-not-exist")
        )
        r = emailsvc.render_email("verify", link="https://x", expiry_hours=72)
        assert r.subject == "Verify your Klangk account"

    def test_html_autoescape_on_html_off_text(self, monkeypatch):
        # .html is autoescaped (closes the str.format XSS gap); .txt is not.
        r = emailsvc.render_email(
            "invite",
            link="https://x",
            expiry_hours=72,
            invited_by="<script>@e.com",
        )
        assert "<script>@e.com" in r.text  # literal in plain text
        assert "<script>@e.com" not in r.html  # escaped in HTML
        assert "&lt;script&gt;@e.com" in r.html


class TestReplyTo:
    def test_reply_to_none_when_unset(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_REPLY_TO", raising=False)
        assert emailsvc.reply_to() is None

    def test_reply_to_resolved_when_set(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_REPLY_TO", "support@example.com")
        assert emailsvc.reply_to() == "support@example.com"

    async def test_multipart_carries_reply_to(self, monkeypatch):
        monkeypatch.setenv("KLANGK_SMTP_REPLY_TO", "support@example.com")
        rendered = emailsvc.render_email(
            "verify", link="https://x", expiry_hours=72
        )
        msg = emailsvc._build_multipart("to@e.com", rendered)
        assert msg["Reply-To"] == "support@example.com"

    def test_multipart_no_reply_to_when_unset(self, monkeypatch):
        monkeypatch.delenv("KLANGK_SMTP_REPLY_TO", raising=False)
        rendered = emailsvc.render_email(
            "verify", link="https://x", expiry_hours=72
        )
        msg = emailsvc._build_multipart("to@e.com", rendered)
        assert msg["Reply-To"] is None
