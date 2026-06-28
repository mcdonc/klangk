"""Email sending via SMTP or local sendmail."""

import asyncio
import html
import logging
import shutil
from email.message import EmailMessage

import aiosmtplib

from .exceptions import SendmailError
from .util import resolve_env_secret

logger = logging.getLogger(__name__)


def _resolve_password() -> str | None:
    """Resolve KLANGK_SMTP_PASSWORD via resolve_env_secret."""
    return resolve_env_secret("KLANGK_SMTP_PASSWORD")


def smtp_config() -> dict:
    """Read SMTP configuration from environment at call time."""
    return {
        "host": resolve_env_secret("KLANGK_SMTP_HOST"),
        "port": int(resolve_env_secret("KLANGK_SMTP_PORT", "587")),
        "user": resolve_env_secret("KLANGK_SMTP_USER"),
        "password": _resolve_password(),
        "from_addr": resolve_env_secret("KLANGK_SMTP_FROM"),
        "use_tls": resolve_env_secret("KLANGK_SMTP_USE_TLS", "true").lower()
        in ("true", "1"),
    }


def use_smtp() -> bool:
    """Return True if SMTP is configured, False to use sendmail."""
    return bool(resolve_env_secret("KLANGK_SMTP_HOST"))


def build_message(to: str, subject: str, body: str) -> EmailMessage:
    cfg = smtp_config()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"] or cfg["user"] or "noreply@localhost"
    msg["To"] = to
    msg.set_content(body)
    return msg


async def send_via_smtp(msg: EmailMessage) -> None:
    cfg = smtp_config()
    logger.debug(
        "SMTP config: host=%s port=%s user=%s tls=%s",
        cfg["host"],
        cfg["port"],
        cfg["user"],
        cfg["use_tls"],
    )
    kwargs: dict = {
        "hostname": cfg["host"],
        "port": cfg["port"],
    }
    if cfg["use_tls"]:
        kwargs["start_tls"] = True
    if cfg["user"] and cfg["password"]:
        kwargs["username"] = cfg["user"]
        kwargs["password"] = cfg["password"]
    await aiosmtplib.send(msg, **kwargs)
    logger.info("Email sent via SMTP to %s", msg["To"])


