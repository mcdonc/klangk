"""Email sending via SMTP or local sendmail."""

import asyncio
import logging
import os
from email.message import EmailMessage

import aiosmtplib

logger = logging.getLogger(__name__)


def _smtp_config() -> dict:
    """Read SMTP configuration from environment at call time."""
    return {
        "host": os.environ.get("BARK_SMTP_HOST"),
        "port": int(os.environ.get("BARK_SMTP_PORT", "587")),
        "user": os.environ.get("BARK_SMTP_USER"),
        "password": os.environ.get("BARK_SMTP_PASSWORD"),
        "from_addr": os.environ.get("BARK_SMTP_FROM"),
        "use_tls": os.environ.get("BARK_SMTP_USE_TLS", "true").lower()
        in ("true", "1"),
    }


def _use_smtp() -> bool:
    """Return True if SMTP is configured, False to use sendmail."""
    return bool(os.environ.get("BARK_SMTP_HOST"))


def _build_message(to: str, subject: str, body: str) -> EmailMessage:
    cfg = _smtp_config()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from_addr"] or cfg["user"] or "noreply@localhost"
    msg["To"] = to
    msg.set_content(body)
    return msg


async def _send_via_smtp(msg: EmailMessage) -> None:
    cfg = _smtp_config()
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


async def _send_via_sendmail(msg: EmailMessage) -> None:
    sendmail = os.environ.get("BARK_SENDMAIL_PATH", "sendmail")
    logger.info("Using sendmail at: %s", sendmail)
    import shutil

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
        raise RuntimeError(
            f"sendmail ({sendmail}) exited with code {proc.returncode}: {stderr.decode()}"
        )
    logger.info("Email sent via sendmail to %s", msg["To"])


async def send_email(to: str, subject: str, body: str) -> None:
    """Send an email via SMTP (if configured) or local sendmail."""
    msg = _build_message(to, subject, body)
    logger.info(
        "From: %s, To: %s, Subject: %s", msg["From"], to, msg["Subject"]
    )
    if _use_smtp():
        logger.info(
            "Sending email to %s via SMTP (%s)",
            to,
            os.environ.get("BARK_SMTP_HOST"),
        )
        await _send_via_smtp(msg)
    else:
        logger.info(
            "Sending email to %s via sendmail (no BARK_SMTP_HOST set)", to
        )
        await _send_via_sendmail(msg)


async def send_verification_email(to: str, verification_url: str) -> None:
    """Send a verification email with the given callback URL."""
    logger.info(
        "Sending verification email to %s with URL: %s", to, verification_url
    )
    subject = "Verify your Bark account"
    body = (
        f"Click the link below to verify your email address and activate "
        f"your Bark account:\n\n{verification_url}\n\n"
        f"This link expires in 72 hours.\n\n"
        f"If you did not request this, you can ignore this email."
    )
    await send_email(to, subject, body)
