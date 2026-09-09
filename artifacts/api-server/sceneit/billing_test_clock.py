"""Approval-gated Stripe Test Clock harness.

This module is verification tooling, not application runtime code.  It never
changes SceneIt's notion of time.  An authorized sandbox verifier supplies the
application/database convergence hook and decides when a checkpoint may pass.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Protocol

import stripe
import psycopg
from psycopg.rows import dict_row

from .billing_provider import _BoundedStripeHTTPClient


OPT_IN_ENV = "SCENEIT_STRIPE_TEST_CLOCK_APPROVED"
TEST_DATABASE_ENV = "SCENEIT_TEST_CLOCK_DATABASE_URL"
MAX_SCENARIOS = 20
MAX_REQUESTS = 100
MAX_POLL_ATTEMPTS = 30
MAX_RUN_SECONDS = 900


class TestClockError(RuntimeError):
    """A safe operator-facing harness failure."""


@dataclass(frozen=True)
class Scenario:
    key: str
    mode: str
    coverage: tuple[str, ...]
    notes: str


SCENARIOS = (
    Scenario("monthly_renewal", "clock", ("renewal",), "Successful monthly renewal."),
    Scenario(
        "renewal_failure_recovery", "clock",
        ("failed-payment", "recovery", "expired-card"),
        "Failure must not extend coverage; card update alone is not recovery.",
    ),
    Scenario(
        "annual_monthly_allowances", "clock", ("annual", "monthly-allowance"),
        "Annual coverage retains application monthly windows and consumption.",
    ),
    Scenario(
        "paid_upgrade", "clock", ("tier-upgrade", "proration"),
        "Same-cadence upgrade is effective only after its paid invoice.",
    ),
    Scenario(
        "scheduled_downgrade", "clock", ("tier-downgrade",),
        "Downgrade is effective at renewal and does not reset usage.",
    ),
    Scenario(
        "scheduled_cadence_change", "clock", ("cadence-change",),
        "Cadence change is effective at subscription renewal.",
    ),
    Scenario(
        "cancel_and_expire", "clock", ("cancellation", "coverage-expiry"),
        "Period-end cancellation preserves, then ends, paid access.",
    ),
    Scenario(
        "tax_inclusive", "clock", ("tax", "inclusive"),
        "Requires a reviewed inclusive-tax test offer and jurisdiction.",
    ),
    Scenario(
        "tax_exclusive_or_zero", "clock", ("tax", "exclusive", "zero-tax"),
        "Requires reviewed exclusive-tax and legitimate zero-tax fixtures.",
    ),
    Scenario(
        "currency_offer", "clock", ("currency",),
        "Requires an approved non-default-currency offer; no FX conversion.",
    ),
    Scenario(
        "refund_companion", "companion", ("full-refund", "partial-refund"),
        "Refund timing is not Test Clock controlled; run as a separate test mutation.",
    ),
    Scenario(
        "replay_inbox_companion", "companion", ("webhook-replay", "inbox"),
        "Replay is bounded; inbox arrival is a designated-recipient manual observation.",
    ),
)
SCENARIO_MAP = {item.key: item for item in SCENARIOS}

# Actions are deliberately application/provider adapter hooks.  Offsets are from
# the scenario's original frozen time, never from application/system time.
SCENARIO_PHASES = {
    "monthly_renewal": (
        ("provision_monthly_paid", 0), ("renewal_paid", 32),
    ),
    "renewal_failure_recovery": (
        ("provision_monthly_paid", 0), ("set_expired_payment_method", 0),
        ("renewal_failed", 32), ("recover_payment_method_and_pay", 32),
    ),
    "annual_monthly_allowances": (
        ("provision_yearly_paid_and_consume", 0),
        ("first_allowance_boundary", 32), ("second_allowance_boundary", 63),
    ),
    "paid_upgrade": (
        ("provision_base_paid", 0), ("confirm_upgrade", 10),
        ("pay_upgrade_invoice", 10),
    ),
    "scheduled_downgrade": (
        ("provision_high_tier_and_consume", 0), ("schedule_downgrade", 5),
        ("downgrade_renewal", 32),
    ),
    "scheduled_cadence_change": (
        ("provision_monthly_paid", 0), ("schedule_yearly_cadence", 5),
        ("cadence_renewal", 32),
    ),
    "cancel_and_expire": (
        ("provision_monthly_paid", 0), ("cancel_at_period_end", 5),
        ("coverage_expired", 32),
    ),
    "tax_inclusive": (
        ("provision_inclusive_tax_offer", 0), ("inclusive_tax_renewal", 32),
    ),
    "tax_exclusive_or_zero": (
        ("provision_exclusive_tax_offer", 0), ("exclusive_tax_renewal", 32),
        ("set_zero_tax_location", 33), ("zero_tax_renewal", 63),
    ),
    "currency_offer": (
        ("provision_approved_currency_offer", 0), ("currency_renewal", 32),
    ),
}
RECEIPT_REQUIRED_PHASES = frozenset({
    "provision_monthly_paid", "provision_yearly_paid_and_consume",
    "provision_base_paid", "provision_high_tier_and_consume",
    "provision_inclusive_tax_offer", "provision_exclusive_tax_offer",
    "provision_approved_currency_offer", "renewal_paid", "renewal_failed",
    "recover_payment_method_and_pay", "confirm_upgrade", "schedule_downgrade",
    "schedule_yearly_cadence", "cancel_at_period_end", "downgrade_renewal",
    "cadence_renewal", "coverage_expired", "inclusive_tax_renewal",
    "exclusive_tax_renewal", "zero_tax_renewal", "currency_renewal",
})


def scenario_matrix():
    """Return a fresh scorecard; fixtures never imply sandbox verification."""
    return [
        {
            "scenario": item.key,
            "mode": item.mode,
            "coverage": list(item.coverage),
            "status": "unverified",
            "notes": item.notes,
        }
        for item in SCENARIOS
    ]


def _valid_tax_location(value):
    return (
        value is None
        or isinstance(value, dict)
        and set(value) == {"country", "postalCode"}
        and isinstance(value["country"], str)
        and re.fullmatch(r"[A-Z]{2}", value["country"])
        and isinstance(value["postalCode"], str)
        and re.fullmatch(r"[A-Za-z0-9 -]{2,12}", value["postalCode"])
    )


@dataclass(frozen=True)
class Approval:
    run_id: str
    database_namespace: str
    frozen_time: int
    scenarios: tuple[str, ...]
    max_requests: int
    max_poll_attempts: int
    max_run_seconds: int
    fixtures: dict

    @classmethod
    def load(cls, path):
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TestClockError("approval_manifest_invalid") from exc
        expected = {
            "approved", "runId", "databaseNamespace", "frozenTime", "scenarios",
            "maxRequests", "maxPollAttempts", "maxRunSeconds",
        }
        if (
            not isinstance(raw, dict)
            or set(raw) not in (expected, expected | {"fixtures"})
            or raw["approved"] is not True
        ):
            raise TestClockError("approval_manifest_invalid")
        scenarios = raw["scenarios"]
        if (
            not isinstance(raw["runId"], str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{7,63}", raw["runId"])
            or not isinstance(raw["databaseNamespace"], str)
            or not re.fullmatch(r"billing_clock_[a-z0-9_]{4,48}", raw["databaseNamespace"])
            or not isinstance(raw["frozenTime"], int)
            or not 1_577_836_800 <= raw["frozenTime"] <= 4_102_444_800
            or not isinstance(scenarios, list)
            or not 1 <= len(scenarios) <= MAX_SCENARIOS
            or len(set(scenarios)) != len(scenarios)
            or any(item not in SCENARIO_MAP for item in scenarios)
        ):
            raise TestClockError("approval_manifest_invalid")
        bounds = (
            ("maxRequests", 1, MAX_REQUESTS),
            ("maxPollAttempts", 1, MAX_POLL_ATTEMPTS),
            ("maxRunSeconds", 1, MAX_RUN_SECONDS),
        )
        for name, minimum, maximum in bounds:
            value = raw[name]
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise TestClockError("approval_manifest_invalid")
        fixtures = raw.get("fixtures", {})
        if not isinstance(fixtures, dict) or set(fixtures) - set(scenarios):
            raise TestClockError("approval_manifest_invalid")
        checked_fixtures = {}
        fixture_fields = {
            "ownerId", "baseTier", "targetTier", "cadence",
            "targetCadence", "currency", "taxLocation", "zeroTaxLocation",
        }
        for scenario, row in fixtures.items():
            if not isinstance(row, dict) or set(row) != fixture_fields:
                raise TestClockError("approval_manifest_invalid")
            if (
                not isinstance(row["ownerId"], str) or not 1 <= len(row["ownerId"]) <= 255
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", row["baseTier"])
                or row["targetTier"] is not None and not re.fullmatch(
                    r"[a-z][a-z0-9_]{0,31}", row["targetTier"])
                or row["cadence"] not in ("monthly", "yearly")
                or row["targetCadence"] not in (None, "monthly", "yearly")
                or not re.fullmatch(r"[a-z]{3}", row["currency"])
                or any(not _valid_tax_location(row[name])
                       for name in ("taxLocation", "zeroTaxLocation"))
            ):
                raise TestClockError("approval_manifest_invalid")
            checked_fixtures[scenario] = ScenarioFixture(
                row["ownerId"], row["baseTier"], row["targetTier"],
                row["cadence"], row["targetCadence"], row["currency"],
                row["taxLocation"], row["zeroTaxLocation"],
            )
        return cls(
            raw["runId"], raw["databaseNamespace"], raw["frozenTime"],
            tuple(scenarios), raw["maxRequests"], raw["maxPollAttempts"],
            raw["maxRunSeconds"], checked_fixtures,
        )


def require_execution_guards(approval, *, environ=None):
    """Fail closed before constructing a provider client."""
    env = os.environ if environ is None else environ
    if env.get(OPT_IN_ENV) != "true":
        raise TestClockError("explicit_test_clock_approval_required")
    key = env.get("STRIPE_SECRET_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_")):
        raise TestClockError("test_mode_stripe_key_required")
    test_database = env.get(TEST_DATABASE_ENV, "")
    production_database = env.get("DATABASE_URL", "")
    if not test_database or test_database == production_database:
        raise TestClockError("isolated_test_database_required")
    if approval.database_namespace not in test_database:
        raise TestClockError("approved_database_namespace_missing")
    return key


def isolated_connection_factory(database_url, database_namespace):
    """Build a connection factory that cannot consult normal app configuration."""
    if not database_url or database_namespace not in database_url:
        raise TestClockError("isolated_test_database_required")

    @contextmanager
    def connect():
        conn = psycopg.connect(
            database_url, row_factory=dict_row, connect_timeout=5,
            application_name=f"sceneit-test-clock-{database_namespace}"[:63],
        )
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return connect


def validate_connection_factory(factory, approval, *, environ=None):
    from .billing_time import validate_isolated_database
    try:
        with factory() as conn:
            validate_isolated_database(
                conn, approval.database_namespace, environ=environ,
            )
    except Exception as exc:
        raise TestClockError("isolated_test_database_validation_failed") from exc


@contextmanager
def isolated_core_connections(factory):
    """Process-local dependency injection; never mutates runtime environment."""
    from . import billing, quota
    prior_billing, prior_quota = billing.connection, quota.connection
    billing.connection = quota.connection = factory
    try:
        yield
    finally:
        billing.connection, quota.connection = prior_billing, prior_quota


def _value(value, name, default=None):
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _id(value, prefix):
    if not isinstance(value, str) or not value.startswith(prefix) or len(value) > 255:
        raise TestClockError("invalid_test_provider_response")
    return value


class StripeTestClockProvider:
    """Small Stripe SDK adapter with one aggregate deadline and request budget."""

    def __init__(
        self, secret_key, *, max_requests, max_seconds, transport=None,
        base_address=None, monotonic=time.monotonic, sleep=time.sleep,
    ):
        if not secret_key.startswith(("sk_test_", "rk_test_")):
            raise TestClockError("test_mode_stripe_key_required")
        self._deadline = monotonic() + max_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._remaining_requests = max_requests
        http_client = _BoundedStripeHTTPClient(self._deadline, transport=transport)
        options = {"max_network_retries": 0, "http_client": http_client}
        if base_address:
            options["base_addresses"] = {"api": base_address}
        self._client = stripe.StripeClient(secret_key, **options).v1

    def _call(self, function, *args, **kwargs):
        if self._remaining_requests <= 0:
            raise TestClockError("test_clock_request_budget_exhausted")
        if self._monotonic() >= self._deadline:
            raise TestClockError("test_clock_deadline_exceeded")
        self._remaining_requests -= 1
        try:
            return function(*args, **kwargs)
        except TestClockError:
            raise
        except Exception as exc:
            raise TestClockError("test_clock_provider_failed") from exc

    def verify_test_environment(self):
        balance = self._call(self._client.balance.retrieve)
        if _value(balance, "livemode") is not False:
            raise TestClockError("live_stripe_account_rejected")

    def create_clock(self, *, name, frozen_time, idempotency_key):
        result = self._call(
            self._client.test_helpers.test_clocks.create,
            {"name": name, "frozen_time": frozen_time},
            options={"idempotency_key": idempotency_key},
        )
        if _value(result, "livemode") is not False:
            raise TestClockError("live_stripe_object_rejected")
        return _id(_value(result, "id"), "clock_")

    def retrieve_clock(self, clock_id):
        result = self._call(self._client.test_helpers.test_clocks.retrieve, clock_id)
        if _value(result, "livemode") is not False:
            raise TestClockError("live_stripe_object_rejected")
        return result

    def create_customer(self, *, clock_id, run_id, scenario, idempotency_key):
        result = self._call(
            self._client.customers.create,
            {
                "test_clock": clock_id,
                "metadata": {
                    "sceneit_test_clock_run": run_id,
                    "sceneit_test_clock_scenario": scenario,
                },
            },
            options={"idempotency_key": idempotency_key},
        )
        if _value(result, "livemode") is not False:
            raise TestClockError("live_stripe_object_rejected")
        if _value(result, "test_clock") != clock_id:
            raise TestClockError("clock_customer_isolation_failed")
        return _id(_value(result, "id"), "cus_")

    def configure_payment_method(self, customer_id, payment_method):
        if payment_method not in (
            "pm_card_visa", "pm_card_chargeCustomerFail",
            "pm_card_authenticationRequired",
        ):
            raise TestClockError("unapproved_test_payment_method")
        self._call(
            self._client.payment_methods.attach, payment_method,
            {"customer": customer_id},
        )
        self._call(self._client.customers.update, customer_id, {
            "invoice_settings": {"default_payment_method": payment_method},
        })

    def configure_tax_location(self, customer_id, location):
        if location is None:
            return
        self._call(self._client.customers.update, customer_id, {
            "address": {
                "country": location["country"],
                "postal_code": location["postalCode"],
            },
        })

    def create_subscription(
        self, *, customer_id, owner_id, price_id, idempotency_key,
    ):
        result = self._call(
            self._client.subscriptions.create, {
                "customer": customer_id,
                "items": [{"price": price_id, "quantity": 1}],
                "metadata": {"sceneit_owner_id": owner_id},
                "automatic_tax": {"enabled": True},
                "payment_behavior": "error_if_incomplete",
            }, options={"idempotency_key": idempotency_key},
        )
        if _value(result, "livemode") is not False:
            raise TestClockError("live_stripe_object_rejected")
        return _id(_value(result, "id"), "sub_")

    def cancel_at_period_end(self, subscription_id):
        self._call(self._client.subscriptions.update, subscription_id, {
            "cancel_at_period_end": True,
        })

    def pay_latest_open_invoice(self, customer_id):
        result = self._call(self._client.invoices.list, {
            "customer": customer_id, "status": "open", "limit": 2,
        })
        rows = list(_value(result, "data", []))
        if bool(_value(result, "has_more")) or len(rows) != 1:
            raise TestClockError("recoverable_invoice_not_unique")
        invoice_id = _id(_value(rows[0], "id"), "in_")
        self._call(
            self._client.invoices.pay, invoice_id,
            {"paid_out_of_band": False},
            options={"idempotency_key": f"sceneit-clock-pay-{invoice_id}"},
        )
        return invoice_id

    def advance(self, clock_id, frozen_time, *, max_poll_attempts):
        result = self._call(
            self._client.test_helpers.test_clocks.advance,
            clock_id, {"frozen_time": frozen_time},
        )
        if _value(result, "livemode") is not False:
            raise TestClockError("live_stripe_object_rejected")
        for attempt in range(max_poll_attempts):
            current = self.retrieve_clock(clock_id)
            status = _value(current, "status")
            if status == "ready" and _value(current, "frozen_time") == frozen_time:
                return current
            if status not in ("advancing", "ready"):
                raise TestClockError("test_clock_terminal_state")
            if attempt + 1 < max_poll_attempts:
                self._sleep(min(1 + attempt / 4, 3))
        raise TestClockError("test_clock_poll_limit_exceeded")

    def delete_customer(self, customer_id):
        self._call(self._client.customers.delete, customer_id)

    def delete_clock(self, clock_id):
        self._call(self._client.test_helpers.test_clocks.delete, clock_id)


class VerificationHook(Protocol):
    """Mutation boundary owned by the authorized hosted-payments verifier."""

    def provision(self, scenario: str, customer_id: str, clock_id: str) -> None: ...

    def perform(
        self, scenario: str, action: str, provider_time: int,
        customer_id: str, clock_id: str,
    ) -> None: ...


@dataclass(frozen=True)
class ScenarioFixture:
    """Approved non-secret identities and expected commercial dimensions."""

    owner_id: str
    base_tier: str
    target_tier: str | None
    cadence: str
    target_cadence: str | None
    currency: str
    tax_location: dict | None = None
    zero_tax_location: dict | None = None


class StandardScenarioMutations:
    """Concrete approved Stripe/app mutation plan; it never decides pass/fail."""

    def __init__(
        self, approval, provider, fixtures, *, settings=None,
        connection_factory,
    ):
        if settings is None:
            from .billing_config import billing_settings
            settings = billing_settings()
        if not settings.enabled or settings.environment != "test":
            raise TestClockError("test_billing_configuration_required")
        self.approval = approval
        self.provider = provider
        self.fixtures = fixtures
        self.settings = settings
        self.connection_factory = connection_factory
        self.subscriptions = {}

    def _offer(self, fixture, *, target=False):
        tier = fixture.target_tier if target else fixture.base_tier
        cadence = (
            fixture.target_cadence
            if target and fixture.target_cadence else fixture.cadence
        )
        try:
            return self.settings.catalog.offer(
                tier, cadence, fixture.currency, for_sale=True,
            )
        except Exception as exc:
            raise TestClockError("approved_fixture_offer_missing") from exc

    def provision(self, scenario, customer_id, clock_id):
        fixture = self.fixtures.get(scenario)
        if not isinstance(fixture, ScenarioFixture):
            raise TestClockError("approved_fixture_missing")
        if fixture.tax_location is None:
            raise TestClockError("approved_tax_location_required")
        with self.connection_factory() as conn:
            existing = conn.execute(
                "SELECT customer_id,environment FROM sceneit_billing_accounts "
                "WHERE owner_id=%s FOR UPDATE", (fixture.owner_id,),
            ).fetchone()
            if existing and (
                existing["customer_id"] not in (None, customer_id)
                or existing["environment"] != "test"
            ):
                raise TestClockError("clock_customer_owner_mismatch")
            conn.execute(
                "INSERT INTO sceneit_billing_accounts"
                "(owner_id,environment,customer_id,customer_attempt_state) "
                "VALUES (%s,'test',%s,'created') ON CONFLICT(owner_id) DO UPDATE "
                "SET customer_id=EXCLUDED.customer_id,"
                "customer_attempt_state='created' "
                "WHERE sceneit_billing_accounts.environment='test' "
                "AND sceneit_billing_accounts.customer_id IS NULL",
                (fixture.owner_id, customer_id),
            )
        self.provider.configure_tax_location(customer_id, fixture.tax_location)
        self.provider.configure_payment_method(customer_id, "pm_card_visa")
        self.subscriptions[scenario] = self.provider.create_subscription(
            customer_id=customer_id, owner_id=fixture.owner_id,
            price_id=self._offer(fixture).price_id,
            idempotency_key=_stable_key(
                self.approval.run_id, scenario, "subscription"
            ),
        )

    def perform(self, scenario, action, provider_time, customer_id, clock_id):
        fixture = self.fixtures[scenario]
        if action == "provision_yearly_paid_and_consume":
            self._consume_annual_fixture(scenario, fixture, provider_time)
            return
        if action.startswith("provision_") or action in (
            "renewal_paid", "renewal_failed", "downgrade_renewal",
            "cadence_renewal", "coverage_expired", "inclusive_tax_renewal",
            "exclusive_tax_renewal", "currency_renewal",
            "first_allowance_boundary", "second_allowance_boundary",
            "zero_tax_renewal",
        ):
            return
        if action == "set_expired_payment_method":
            # Stripe has no attachable already-expired PM. This documented
            # decline fixture drives the same renewal-failure recovery state;
            # hosted expired-card wording remains a companion assertion.
            self.provider.configure_payment_method(
                customer_id, "pm_card_chargeCustomerFail"
            )
            return
        if action == "recover_payment_method_and_pay":
            self.provider.configure_payment_method(customer_id, "pm_card_visa")
            self.provider.pay_latest_open_invoice(customer_id)
            return
        if action == "cancel_at_period_end":
            self.provider.cancel_at_period_end(self.subscriptions[scenario])
            return
        if action == "set_zero_tax_location":
            if fixture.zero_tax_location is None:
                raise TestClockError("approved_zero_tax_location_required")
            self.provider.configure_tax_location(
                customer_id, fixture.zero_tax_location
            )
            return
        if action in (
            "confirm_upgrade", "pay_upgrade_invoice", "schedule_downgrade",
            "schedule_yearly_cadence",
        ):
            self._application_change(
                scenario, action, fixture, provider_time,
            )
            return
        raise TestClockError("unsupported_scenario_action")

    def _consume_annual_fixture(self, scenario, fixture, provider_time):
        from .billing_config import BillingProblem
        from .billing_time import isolated_verification_time
        from .quota import reserve
        deadline = time.monotonic() + self.approval.max_run_seconds
        last = None
        for attempt in range(self.approval.max_poll_attempts):
            try:
                with self.connection_factory() as conn:
                    with isolated_verification_time(
                        conn, datetime.fromtimestamp(provider_time, timezone.utc),
                        self.approval.database_namespace,
                    ):
                        reserve(
                            conn, fixture.owner_id,
                            _stable_key(self.approval.run_id, scenario, "usage"),
                            {"searches": 1},
                        )
                return
            except BillingProblem as exc:
                last = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(min(1 + attempt / 4, 3))
        raise TestClockError("annual_fixture_coverage_not_converged") from last

    def _application_change(self, scenario, action, fixture, provider_time):
        # Preview/confirm use the production business functions against the
        # isolated database and configured Stripe test adapter.
        if action in ("pay_upgrade_invoice",):
            return  # Payment is created/confirmed by confirm_change.
        from .billing import confirm_change, preview_change
        from .billing_time import isolated_verification_time
        target = self._offer(fixture, target=True)
        preview_key = _stable_uuid(self.approval.run_id, scenario, "preview")
        with self.connection_factory() as validation_conn:
            with isolated_verification_time(
                validation_conn,
                datetime.fromtimestamp(provider_time, timezone.utc),
                self.approval.database_namespace,
            ), isolated_core_connections(self.connection_factory):
                preview = preview_change(
                    fixture.owner_id, target.tier, target.cadence,
                    target.currency, preview_key, purchase_authorized=True,
                )
                confirm_change(
                    fixture.owner_id, preview["previewId"],
                    _stable_uuid(self.approval.run_id, scenario, "confirm"),
                    purchase_authorized=True,
                )


class IsolatedDatabaseVerifier:
    """Concrete authoritative receipt/coverage/status convergence verifier."""

    def __init__(
        self, approval, fixtures, *, connection_factory=None,
        status_function=None, sleep=time.sleep, monotonic=time.monotonic,
        environ=None,
    ):
        if connection_factory is None:
            raise TestClockError("isolated_connection_factory_required")
        if status_function is None:
            from .billing import status
            status_function = status
        self.approval = approval
        self.fixtures = fixtures
        self.connection_factory = connection_factory
        self.status_function = status_function
        self.sleep = sleep
        self.monotonic = monotonic
        self.environ = environ
        self._annual_window = {}
        self._receipt_baselines = {}

    def begin_phase(self, scenario, phase, customer_id):
        with self.connection_factory() as conn:
            row = conn.execute(
                "SELECT received_at,event_id FROM sceneit_billing_events "
                "WHERE customer_id=%s ORDER BY received_at DESC,event_id DESC LIMIT 1",
                (customer_id,),
            ).fetchone()
        self._receipt_baselines[(scenario, phase)] = (
            (row["received_at"], row["event_id"]) if row else None
        )

    def checkpoint(
        self, scenario, phase, provider_time, customer_id,
        *, max_attempts, deadline,
    ):
        fixture = self.fixtures.get(scenario)
        if not isinstance(fixture, ScenarioFixture):
            return {"status": "blocked", "code": "approved_fixture_missing"}
        from .billing_time import isolated_verification_time
        instant = datetime.fromtimestamp(provider_time, timezone.utc)
        last_code = "database_convergence_pending"
        for attempt in range(max_attempts):
            if self.monotonic() >= deadline:
                break
            try:
                with self.connection_factory() as conn:
                    with isolated_verification_time(
                        conn, instant, self.approval.database_namespace,
                        environ=self.environ,
                    ), isolated_core_connections(self.connection_factory):
                        snapshot = self.status_function(fixture.owner_id)
                        account = conn.execute(
                            "SELECT customer_id,allowance_anchor "
                            "FROM sceneit_billing_accounts WHERE owner_id=%s",
                            (fixture.owner_id,),
                        ).fetchone()
                        baseline = self._receipt_baselines.get((scenario, phase))
                        receipt = conn.execute(
                            "SELECT event_id,state,processed_at FROM "
                            "sceneit_billing_events WHERE customer_id=%s "
                            "AND state='completed' AND processed_at IS NOT NULL "
                            + (
                                "AND (received_at,event_id)>(%s,%s) "
                                if baseline and phase in RECEIPT_REQUIRED_PHASES
                                else ""
                            ) +
                            "ORDER BY processed_at DESC,event_id DESC LIMIT 1",
                            (
                                (customer_id, baseline[0], baseline[1])
                                if baseline and phase in RECEIPT_REQUIRED_PHASES
                                else (customer_id,)
                            ),
                        ).fetchone()
                        coverage = conn.execute(
                            "SELECT tier_key,cadence,currency,subtotal,tax,total,"
                            "amount_paid,tax_behavior FROM sceneit_paid_coverage "
                            "WHERE owner_id=%s AND NOT reversed AND starts_at<=%s "
                            "AND ends_at>%s ORDER BY tier_rank DESC NULLS LAST,"
                            "ends_at DESC LIMIT 1",
                            (fixture.owner_id, instant, instant),
                        ).fetchone()
                    last_code = self._assert_snapshot(
                        scenario, phase, fixture, customer_id, snapshot,
                        account, receipt, coverage,
                    )
                if last_code is None:
                    return {
                        "status": "passed",
                        "code": "authoritative_database_assertions_passed",
                    }
            except Exception as exc:
                # Configuration/identity failures are not convergence delays.
                if exc.__class__.__name__ == "BillingTimeError":
                    return {"status": "blocked", "code": "database_identity_rejected"}
                last_code = "database_query_failed"
            if attempt + 1 < max_attempts:
                self.sleep(min(1 + attempt / 4, 3))
        return {"status": "failed", "code": last_code}

    def _assert_snapshot(
        self, scenario, phase, fixture, customer_id, snapshot,
        account, receipt, coverage,
    ):
        if not account or account["customer_id"] != customer_id:
            return "clock_customer_owner_mismatch"
        if (
            not receipt or receipt["state"] != "completed"
            or receipt["processed_at"] is None
        ):
            return "verified_webhook_receipt_pending"
        inactive = phase in ("renewal_failed", "coverage_expired")
        if inactive:
            if snapshot["membership"] != "inactive" or coverage is not None:
                return "coverage_should_be_inactive"
        elif snapshot["membership"] != "active" or coverage is None:
            return "coverage_should_be_active"
        if phase == "renewal_failed" and snapshot["paymentProblem"] is None:
            return "payment_problem_missing"
        if phase == "recover_payment_method_and_pay" and snapshot["paymentProblem"] is not None:
            return "payment_problem_not_resolved"
        if phase == "cancel_at_period_end" and not snapshot["cancelAtPeriodEnd"]:
            return "period_end_cancellation_missing"
        if phase in ("schedule_downgrade", "schedule_yearly_cadence"):
            if snapshot["pendingChange"] is None:
                return "scheduled_change_missing"
        expected_tier = fixture.base_tier
        if phase in (
            "confirm_upgrade", "pay_upgrade_invoice", "downgrade_renewal"
        ) and fixture.target_tier:
            expected_tier = fixture.target_tier
        if not inactive and snapshot["effectiveTier"] != expected_tier:
            return "effective_tier_mismatch"
        expected_cadence = fixture.cadence
        if phase == "cadence_renewal" and fixture.target_cadence:
            expected_cadence = fixture.target_cadence
        if not inactive and snapshot["cadence"] != expected_cadence:
            return "effective_cadence_mismatch"
        if not inactive and snapshot["currency"] != fixture.currency:
            return "effective_currency_mismatch"
        if scenario.startswith("tax_") and coverage is not None:
            facts = (
                coverage["subtotal"], coverage["tax"], coverage["total"],
                coverage["amount_paid"],
            )
            if (
                any(type(value) is not int or value < 0 for value in facts)
                or coverage["total"] != coverage["amount_paid"]
                or coverage["tax_behavior"] not in ("inclusive", "exclusive")
            ):
                return "authoritative_tax_facts_invalid"
            if scenario == "tax_inclusive" and coverage["tax_behavior"] != "inclusive":
                return "inclusive_tax_behavior_mismatch"
            if (
                scenario == "tax_exclusive_or_zero"
                and coverage["tax_behavior"] != "exclusive"
            ):
                return "exclusive_tax_behavior_mismatch"
            if phase == "exclusive_tax_renewal" and coverage["tax"] <= 0:
                return "exclusive_tax_fixture_not_positive"
            if phase == "zero_tax_renewal" and coverage["tax"] != 0:
                return "zero_tax_fixture_not_zero"
        if scenario == "annual_monthly_allowances":
            usage = snapshot.get("usage") or {}
            window = usage.get("windowStart")
            if phase == "provision_yearly_paid_and_consume":
                if not window or not any(
                    metric["used"] > 0
                    for metric in usage.get("metrics", {}).values()
                ):
                    return "annual_consumption_fixture_missing"
                self._annual_window[scenario] = window
            elif window == self._annual_window.get(scenario):
                return "annual_monthly_window_not_advanced"
        return None


def _stable_key(run_id, scenario, operation):
    digest = hashlib.sha256(f"{run_id}:{scenario}:{operation}".encode()).hexdigest()
    return f"sceneit-clock-{digest}"


def _stable_uuid(run_id, scenario, operation):
    digest = hashlib.sha256(f"{run_id}:{scenario}:{operation}".encode()).hexdigest()
    return str(__import__("uuid").UUID(digest[:32]))


def _write_private_json(path, document):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, sort_keys=True, indent=2)
        stream.write("\n")
    os.replace(temporary, target)
    os.chmod(target, 0o600)


class TestClockHarness:
    def __init__(self, approval, provider, manifest_path):
        self.approval = approval
        self.provider = provider
        self.manifest_path = Path(manifest_path)

    def _load_or_new(self):
        if self.manifest_path.exists():
            try:
                result = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise TestClockError("resource_manifest_invalid") from exc
            if result.get("runId") != self.approval.run_id:
                raise TestClockError("resource_manifest_run_mismatch")
            return result
        return {
            "version": 1, "runId": self.approval.run_id,
            "databaseNamespace": self.approval.database_namespace,
            "resources": {}, "results": scenario_matrix(),
            "cleanup": {"state": "required"},
        }

    def prepare(self):
        manifest = self._load_or_new()
        self.provider.verify_test_environment()
        for scenario in self.approval.scenarios:
            definition = SCENARIO_MAP[scenario]
            if definition.mode != "clock":
                continue
            resource = manifest["resources"].get(scenario)
            if resource and resource.get("state") in ("prepared", "executed"):
                continue
            if resource is None:
                clock_id = self.provider.create_clock(
                    name=f"sceneit-{self.approval.run_id}-{scenario}"[:80],
                    frozen_time=self.approval.frozen_time,
                    idempotency_key=_stable_key(
                        self.approval.run_id, scenario, "clock"
                    ),
                )
                resource = {
                    "clockId": clock_id, "customerId": None,
                    "state": "clock_created",
                }
                manifest["resources"][scenario] = resource
                _write_private_json(self.manifest_path, manifest)
            if not resource.get("customerId"):
                resource["customerId"] = self.provider.create_customer(
                    clock_id=resource["clockId"], run_id=self.approval.run_id,
                    scenario=scenario, idempotency_key=_stable_key(
                        self.approval.run_id, scenario, "customer"
                    ),
                )
            resource["state"] = "prepared"
            _write_private_json(self.manifest_path, manifest)
        return manifest

    def run_scenario(
        self, scenario, actions: VerificationHook,
        verifier: IsolatedDatabaseVerifier,
    ):
        """Run one approved clock plan through an authorized integration hook.

        The hook must use only the disposable database and approved test
        resources.  It is responsible for real signed webhook convergence and
        authoritative application assertions; the harness does not forge
        events, alter database time, or infer success from provider readiness.
        """
        if scenario not in self.approval.scenarios:
            raise TestClockError("scenario_not_approved")
        definition = SCENARIO_MAP[scenario]
        if definition.mode != "clock":
            raise TestClockError("companion_scenario_requires_separate_execution")
        manifest = self._load_or_new()
        resource = manifest.get("resources", {}).get(scenario)
        if not resource or resource.get("state") == "deleted":
            raise TestClockError("scenario_resources_not_prepared")
        clock_id, customer_id = resource["clockId"], resource["customerId"]
        deadline = time.monotonic() + self.approval.max_run_seconds
        first_action = SCENARIO_PHASES[scenario][0][0]
        verifier.begin_phase(scenario, first_action, customer_id)
        actions.provision(scenario, customer_id, clock_id)
        phases = []
        previous_time = self.approval.frozen_time
        for index, (action, days) in enumerate(SCENARIO_PHASES[scenario]):
            if index:
                verifier.begin_phase(scenario, action, customer_id)
            target = self.approval.frozen_time + days * 86_400
            if target > previous_time:
                self.provider.advance(
                    clock_id, target,
                    max_poll_attempts=self.approval.max_poll_attempts,
                )
                previous_time = target
            actions.perform(scenario, action, target, customer_id, clock_id)
            result = verifier.checkpoint(
                scenario, action, target, customer_id,
                max_attempts=self.approval.max_poll_attempts,
                deadline=deadline,
            )
            # Hooks may not inject arbitrary evidence into the resource file.
            if not isinstance(result, dict) or result.get("status") not in (
                "passed", "failed", "blocked", "unsupported",
            ):
                raise TestClockError("invalid_verification_hook_result")
            phases.append({
                "phase": action, "providerTime": target,
                "status": result["status"],
                "code": result.get("code")
                if isinstance(result.get("code"), str)
                and re.fullmatch(r"[a-z0-9_.-]{1,80}", result["code"])
                else None,
            })
            if result["status"] != "passed":
                break
        resource["state"] = "executed"
        resource["phases"] = phases
        for score in manifest["results"]:
            if score["scenario"] == scenario:
                score["status"] = (
                    "passed" if phases and all(
                        item["status"] == "passed" for item in phases
                    ) and len(phases) == len(SCENARIO_PHASES[scenario])
                    else phases[-1]["status"]
                )
        _write_private_json(self.manifest_path, manifest)
        return phases

    def cleanup(self):
        manifest = self._load_or_new()
        failures = []
        for scenario, resource in manifest["resources"].items():
            if resource.get("state") == "deleted":
                continue
            try:
                # Persist between the two deletes so a clock failure never causes
                # a rerun to blindly repeat the already-completed customer write.
                if resource.get("customerId"):
                    self.provider.delete_customer(resource["customerId"])
                    resource["customerId"] = None
                    resource["state"] = "customer_deleted"
                    _write_private_json(self.manifest_path, manifest)
                self.provider.delete_clock(resource["clockId"])
                resource["state"] = "deleted"
            except TestClockError:
                failures.append(scenario)
            _write_private_json(self.manifest_path, manifest)
        manifest["cleanup"] = {
            "state": "complete" if not failures else "incomplete",
            "failedScenarios": failures,
        }
        _write_private_json(self.manifest_path, manifest)
        return manifest


def _parser():
    parser = argparse.ArgumentParser(
        description="Approval-gated Stripe Test Clock preparation tooling."
    )
    parser.add_argument("command", choices=("matrix", "prepare", "run", "cleanup"))
    parser.add_argument("--approval")
    parser.add_argument("--manifest")
    parser.add_argument("--scenario", choices=tuple(SCENARIO_MAP))
    parser.add_argument("--operator-approved", action="store_true")
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "matrix":
        print(json.dumps(scenario_matrix(), sort_keys=True, indent=2))
        return 0
    if not args.operator_approved or not args.approval or not args.manifest:
        raise TestClockError("operator_approval_and_manifests_required")
    approval = Approval.load(args.approval)
    key = require_execution_guards(approval)
    test_factory = isolated_connection_factory(
        os.environ[TEST_DATABASE_ENV], approval.database_namespace,
    )
    # This precedes every provider resource mutation and every database write.
    validate_connection_factory(test_factory, approval)
    provider = StripeTestClockProvider(
        key, max_requests=approval.max_requests,
        max_seconds=approval.max_run_seconds,
    )
    harness = TestClockHarness(approval, provider, args.manifest)
    if args.command == "run":
        if not args.scenario or args.scenario not in approval.fixtures:
            raise TestClockError("approved_run_scenario_fixture_required")
        harness.prepare()
        mutations = StandardScenarioMutations(
            approval, provider, approval.fixtures,
            connection_factory=test_factory,
        )
        verifier = IsolatedDatabaseVerifier(
            approval, approval.fixtures, connection_factory=test_factory,
        )
        phases = harness.run_scenario(args.scenario, mutations, verifier)
        print(json.dumps({
            "runId": approval.run_id, "scenario": args.scenario,
            "phases": phases,
        }, sort_keys=True))
        return 0 if phases and all(
            phase["status"] == "passed" for phase in phases
        ) else 1
    result = harness.prepare() if args.command == "prepare" else harness.cleanup()
    # Only counts and states go to stdout; provider IDs remain in the 0600 manifest.
    print(json.dumps({
        "runId": result["runId"],
        "resourceCount": len(result["resources"]),
        "cleanup": result["cleanup"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())