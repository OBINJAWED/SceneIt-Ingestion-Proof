"""Strict, opt-in TLS SMTP transport for billing notifications."""
import os
import re
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from functools import lru_cache
from urllib.parse import urlsplit

from .config import ConfigError


class SMTPDeliveryError(RuntimeError):
    """A classified SMTP failure safe for durable retry decisions."""

    def __init__(self, code, disposition):
        self.code = code
        self.disposition = disposition
        super().__init__(code)


@dataclass(frozen=True)
class NotificationSettings:
    enabled: bool = False
    scheduler_enabled: bool = False
    dunning_enabled: bool = False
    alerts_enabled: bool = False
    host: str = ""
    port: int = 0
    tls_mode: str = ""
    username: str = ""
    password: str = ""
    sender: str = ""
    operator_recipient: str = ""
    message_domain: str = ""
    billing_action_url: str = ""
    schedule_hours: tuple = ()
    max_attempts: int = 0
    timeout_seconds: int = 0
    lease_seconds: int = 0
    alert_cooldown_seconds: int = 0
    pending_age_seconds: int = 0
    stalled_age_seconds: int = 0


def _flag(name):
    value = os.environ.get(name, "false")
    if value not in ("true", "false"):
        raise ConfigError(f"{name} must be true or false")
    return value == "true"


def _approved(name):
    if not _flag(name):
        raise ConfigError(f"{name} requires explicit operator approval")


def _email(name):
    value = os.environ.get(name, "")
    if (len(value) > 254 or "\r" in value or "\n" in value
            or not re.fullmatch(r"[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+", value)):
        raise ConfigError(f"{name} must be one approved mailbox")
    return value


def _integer(name, low, high):
    raw = os.environ.get(name, "")
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return int(raw)


@lru_cache(maxsize=1)
def notification_settings():
    if not _flag("SCENEIT_BILLING_NOTIFICATIONS_ENABLED"):
        return NotificationSettings()
    _approved("SCENEIT_BILLING_NOTIFICATION_SENDER_APPROVED")
    _approved("SCENEIT_BILLING_NOTIFICATION_OPERATOR_APPROVED")
    _approved("SCENEIT_BILLING_NOTIFICATION_SCHEDULE_APPROVED")
    _approved("SCENEIT_BILLING_NOTIFICATION_ACTION_APPROVED")
    if os.environ.get("SCENEIT_BILLING_DUNNING_OWNER", "") != "application":
        raise ConfigError("Application must be the approved dunning campaign owner")
    _approved("SCENEIT_BILLING_DUNNING_OWNER_APPROVED")

    mode = os.environ.get("SCENEIT_BILLING_SMTP_TLS", "")
    if mode not in ("implicit", "starttls"):
        raise ConfigError("SMTP TLS must be implicit or starttls")
    host = os.environ.get("SCENEIT_BILLING_SMTP_HOST", "")
    if (not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host)
            or host.startswith(".") or host.endswith(".")):
        raise ConfigError("SMTP host is invalid")
    username = os.environ.get("SCENEIT_BILLING_SMTP_USERNAME", "")
    password = os.environ.get("SCENEIT_BILLING_SMTP_PASSWORD", "")
    if not username or not password:
        raise ConfigError("SMTP credentials are required")
    domain = os.environ.get("SCENEIT_BILLING_MESSAGE_DOMAIN", "")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", domain):
        raise ConfigError("An approved Message-ID domain is required")
    action = os.environ.get("SCENEIT_BILLING_ACTION_URL", "")
    parsed = urlsplit(action)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path in ("", "/") or "\\" in action
            or any(ord(char) <= 32 for char in action)):
        raise ConfigError("Billing action must be one fixed trusted HTTPS URL")
    raw_schedule = os.environ.get("SCENEIT_BILLING_DUNNING_SCHEDULE_HOURS", "")
    try:
        schedule = tuple(int(item) for item in raw_schedule.split(","))
    except ValueError as exc:
        raise ConfigError("Dunning schedule must contain finite integer hours") from exc
    if (not schedule or len(schedule) > 6 or schedule != tuple(sorted(set(schedule)))
            or schedule[0] < 0 or schedule[-1] > 24 * 30):
        raise ConfigError("Dunning schedule must be unique increasing hours within 30 days")
    scheduler_enabled = _flag("SCENEIT_BILLING_NOTIFICATION_SCHEDULER_ENABLED")
    if scheduler_enabled:
        _approved("SCENEIT_BILLING_NOTIFICATION_ACTIVATION_APPROVED")
    return NotificationSettings(
        enabled=True,
        scheduler_enabled=scheduler_enabled,
        dunning_enabled=_flag("SCENEIT_BILLING_DUNNING_ENABLED"),
        alerts_enabled=_flag("SCENEIT_BILLING_WEBHOOK_ALERTS_ENABLED"),
        host=host,
        port=_integer("SCENEIT_BILLING_SMTP_PORT", 1, 65535),
        tls_mode=mode,
        username=username,
        password=password,
        sender=_email("SCENEIT_BILLING_SMTP_SENDER"),
        operator_recipient=_email("SCENEIT_BILLING_OPERATOR_RECIPIENT"),
        message_domain=domain,
        billing_action_url=action,
        schedule_hours=schedule,
        max_attempts=_integer("SCENEIT_BILLING_NOTIFICATION_MAX_ATTEMPTS", 1, 10),
        timeout_seconds=_integer("SCENEIT_BILLING_SMTP_TIMEOUT_SECONDS", 1, 30),
        lease_seconds=_integer("SCENEIT_BILLING_NOTIFICATION_LEASE_SECONDS", 30, 600),
        alert_cooldown_seconds=_integer(
            "SCENEIT_BILLING_ALERT_COOLDOWN_SECONDS", 300, 604800
        ),
        pending_age_seconds=_integer(
            "SCENEIT_BILLING_PENDING_ALERT_SECONDS", 60, 604800
        ),
        stalled_age_seconds=_integer(
            "SCENEIT_BILLING_STALLED_ALERT_SECONDS", 60, 604800
        ),
    )


