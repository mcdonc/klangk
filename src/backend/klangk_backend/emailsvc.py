"""Email sending via SMTP or local sendmail."""

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

import aiosmtplib
from jinja2 import (
    ChoiceLoader,
    Environment,
    FileSystemLoader,
    PackageLoader,
    select_autoescape,
)

from . import auth
from .exceptions import SendmailError
from .util import resolve_env_value

logger = logging.getLogger(__name__)

# Supported email events. Each maps to a directory under
# email_templates/ holding subject.txt, body.txt, body.html. See #1165.
EMAIL_EVENTS = ("verify", "reset", "invite")

# Cached Jinja environment. Built lazily on first render; reset via
# reset_template_env() (mainly for tests that flip
# KLANGK_EMAIL_TEMPLATES_DIR between cases).
_env: Environment | None = None


def resolve_password() -> str | None:
    """Resolve KLANGK_SMTP_PASSWORD via resolve_env_value."""
    return resolve_env_value("KLANGK_SMTP_PASSWORD")


def product_name() -> str:
    """Configured product name (KLANGK_PRODUCT_NAME), default 'Klangk'.

    Used to white-label emails so deployments can rename the product
    without editing source. Resolved at call time via resolve_env_value so
    file:/cmd: prefixes work and value changes take effect per send.
    """
    return resolve_env_value("KLANGK_PRODUCT_NAME", "Klangk") or "Klangk"


def smtp_config() -> dict:
    """Read SMTP configuration from environment at call time."""
    return {
        "host": resolve_env_value("KLANGK_SMTP_HOST"),
        "port": int(resolve_env_value("KLANGK_SMTP_PORT", "587")),
        "user": resolve_env_value("KLANGK_SMTP_USER"),
        "password": resolve_password(),
        "from_addr": resolve_env_value("KLANGK_SMTP_FROM"),
        "use_tls": resolve_env_value("KLANGK_SMTP_USE_TLS", "true").lower()
        in ("true", "1"),
    }


def use_smtp() -> bool:
    """Return True if SMTP is configured, False to use sendmail."""
    return bool(resolve_env_value("KLANGK_SMTP_HOST"))


def reply_to() -> str | None:
    """Configured Reply-To address (KLANGK_SMTP_REPLY_TO), or None.

    Compliance/deliverability knob: orgs want a monitored reply address
    distinct from the envelope From. None -> no header (today's behavior).
    See #1165 / #261.
    """
    return resolve_env_value("KLANGK_SMTP_REPLY_TO", "") or None


def _set_headers(msg: EmailMessage) -> None:
    """Apply optional headers shared by every outgoing message."""
    rt = reply_to()
    if rt:
        msg["Reply-To"] = rt


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
    sendmail = resolve_env_value("KLANGK_SENDMAIL_PATH", "sendmail")
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


def reset_template_env() -> None:
    """Drop the cached Jinja environment.

    For tests that change KLANGK_EMAIL_TEMPLATES_DIR between cases, since
    the environment is built once and cached.
    """
    global _env
    _env = None


def _brand_ctx() -> dict:
    """Global branding variables surfaced to every email template.

    Resolved at call time so file:/cmd: prefixes work and value changes
    take effect without a restart. Mirrors what /config exposes to the
    frontend (see api.get_config).
    """
    return {
        "product_name": product_name(),
        "logo_url": resolve_env_value("KLANGK_LOGO_URL", "") or "",
        "brand_color": resolve_env_value("KLANGK_BRAND_COLOR", "#E65100")
        or "#E65100",
        # Configurable legal & support links (#1177). Plain env values, no
        # file:/cmd: resolution -- they are public and shown in the email
        # footer to all recipients. Mirrors what /config exposes to the
        # frontend; the base template renders whatever is set.
        "terms_url": os.environ.get("KLANGK_TERMS_URL", ""),
        "privacy_url": os.environ.get("KLANGK_PRIVACY_URL", ""),
        "aup_url": os.environ.get("KLANGK_AUP_URL", ""),
        "support_url": os.environ.get("KLANGK_SUPPORT_URL", ""),
        "support_email": os.environ.get("KLANGK_SUPPORT_EMAIL", ""),
    }


