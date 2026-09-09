"""Explicit opt-in commercial policy. No prices or allowances are defaults."""
import os
import re
import json
from dataclasses import dataclass, field
from functools import lru_cache
from urllib.parse import urlsplit

from .config import ConfigError

METRICS = ("imports", "upload_attempts", "analysis_seconds", "searches",
           "media_bytes", "frames")
ALL_METRICS = (*METRICS, "storage_bytes")
CADENCES = ("monthly", "yearly")
TAX_BEHAVIORS = ("inclusive", "exclusive")
CAPABILITIES = frozenset(
    {"imports", "uploads", "analysis", "searches", "frames", "media"}
)


class BillingProblem(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


@dataclass(frozen=True)
class BillingTier:
    key: str
    name: str
    rank: int
    capabilities: tuple[str, ...]
    limits: dict[str, int]


@dataclass(frozen=True)
class BillingOffer:
    tier: str
    cadence: str
    currency: str
    price_id: str
    unit_amount: int
    tax_behavior: str
    tax_code: str
    sale_enabled: bool


@dataclass(frozen=True)
class BillingCatalog:
    tiers: dict[str, BillingTier] = field(default_factory=dict)
    offers: dict[tuple[str, str, str], BillingOffer] = field(default_factory=dict)
    prices: dict[str, BillingOffer] = field(default_factory=dict)

    def offer(self, tier, cadence, currency, *, for_sale=False):
        result = self.offers.get((tier, cadence, currency))
        if result is None or (for_sale and not result.sale_enabled):
            raise BillingProblem("offer_unavailable", "The selected billing offer is unavailable.", 400)
        return result

    def public_offers(self):
        return [
            {
                "tier": offer.tier, "name": self.tiers[offer.tier].name,
                "rank": self.tiers[offer.tier].rank,
                "cadence": offer.cadence, "currency": offer.currency,
                "unitAmount": offer.unit_amount, "taxBehavior": offer.tax_behavior,
                "capabilities": list(self.tiers[offer.tier].capabilities),
                "limits": dict(self.tiers[offer.tier].limits),
            }
            for offer in sorted(
                self.offers.values(),
                key=lambda item: (item.tier, item.cadence, item.currency),
            )
            if offer.sale_enabled
        ]


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
    catalog: BillingCatalog = field(default_factory=BillingCatalog)
    tax_id_collection: bool = False


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


def _positive(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 10**15:
        raise ConfigError(f"{label} requires a finite positive integer")
    return value


def _catalog():
    raw = os.environ.get("SCENEIT_BILLING_CATALOG", "")
    try:
        document = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ConfigError("SCENEIT_BILLING_CATALOG must be reviewed JSON") from exc
    if not isinstance(document, dict) or document.get("reviewed") is not True:
        raise ConfigError("The complete billing catalog requires explicit review")
    if set(document) != {"reviewed", "tiers", "offers"}:
        raise ConfigError("Billing catalog contains unsupported fields")
    tier_rows, offer_rows = document["tiers"], document["offers"]
    if not isinstance(tier_rows, list) or not 1 <= len(tier_rows) <= 20:
        raise ConfigError("Billing catalog requires a finite tier list")
    if not isinstance(offer_rows, list) or not 1 <= len(offer_rows) <= 200:
        raise ConfigError("Billing catalog requires a finite offer list")
    tiers = {}
    for row in tier_rows:
        if not isinstance(row, dict) or set(row) != {
            "key", "name", "rank", "capabilities", "limits"
        }:
            raise ConfigError("Billing tier fields are invalid")
        key, name = row["key"], row["name"]
        rank, capabilities, limits = row["rank"], row["capabilities"], row["limits"]
        if (
            not isinstance(key, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", key)
            or not isinstance(name, str) or not 1 <= len(name) <= 80
            or isinstance(rank, bool) or not isinstance(rank, int)
            or not 0 <= rank <= 1000
            or not isinstance(capabilities, list) or len(capabilities) > 50
            or any(item not in CAPABILITIES for item in capabilities)
            or len(set(capabilities)) != len(capabilities)
            or not isinstance(limits, dict) or set(limits) != set(ALL_METRICS)
            or key in tiers
        ):
            raise ConfigError("Billing tier is incomplete or invalid")
        checked = {
            metric: _positive(limits[metric], f"tier {key} {metric}")
            for metric in ALL_METRICS
        }
        tiers[key] = BillingTier(key, name, rank, tuple(capabilities), checked)
    if len({tier.rank for tier in tiers.values()}) != len(tiers):
        raise ConfigError("Billing tier ranks must be distinct")
    offers, prices = {}, {}
    for row in offer_rows:
        expected = {
            "tier", "cadence", "currency", "priceId", "unitAmount",
            "taxBehavior", "taxCode", "saleEnabled",
        }
        if not isinstance(row, dict) or set(row) != expected:
            raise ConfigError("Billing offer fields are invalid")
        tier, cadence, currency = row["tier"], row["cadence"], row["currency"]
        price_id, tax_code = row["priceId"], row["taxCode"]
        if (
            tier not in tiers or cadence not in CADENCES
            or not isinstance(currency, str) or not re.fullmatch(r"[a-z]{3}", currency)
            or not isinstance(price_id, str)
            or not re.fullmatch(r"price_[A-Za-z0-9]+", price_id)
            or row["taxBehavior"] not in TAX_BEHAVIORS
            or not isinstance(tax_code, str)
            or not re.fullmatch(r"txcd_[0-9]{8}", tax_code)
            or not isinstance(row["saleEnabled"], bool)
        ):
            raise ConfigError("Billing offer is incomplete or invalid")
        offer = BillingOffer(
            tier, cadence, currency, price_id,
            _positive(row["unitAmount"], f"offer {price_id} unitAmount"),
            row["taxBehavior"], tax_code, row["saleEnabled"],
        )
        key = (tier, cadence, currency)
        if price_id in prices:
            raise ConfigError("Stripe Price mappings must be unique")
        prices[price_id] = offer
        existing = offers.get(key)
        if offer.sale_enabled:
            if existing is not None and existing.sale_enabled:
                raise ConfigError("Only one sale-enabled Price is allowed per offer")
            offers[key] = offer
        elif existing is None:
            offers[key] = offer
    return BillingCatalog(tiers, offers, prices)


@lru_cache(maxsize=1)
def billing_settings():
    if not _flag("SCENEIT_BILLING_ENABLED"):
        return BillingSettings()
    environment = os.environ.get("SCENEIT_BILLING_ENVIRONMENT", "")
    if environment not in ("test", "live"):
        raise ConfigError("SCENEIT_BILLING_ENVIRONMENT must be test or live")
    if environment == "live" and not _flag("SCENEIT_BILLING_LIVE_APPROVED"):
        raise ConfigError("Live billing requires explicit operator approval")
    catalog = _catalog()
    # Kept as a historical compatibility lookup for older quota and recovery
    # code. New purchase/change code always resolves the full catalog tuple.
    prices = {
        f"{offer.tier}:{offer.cadence}:{offer.currency}": offer.price_id
        for offer in catalog.offers.values()
    }
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
        catalog=catalog,
        tax_id_collection=_flag("SCENEIT_STRIPE_TAX_ID_COLLECTION"),
    )


def reset_billing_settings():
    billing_settings.cache_clear()