async def send_via_sendmail(msg: EmailMessage) -> None:
    sendmail = resolve_env_secret("KLANGK_SENDMAIL_PATH", "sendmail")
    logger.info("Using sendmail at: %s", sendmail)
    resolved = shutil.which(sendmail)
    logger.info("Resolved sendmail path: %s", resolved)
    proc = await asyncio.create_subprocess_exec(
        sendmail,
        "-t",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(msg.as_bytes())
    if proc.returncode != 0:
        raise SendmailError(
            f"sendmail ({sendmail}) exited with code {proc.returncode}: {stderr.decode()}"
        )
    logger.info("Email sent via sendmail to %s", msg["To"])


async def send_email(to: str, subject: str, body: str) -> None:
    """Send an email via SMTP (if configured) or local sendmail."""
    msg = build_message(to, subject, body)
    logger.info(
        "From: %s, To: %s, Subject: %s", msg["From"], to, msg["Subject"]
    )
    if use_smtp():
        logger.info(
            "Sending email to %s via SMTP (%s)",
            to,
            resolve_env_secret("KLANGK_SMTP_HOST"),
        )
        await send_via_smtp(msg)
    else:
        logger.info(
            "Sending email to %s via sendmail (no KLANGK_SMTP_HOST set)", to
        )
        await send_via_sendmail(msg)


async def send_verification_email(to: str, verification_url: str) -> None:
    """Send a verification email with the given callback URL.

    Sends as multipart/alternative with both plain text and HTML
    so the link is clickable regardless of mail client.
    """
    text_body = (
        "Click the link below to verify your email address and "
        "activate your Klangk account:\n\n"
        f"<{verification_url}>\n\n"
        "This link expires in 72 hours.\n\n"
        "If you did not request this, you can ignore this email."
    )
    html_body = (
        '<div style="font-family:sans-serif;max-width:480px;margin:0 auto">'
        '<div style="text-align:center;padding:24px 0">'
        '<span style="display:inline-block;background:#E65100;'
        "color:#fff;border-radius:50%;width:48px;height:48px;"
        'line-height:48px;font-size:24px">&#128062;</span>'
        '<h2 style="margin:8px 0 0">Klangk</h2>'
        "</div>"
        "<p>Click the link below to verify your email address and "
        "activate your Klangk account:</p>"
        f'<p><a href="{verification_url}">Verify my account</a></p>'
        "<p>This link expires in 72 hours.</p>"
        "<p><small>If you did not request this, you can "
        "ignore this email.</small></p>"
        "</div>"
    )
    cfg = smtp_config()
    msg = EmailMessage()
    msg["Subject"] = "Verify your Klangk account"
    msg["From"] = cfg["from_addr"] or cfg["user"] or "noreply@localhost"
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if use_smtp():
        await send_via_smtp(msg)
    else:
        await send_via_sendmail(msg)
    logger.info("Verification email sent to %s", to)


async def send_password_reset_email(to: str, reset_url: str) -> None:
    """Send a password reset email with the given callback URL."""
    text_body = (
        "Click the link below to reset your Klangk password:\n\n"
        f"<{reset_url}>\n\n"
        "This link expires in 1 hour.\n\n"
        "If you did not request this, you can ignore this email."
    )
    html_body = (
        '<div style="font-family:sans-serif;max-width:480px;'
        'margin:0 auto">'
        '<div style="text-align:center;padding:24px 0">'
        '<span style="display:inline-block;background:#E65100;'
        "color:#fff;border-radius:50%;width:48px;height:48px;"
        'line-height:48px;font-size:24px">&#128062;</span>'
        '<h2 style="margin:8px 0 0">Klangk</h2>'
        "</div>"
        "<p>Click the link below to reset your password:</p>"
        f'<p><a href="{reset_url}">Reset my password</a></p>'
        "<p>This link expires in 1 hour.</p>"
        "<p><small>If you did not request this, you can "
        "ignore this email.</small></p>"
        "</div>"
    )
    cfg = smtp_config()
    msg = EmailMessage()
    msg["Subject"] = "Reset your Klangk password"
    msg["From"] = cfg["from_addr"] or cfg["user"] or "noreply@localhost"
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if use_smtp():
        await send_via_smtp(msg)
    else:
        await send_via_sendmail(msg)
    logger.info("Password reset email sent to %s", to)


async def send_invitation_email(
    to: str, invite_url: str, invited_by_email: str
) -> None:
    """Send an invitation email with the given registration URL."""
    # Escape the inviter's email for safe interpolation into the HTML
    # body. The email validator permits characters such as '<', '>', and
    # '"' in the local part, so an unescaped value could be used to inject
    # markup/script into invitation emails sent to other users.
    invited_by_html = html.escape(invited_by_email)
    text_body = (
        f"{invited_by_email} has invited you to join Klangk.\n\n"
        "Click the link below to set your password and activate "
        "your account:\n\n"
        f"<{invite_url}>\n\n"
        "This link expires in 72 hours.\n\n"
        "If you did not expect this invitation, you can ignore this email."
    )
    html_body = (
        '<div style="font-family:sans-serif;max-width:480px;margin:0 auto">'
        '<div style="text-align:center;padding:24px 0">'
        '<span style="display:inline-block;background:#E65100;'
        "color:#fff;border-radius:50%;width:48px;height:48px;"
        'line-height:48px;font-size:24px">&#128062;</span>'
        '<h2 style="margin:8px 0 0">Klangk</h2>'
        "</div>"
        f"<p><strong>{invited_by_html}</strong> has invited you to "
        "join Klangk.</p>"
        "<p>Click the link below to set your password and activate "
        "your account:</p>"
        f'<p><a href="{invite_url}">Accept invitation</a></p>'
        "<p>This link expires in 72 hours.</p>"
        "<p><small>If you did not expect this invitation, you can "
        "ignore this email.</small></p>"
        "</div>"
    )
    cfg = smtp_config()
    msg = EmailMessage()
    msg["Subject"] = "You've been invited to Klangk"
    msg["From"] = cfg["from_addr"] or cfg["user"] or "noreply@localhost"
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    if use_smtp():
        await send_via_smtp(msg)
    else:
        await send_via_sendmail(msg)
    logger.info(
        "Invitation email sent to %s (invited by %s)", to, invited_by_email
    )