def _template_env() -> Environment:
    """Build (and cache) the Jinja environment.

    Uses a ChoiceLoader: a deployer directory (KLANGK_EMAIL_TEMPLATES_DIR)
    is tried first so it shadows the built-ins on a per-file basis; the
    built-in package templates (email_templates/) are the fallback. Both
    {% extends %} and {% include %} resolve through the same chain, so
    overriding just base.html re-brands every email at once. See #1165.
    """
    global _env
    if _env is None:
        loaders = []
        user_dir = resolve_env_value("KLANGK_EMAIL_TEMPLATES_DIR", "")
        if user_dir:
            path = Path(user_dir)
            if path.is_dir():
                loaders.append(FileSystemLoader(str(path)))
        loaders.append(PackageLoader("klangk_backend", "email_templates"))
        _env = Environment(
            loader=ChoiceLoader(loaders),
            # autoescape by extension: .html/.xml are escaped (closes the
            # template-authoring XSS gap str.format leaves open); .txt is
            # NOT (so <{{ link }}> stays literal). NOTE: a .html.j2 suffix
            # would NOT match and silently disable escaping -- keep .html.
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
            auto_reload=False,
        )
    return _env


@dataclass(frozen=True)
class EmailRender:
    """Rendered email content: a subject plus plain-text and HTML bodies."""

    subject: str
    text: str
    html: str


def render_email(
    event: str,
    *,
    link: str,
    expiry_hours: int | float,
    invited_by: str = "",
) -> EmailRender:
    """Render subject/text/html for an auth email event.

    ``event`` is one of EMAIL_EVENTS (verify / reset / invite). ``link`` is
    the per-email callback URL; ``expiry_hours`` is the real token TTL
    (fixing the prior drift where bodies hardcoded "72 hours"/"1 hour"
    regardless of config); ``invited_by`` is the inviter's email (invite
    only). Branding globals come from _brand_ctx().

    The subject receives only branding vars -- never the link/token -- so
    tokens can't leak into mail-server subject logs.
    """
    if event not in EMAIL_EVENTS:
        raise ValueError(f"unknown email event: {event!r}")
    env = _template_env()
    ctx = {
        **_brand_ctx(),
        "link": link,
        "expiry_hours": int(expiry_hours),
        "invited_by": invited_by,
    }
    subject = env.get_template(f"{event}/subject.txt").render(**ctx).strip()
    text = env.get_template(f"{event}/body.txt").render(**ctx)
    html = env.get_template(f"{event}/body.html").render(**ctx)
    return EmailRender(subject=subject, text=text, html=html)


def build_multipart(to: str, rendered: EmailRender) -> EmailMessage:
    """Assemble a multipart/alternative (text + HTML) message."""
    cfg = smtp_config()
    msg = EmailMessage()
    msg["Subject"] = rendered.subject
    msg["From"] = cfg["from_addr"] or cfg["user"] or "noreply@localhost"
    msg["To"] = to
    msg.set_content(rendered.text)
    msg.add_alternative(rendered.html, subtype="html")
    _set_headers(msg)
    return msg


async def _send(msg: EmailMessage) -> None:
    """Deliver via SMTP (if configured) or local sendmail."""
    if use_smtp():
        await send_via_smtp(msg)
    else:
        await send_via_sendmail(msg)


async def send_verification_email(to: str, verification_url: str) -> None:
    """Send a verification email with the given callback URL."""
    rendered = render_email(
        "verify",
        link=verification_url,
        expiry_hours=auth.VERIFY_TOKEN_EXPIRE_HOURS,
    )
    await _send(build_multipart(to, rendered))
    logger.info("Verification email sent to %s", to)


async def send_password_reset_email(to: str, reset_url: str) -> None:
    """Send a password reset email with the given callback URL."""
    rendered = render_email(
        "reset",
        link=reset_url,
        expiry_hours=auth.RESET_TOKEN_EXPIRE_HOURS,
    )
    await _send(build_multipart(to, rendered))
    logger.info("Password reset email sent to %s", to)


async def send_invitation_email(
    to: str, invite_url: str, invited_by_email: str
) -> None:
    """Send an invitation email with the given registration URL."""
    rendered = render_email(
        "invite",
        link=invite_url,
        expiry_hours=auth.INVITE_TOKEN_EXPIRE_HOURS,
        invited_by=invited_by_email,
    )
    await _send(build_multipart(to, rendered))
    logger.info(
        "Invitation email sent to %s (invited by %s)", to, invited_by_email
    )
