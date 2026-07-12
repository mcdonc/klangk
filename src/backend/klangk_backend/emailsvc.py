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
from .settings import resolve_indirection

logger = logging.getLogger(__name__)

# Supported email events. Each maps to a directory under
# email_templates/ holding subject.txt, body.txt, body.html. See #1165.
EMAIL_EVENTS = ("verify", "reset", "invite")


@dataclass(frozen=True)
class EmailRender:
    """Rendered email content: a subject plus plain-text and HTML bodies."""

    subject: str
    text: str
    html: str


class EmailService:
    """Owned email subsystem wired onto ``app.state.email`` (#1483).

    Replaces the flat module-level functions that read config at call
    time. Config is read from ``self.settings`` (``KlangkSettings``);
    every env var the module touches has a matching typed field.
    """

    def __init__(self, app_state) -> None:
        self.app_state = app_state
        self.settings = app_state.settings
        # Per-instance cached Jinja environment. Built lazily on first
        # render; reset via reset_template_env() (mainly for tests that
        # flip KLANGK_EMAIL_TEMPLATES_DIR between cases).
        self._env: Environment | None = None

    # --- config ---

    def resolve_password(self) -> str | None:
        """Resolve KLANGK_SMTP_PASSWORD.

        ``file:``/``cmd:`` prefixes are dereferenced here at call time.
        Remove this indirection (read ``self.settings.smtp_password``
        directly) once #1461 moves resolution into KlangkSettings at
        construction.
        """
        return resolve_indirection(
            self.settings.smtp_password, "KLANGK_SMTP_PASSWORD"
        )

    def product_name(self) -> str:
        """Configured product name (KLANGK_PRODUCT_NAME), default 'Klangk'."""
        return self.settings.product_name or "Klangk"

    def smtp_config(self) -> dict:
        """Read SMTP configuration from settings at call time."""
        return {
            "host": self.settings.smtp_host,
            "port": int(self.settings.smtp_port or "587"),
            "user": self.settings.smtp_user,
            "password": self.resolve_password(),
            "from_addr": self.settings.smtp_from,
            "use_tls": (self.settings.smtp_use_tls or "true").lower()
            in ("true", "1"),
        }

    def use_smtp(self) -> bool:
        """Return True if SMTP is configured, False to use sendmail."""
        return bool(self.settings.smtp_host)

    def reply_to(self) -> str | None:
        """Configured Reply-To address (KLANGK_SMTP_REPLY_TO), or None.

        Compliance/deliverability knob: orgs want a monitored reply address
        distinct from the envelope From. None -> no header (today's
        behavior). See #1165 / #261.
        """
        return self.settings.smtp_reply_to or None

    def _set_headers(self, msg: EmailMessage) -> None:
        """Apply optional headers shared by every outgoing message."""
        rt = self.reply_to()
        if rt:
            msg["Reply-To"] = rt

    # --- transport ---

    async def send_via_smtp(self, msg: EmailMessage) -> None:
        cfg = self.smtp_config()
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

    async def send_via_sendmail(self, msg: EmailMessage) -> None:
        sendmail = self.settings.sendmail_path or "sendmail"
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

    # --- templating ---

    def reset_template_env(self) -> None:
        """Drop the cached Jinja environment.

        For tests that change KLANGK_EMAIL_TEMPLATES_DIR between cases,
        since the environment is built once and cached.
        """
        self._env = None

    def _brand_ctx(self) -> dict:
        """Global branding variables surfaced to every email template.

        Mirrors what /config exposes to the frontend (see api.get_config).
        """
        s = self.settings
        return {
            "product_name": self.product_name(),
            "logo_url": s.logo_url or "",
            "brand_color": s.brand_color or "#E65100",
            # Configurable legal & support links (#1177). Plain env values,
            # shown in the email footer to all recipients. Mirrors what
            # /config exposes to the frontend; the base template renders
            # whatever is set.
            "terms_url": s.terms_url or "",
            "privacy_url": s.privacy_url or "",
            "aup_url": s.aup_url or "",
            "support_url": s.support_url or "",
            "support_email": s.support_email or "",
        }

    def _customize_dir(self) -> str:
        """Return the root customization directory.

        Resolves ``KLANGK_CUSTOMIZE_DIR`` (default ``~/.klangk/custom``).
        """
        return self.settings.customize_dir or str(
            os.path.join(os.path.expanduser("~"), ".klangk", "custom")
        )

    def _template_env(self) -> Environment:
        """Build (and cache) the Jinja environment.

        Uses a ChoiceLoader: a deployer directory (KLANGK_EMAIL_TEMPLATES_DIR)
        is tried first so it shadows the built-ins on a per-file basis; the
        built-in package templates (email_templates/) are the fallback. Both
        {% extends %} and {% include %} resolve through the same chain, so
        overriding just base.html re-brands every email at once. See #1165.
        """
        if self._env is None:
            loaders = []
            user_dir = self.settings.email_templates_dir or ""
            if not user_dir:
                candidate = Path(self._customize_dir()) / "email-templates"
                if candidate.is_dir():
                    user_dir = str(candidate)
            if user_dir:
                path = Path(user_dir)
                if path.is_dir():
                    logger.info("Email templates loaded from %s", path)
                    loaders.append(FileSystemLoader(str(path)))
            loaders.append(PackageLoader("klangk_backend", "email_templates"))
            self._env = Environment(
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
        return self._env

    def render_email(
        self,
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
        env = self._template_env()
        ctx = {
            **self._brand_ctx(),
            "link": link,
            "expiry_hours": int(expiry_hours),
            "invited_by": invited_by,
        }
        subject = (
            env.get_template(f"{event}/subject.txt").render(**ctx).strip()
        )
        text = env.get_template(f"{event}/body.txt").render(**ctx)
        html = env.get_template(f"{event}/body.html").render(**ctx)
        return EmailRender(subject=subject, text=text, html=html)

    def build_multipart(self, to: str, rendered: EmailRender) -> EmailMessage:
        """Assemble a multipart/alternative (text + HTML) message."""
        cfg = self.smtp_config()
        msg = EmailMessage()
        msg["Subject"] = rendered.subject
        msg["From"] = cfg["from_addr"] or cfg["user"] or "noreply@localhost"
        msg["To"] = to
        msg.set_content(rendered.text)
        msg.add_alternative(rendered.html, subtype="html")
        self._set_headers(msg)
        return msg

    async def _send(self, msg: EmailMessage) -> None:
        """Deliver via SMTP (if configured) or local sendmail."""
        if self.use_smtp():
            await self.send_via_smtp(msg)
        else:
            await self.send_via_sendmail(msg)

    async def send_verification_email(
        self, to: str, verification_url: str
    ) -> None:
        """Send a verification email with the given callback URL."""
        rendered = self.render_email(
            "verify",
            link=verification_url,
            expiry_hours=auth.VERIFY_TOKEN_EXPIRE_HOURS,
        )
        await self._send(self.build_multipart(to, rendered))
        logger.info("Verification email sent to %s", to)

    async def send_password_reset_email(self, to: str, reset_url: str) -> None:
        """Send a password reset email with the given callback URL."""
        rendered = self.render_email(
            "reset",
            link=reset_url,
            expiry_hours=auth.RESET_TOKEN_EXPIRE_HOURS,
        )
        await self._send(self.build_multipart(to, rendered))
        logger.info("Password reset email sent to %s", to)

    async def send_invitation_email(
        self, to: str, invite_url: str, invited_by_email: str
    ) -> None:
        """Send an invitation email with the given registration URL."""
        rendered = self.render_email(
            "invite",
            link=invite_url,
            expiry_hours=auth.INVITE_TOKEN_EXPIRE_HOURS,
            invited_by=invited_by_email,
        )
        await self._send(self.build_multipart(to, rendered))
        logger.info(
            "Invitation email sent to %s (invited by %s)", to, invited_by_email
        )