def reset_notification_settings():
    notification_settings.cache_clear()


class TLSMailer:
    """Send one message, requiring certificate-verified TLS without fallback."""

    def __init__(self, settings):
        self.settings = settings

    def send(self, recipient, subject, text, message_id, *, deadline=None):
        message = EmailMessage()
        message["From"] = self.settings.sender
        message["To"] = recipient
        message["Subject"] = subject
        message["Message-ID"] = message_id
        message["Auto-Submitted"] = "auto-generated"
        message.set_content(text)
        context = ssl.create_default_context()
        smtp = None
        data_started = False
        deadline = min(
            deadline if deadline is not None else float("inf"),
            time.monotonic() + self.settings.timeout_seconds,
        )

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise socket.timeout("SMTP aggregate deadline elapsed")
            if smtp is not None and getattr(smtp, "sock", None) is not None:
                smtp.sock.settimeout(value)
            return value

        def invoke(function, *args, **kwargs):
            remaining()
            result = function(*args, **kwargs)
            remaining()
            return result

        try:
            smtp_type = (
                smtplib.SMTP_SSL
                if self.settings.tls_mode == "implicit" else smtplib.SMTP
            )
            kwargs = {
                "host": self.settings.host,
                "port": self.settings.port,
                "timeout": min(self.settings.timeout_seconds, remaining()),
            }
            if smtp_type is smtplib.SMTP_SSL:
                kwargs["context"] = context
            smtp = smtp_type(**kwargs)
            if self.settings.tls_mode == "starttls":
                invoke(smtp.ehlo)
                invoke(smtp.starttls, context=context)
                invoke(smtp.ehlo)
            invoke(smtp.login, self.settings.username, self.settings.password)
            code, _ = invoke(smtp.mail, self.settings.sender)
            if code >= 400:
                raise smtplib.SMTPSenderRefused(code, b"", self.settings.sender)
            code, _ = invoke(smtp.rcpt, recipient)
            if code >= 400:
                raise smtplib.SMTPRecipientsRefused({recipient: (code, b"")})
            data_started = True
            code, _ = invoke(smtp.data, message.as_bytes())
            if not 200 <= code < 300:
                raise smtplib.SMTPDataError(code, b"")
        except smtplib.SMTPNotSupportedError as exc:
            raise SMTPDeliveryError("smtp_tls_required", "permanent") from exc
        except smtplib.SMTPResponseException as exc:
            disposition = "transient" if 400 <= exc.smtp_code < 500 else "permanent"
            raise SMTPDeliveryError(f"smtp_{exc.smtp_code}", disposition) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            codes = [value[0] for value in exc.recipients.values()]
            disposition = "transient" if codes and all(400 <= c < 500 for c in codes) else "permanent"
            raise SMTPDeliveryError("smtp_recipient_rejected", disposition) from exc
        except (smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError,
                ConnectionError, OSError, ssl.SSLError) as exc:
            disposition = "ambiguous" if data_started else "transient"
            raise SMTPDeliveryError("smtp_transport", disposition) from exc
        finally:
            if smtp is not None:
                try:
                    # Do not add an unbounded QUIT round trip after the delivery
                    # deadline. Closing the already-bounded socket is sufficient.
                    smtp.close()
                except OSError:
                    pass
