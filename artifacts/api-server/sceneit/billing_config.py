"""Explicit opt-in commercial policy. No prices or allowances are defaults."""
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

from .config import ConfigError

METRICS = ("imports", "upload_attempts", "analysis_seconds", "searches",
           "media_bytes", "frames")
ALL_METRICS = (*METRICS, "storage_bytes")


class BillingProblem(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


@dataclass(frozen=True)
class BillingSettings:
    enabled: bool = False
    environment: str = "test"
    prices: dict = field(default_factory=dict)
    return_url: str = ""
    portal_configuration: str = ""
    secret_key: str = field(default="", repr=False)
    webhook_secret: str = field(default="", repr=False)
    limits: dict = field(default_factory=dict)
    app_limits: dict = field(default_factory=dict)


def _flag(name, default="false"):
    raw = os.environ.get(name, default)
    if raw not in ("true", "false"):
        raise ConfigError(f"{name} must be true or false")
    return raw == "true"


def _limits(prefix):
    result = {}
    for metric in ALL_METRICS:
        name = f"{prefix}_{metric.upper()}"
        raw = os.environ.get(name, "")
        if not re.fullmatch(r"[1-9][0-9]{0,14}", raw):
            raise ConfigError(f"{name} requires a finite positive integer")
        result[metric] = int(raw)
    return result


@lru_cache(maxsize=1)
def billing_settings():
    if not _flag("SCENEIT_BILLING_ENABLED"):
        return BillingSettings()
    environment = os.environ.get("SCENEIT_BILLING_ENVIRONMENT", "")
    if environment not in ("test", "live"):
        raise ConfigError("SCENEIT_BILLING_ENVIRONMENT must be test or live")
    if environment == "live" and not _flag("SCENEIT_BILLING_LIVE_APPROVED"):
        raise ConfigError("Live billing requires explicit operator approval")
    prices = {plan: os.environ.get(f"SCENEIT_STRIPE_PRICE_{plan.upper()}", "")
              for plan in ("monthly", "yearly")}
    if (any(not re.fullmatch(r"price_[A-Za-z0-9]+", value) for value in prices.values())
            or len(set(prices.values())) != 2):
        raise ConfigError("Two distinct allowlisted recurring Stripe prices are required")
    return_url = os.environ.get("SCENEIT_BILLING_RETURN_URL", "")
    parsed = urlsplit(return_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or "\\" in return_url or any(ord(c) <= 32 for c in return_url)):
        raise ConfigError("SCENEIT_BILLING_RETURN_URL must be a fixed trusted HTTPS URL")
    portal = os.environ.get("SCENEIT_STRIPE_PORTAL_CONFIGURATION", "")
    if not re.fullmatch(r"bpc_[A-Za-z0-9]+", portal):
        raise ConfigError("A restricted Stripe portal configuration is required")
    secret = os.environ.get("STRIPE_SECRET_KEY", "")
    webhook = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret.startswith((f"sk_{environment}_", f"rk_{environment}_")):
        raise ConfigError("Stripe credential does not match the billing environment")
    if not webhook.startswith("whsec_") or len(webhook) < 16:
        raise ConfigError("A Stripe webhook signing credential is required")
    return BillingSettings(
        enabled=True, environment=environment, prices=prices,
        return_url=return_url, portal_configuration=portal,
        secret_key=secret, webhook_secret=webhook,
        limits=_limits("SCENEIT_MEMBER"), app_limits=_limits("SCENEIT_APP"),
    )


def reset_billing_settings():
    billing_settings.cache_clear()