"""Provider-free tests for the opt-in Test Clock harness."""
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

from sceneit.billing_test_clock import (
    Approval, IsolatedDatabaseVerifier, ScenarioFixture,
    StandardScenarioMutations,
    StripeTestClockProvider, TestClockError, TestClockHarness,
    require_execution_guards, scenario_matrix,
)


def approval_document(**changes):
    result = {
        "approved": True,
        "runId": "clock-run-001",
        "databaseNamespace": "billing_clock_run001",
        "frozenTime": 1_800_000_000,
        "scenarios": ["monthly_renewal", "refund_companion"],
        "maxRequests": 20,
        "maxPollAttempts": 3,
        "maxRunSeconds": 30,
    }
    result.update(changes)
    return result


class Rows:
    def __init__(self, one=None):
        self.one = one

    def fetchone(self):
        return self.one


class IsolatedFixtureConnection:
    def __init__(self):
        self.customer_id = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        if "current_database()" in query:
            return Rows({
                "database": "billing_clock_run001", "schema": "public",
                "has_coverage": True, "has_events": True,
            })
        if "FROM sceneit_billing_accounts" in query and "FOR UPDATE" in query:
            return Rows(None)
        if query.startswith("INSERT INTO sceneit_billing_accounts"):
            self.customer_id = params[1]
            return Rows()
        if "allowance_anchor" in query:
            return Rows({
                "customer_id": self.customer_id or "cus_fixture",
                "allowance_anchor": object(),
            })
        if "FROM sceneit_billing_events" in query:
            if "received_at,event_id" in query:
                return Rows(None)
            return Rows({
                "event_id": "evt_safe", "state": "completed",
                "processed_at": object(),
            })
        if "FROM sceneit_paid_coverage" in query:
            return Rows({
                "tier_key": "base", "cadence": "monthly", "currency": "usd",
                "subtotal": 100, "tax": 0, "total": 100,
                "amount_paid": 100, "tax_behavior": "exclusive",
            })
        raise AssertionError(query)


class FakeProvider:
    def __init__(self):
        self.created = []
        self.deleted = []

    def verify_test_environment(self):
        return None

    def create_clock(self, **values):
        self.created.append(("clock", values))
        return "clock_fixture"

    def create_customer(self, **values):
        self.created.append(("customer", values))
        return "cus_fixture"

    def delete_customer(self, customer_id):
        self.deleted.append(("customer", customer_id))

    def delete_clock(self, clock_id):
        self.deleted.append(("clock", clock_id))

    def advance(self, clock_id, frozen_time, *, max_poll_attempts):
        self.created.append(("advance", {
            "clock_id": clock_id, "frozen_time": frozen_time,
            "max_poll_attempts": max_poll_attempts,
        }))


class FakeHook:
    def __init__(self):
        self.actions = []

    def provision(self, scenario, customer_id, clock_id):
        self.actions.append(("provision", scenario, customer_id, clock_id))

    def perform(self, scenario, action, provider_time, customer_id, clock_id):
        self.actions.append(("perform", action, provider_time))


class FakeVerifier:
    def begin_phase(self, scenario, phase, customer_id):
        return None

    def checkpoint(
        self, scenario, phase, provider_time, customer_id, *,
        max_attempts, deadline,
    ):
        return {"status": "passed", "code": "authoritative_assertions_passed"}


class TestClockHarnessTests(unittest.TestCase):
    def load_approval(self, directory, **changes):
        path = Path(directory) / "approval.json"
        path.write_text(json.dumps(approval_document(**changes)), encoding="utf-8")
        return Approval.load(path)

    def test_guards_reject_live_key_shared_database_and_missing_opt_in(self):
        with tempfile.TemporaryDirectory() as directory:
            approval = self.load_approval(directory)
            base = {
                "SCENEIT_STRIPE_TEST_CLOCK_APPROVED": "true",
                "SCENEIT_TEST_CLOCK_DATABASE_URL":
                    "postgresql://test/billing_clock_run001",
                "DATABASE_URL": "postgresql://production/app",
                "STRIPE_SECRET_KEY": "sk_live_forbidden",
            }
            with self.assertRaisesRegex(TestClockError, "test_mode"):
                require_execution_guards(approval, environ=base)
            base["STRIPE_SECRET_KEY"] = "sk_test_fixture"
            base["DATABASE_URL"] = base["SCENEIT_TEST_CLOCK_DATABASE_URL"]
            with self.assertRaisesRegex(TestClockError, "isolated"):
                require_execution_guards(approval, environ=base)
            base["DATABASE_URL"] = "postgresql://production/app"
            base.pop("SCENEIT_STRIPE_TEST_CLOCK_APPROVED")
            with self.assertRaisesRegex(TestClockError, "approval"):
                require_execution_guards(approval, environ=base)

    def test_run_cli_validates_and_injects_only_approved_database(self):
        calls = []

        class RawConnection:
            def execute(self, query):
                calls.append("database_validated")
                return Rows({
                    "database": "billing_clock_run001", "schema": "public",
                    "has_coverage": True, "has_events": True,
                })

            def commit(self):
                pass

            def rollback(self):
                pass

            def close(self):
                pass

        class Provider:
            def __init__(self, *_args, **_kwargs):
                calls.append("provider_constructed")

        class Harness:
            def __init__(self, *_args):
                pass

            def prepare(self):
                calls.append("provider_mutation")
                return {
                    "runId": "clock-run-001", "resources": {},
                    "cleanup": {"state": "required"},
                }

            def run_scenario(self, scenario, mutations, verifier):
                calls.append("scenario_run")
                self.assert_factory = mutations.connection_factory
                with mutations.connection_factory() as conn:
                    conn.execute("SELECT current_database()")
                return [{"phase": "renewal_paid", "status": "passed", "code": "passed"}]

        class Mutations:
            def __init__(
                self, _approval, _provider, _fixtures, *,
                connection_factory,
            ):
                self.connection_factory = connection_factory

        class Verifier:
            def __init__(
                self, _approval, _fixtures, *, connection_factory,
            ):
                self.connection_factory = connection_factory

        with tempfile.TemporaryDirectory() as directory:
            approval_path = Path(directory) / "approval.json"
            approval_path.write_text(
                json.dumps(approval_document(
                    scenarios=["monthly_renewal"],
                    fixtures={"monthly_renewal": {
                        "ownerId": "fixture-owner", "baseTier": "base",
                        "targetTier": None, "cadence": "monthly",
                        "targetCadence": None, "currency": "usd",
                        "taxLocation": {
                            "country": "US", "postalCode": "94107",
                        },
                        "zeroTaxLocation": None,
                    }},
                )), encoding="utf-8"
            )
            environment = {
                "SCENEIT_STRIPE_TEST_CLOCK_APPROVED": "true",
                "SCENEIT_TEST_CLOCK_DATABASE_URL":
                    "postgresql://approved/billing_clock_run001",
                "DATABASE_URL": "postgresql://sentinel/ordinary",
                "STRIPE_SECRET_KEY": "sk_test_fixture",
            }
            from sceneit import billing_test_clock
            with (
                patch.dict("os.environ", environment, clear=True),
                patch.object(
                    billing_test_clock.psycopg, "connect",
                    side_effect=lambda url, **_kwargs: (
                        self.assertEqual(
                            url, environment["SCENEIT_TEST_CLOCK_DATABASE_URL"]
                        ) or RawConnection()
                    ),
                ),
                patch.object(
                    billing_test_clock, "StripeTestClockProvider", Provider
                ),
                patch.object(billing_test_clock, "TestClockHarness", Harness),
                patch.object(
                    billing_test_clock, "StandardScenarioMutations", Mutations
                ),
                patch.object(
                    billing_test_clock, "IsolatedDatabaseVerifier", Verifier
                ),
                patch(
                    "sceneit.db.connection",
                    side_effect=AssertionError("ordinary database was accessed"),
                ),
            ):
                self.assertEqual(billing_test_clock.main([
                    "run", "--operator-approved",
                    "--approval", str(approval_path),
                    "--manifest", str(Path(directory) / "resources.json"),
                    "--scenario", "monthly_renewal",
                ]), 0)
        self.assertLess(
            calls.index("database_validated"), calls.index("provider_mutation")
        )

    def test_approval_is_strict_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(TestClockError, "invalid"):
                self.load_approval(directory, maxRequests=101)
            with self.assertRaisesRegex(TestClockError, "invalid"):
                self.load_approval(directory, scenarios=["not_a_scenario"])

    def test_prepare_is_rerun_safe_and_companions_create_no_resources(self):
        with tempfile.TemporaryDirectory() as directory:
            approval = self.load_approval(directory)
            provider = FakeProvider()
            manifest_path = Path(directory) / "resources.json"
            harness = TestClockHarness(approval, provider, manifest_path)
            harness.prepare()
            harness.prepare()
            self.assertEqual([row[0] for row in provider.created], ["clock", "customer"])
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["resources"]), {"monthly_renewal"})
            self.assertEqual(manifest_path.stat().st_mode & 0o777, 0o600)
            harness.cleanup()
            harness.cleanup()
            self.assertEqual(provider.deleted, [
                ("customer", "cus_fixture"), ("clock", "clock_fixture"),
            ])

    def test_matrix_never_claims_fixture_verification(self):
        rows = scenario_matrix()
        self.assertTrue(rows)
        self.assertTrue(all(row["status"] == "unverified" for row in rows))
        coverage = {value for row in rows for value in row["coverage"]}
        self.assertTrue({
            "renewal", "failed-payment", "recovery", "expired-card", "annual",
            "monthly-allowance", "tier-upgrade", "tier-downgrade",
            "cadence-change", "cancellation", "coverage-expiry", "tax",
            "currency", "full-refund", "webhook-replay", "inbox",
        }.issubset(coverage))

    def test_integration_hook_drives_bounded_scenario_without_time_override(self):
        with tempfile.TemporaryDirectory() as directory:
            approval = self.load_approval(directory)
            provider = FakeProvider()
            manifest_path = Path(directory) / "resources.json"
            harness = TestClockHarness(approval, provider, manifest_path)
            harness.prepare()
            hook = FakeHook()
            phases = harness.run_scenario(
                "monthly_renewal", hook, FakeVerifier()
            )
            self.assertEqual([item["status"] for item in phases], ["passed", "passed"])
            advances = [row for row in provider.created if row[0] == "advance"]
            self.assertEqual(len(advances), 1)
            self.assertEqual(
                advances[0][1]["frozen_time"], approval.frozen_time + 32 * 86_400
            )
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            result = next(
                row for row in saved["results"]
                if row["scenario"] == "monthly_renewal"
            )
            self.assertEqual(result["status"], "passed")

    def test_authoritative_verifier_cannot_pass_without_completed_receipt(self):
        verifier = IsolatedDatabaseVerifier(
            object(), {}, connection_factory=lambda: None,
            status_function=lambda _owner: None,
        )
        fixture = ScenarioFixture(
            "owner-1", "base", None, "monthly", None, "usd"
        )
        snapshot = {
            "membership": "active", "paymentProblem": None,
            "cancelAtPeriodEnd": False, "pendingChange": None,
            "effectiveTier": "base", "cadence": "monthly", "currency": "usd",
            "usage": {},
        }
        account = {"customer_id": "cus_fixture", "allowance_anchor": object()}
        coverage = {
            "subtotal": 100, "tax": 0, "total": 100, "amount_paid": 100,
            "tax_behavior": "exclusive",
        }
        self.assertEqual(
            verifier._assert_snapshot(
                "monthly_renewal", "renewal_paid", fixture, "cus_fixture",
                snapshot, account, None, coverage,
            ),
            "verified_webhook_receipt_pending",
        )
        receipt = {
            "state": "completed", "processed_at": object(),
        }
        self.assertIsNone(verifier._assert_snapshot(
            "monthly_renewal", "renewal_paid", fixture, "cus_fixture",
            snapshot, account, receipt, coverage,
        ))

    def test_tax_scenarios_require_exact_behavior_and_zero(self):
        verifier = IsolatedDatabaseVerifier(
            object(), {}, connection_factory=lambda: None,
            status_function=lambda _owner: None,
        )
        fixture = ScenarioFixture(
            "owner-1", "base", None, "monthly", None, "usd"
        )
        snapshot = {
            "membership": "active", "paymentProblem": None,
            "cancelAtPeriodEnd": False, "pendingChange": None,
            "effectiveTier": "base", "cadence": "monthly", "currency": "usd",
            "usage": {},
        }
        account = {"customer_id": "cus_fixture"}
        receipt = {"state": "completed", "processed_at": object()}
        coverage = {
            "subtotal": 100, "tax": 10, "total": 110, "amount_paid": 110,
            "tax_behavior": "exclusive",
        }
        self.assertEqual(verifier._assert_snapshot(
            "tax_inclusive", "inclusive_tax_renewal", fixture, "cus_fixture",
            snapshot, account, receipt, coverage,
        ), "inclusive_tax_behavior_mismatch")
        self.assertEqual(verifier._assert_snapshot(
            "tax_exclusive_or_zero", "zero_tax_renewal",
            fixture, "cus_fixture", snapshot, account, receipt, coverage,
        ), "zero_tax_fixture_not_zero")

    def test_phase_receipt_baseline_rejects_reused_completed_event(self):
        class ReceiptConnection(IsolatedFixtureConnection):
            def execute(self, query, params=None):
                if "current_database()" in query:
                    return super().execute(query, params)
                if query.startswith("SELECT received_at,event_id"):
                    return Rows({"received_at": 10, "event_id": "evt_old"})
                if "sceneit_billing_events" in query:
                    # The strict post-baseline query finds no newer receipt.
                    return Rows(None if len(params) == 3 else {
                        "state": "completed", "processed_at": object(),
                    })
                return super().execute(query, params)

        with tempfile.TemporaryDirectory() as directory:
            fixture_row = {
                "ownerId": "fixture-owner", "baseTier": "base",
                "targetTier": None, "cadence": "monthly",
                "targetCadence": None, "currency": "usd", "taxLocation": None,
                "zeroTaxLocation": None,
            }
            approval = self.load_approval(
                directory, scenarios=["monthly_renewal"],
                fixtures={"monthly_renewal": fixture_row},
            )
            conn = ReceiptConnection()
            environment = {
                "SCENEIT_STRIPE_TEST_CLOCK_APPROVED": "true",
                "SCENEIT_TEST_CLOCK_DATABASE_URL":
                    "postgresql://local/billing_clock_run001",
                "DATABASE_URL": "postgresql://production/sentinel",
            }
            verifier = IsolatedDatabaseVerifier(
                approval, approval.fixtures,
                connection_factory=lambda: conn,
                status_function=lambda _owner: {
                    "membership": "active", "paymentProblem": None,
                    "cancelAtPeriodEnd": False, "pendingChange": None,
                    "effectiveTier": "base", "cadence": "monthly",
                    "currency": "usd", "usage": {},
                },
                sleep=lambda _delay: None, environ=environment,
            )
            verifier.begin_phase(
                "monthly_renewal", "renewal_paid", "cus_fixture"
            )
            result = verifier.checkpoint(
                "monthly_renewal", "renewal_paid", approval.frozen_time,
                "cus_fixture", max_attempts=1, deadline=10**20,
            )
            self.assertEqual(result["code"], "verified_webhook_receipt_pending")

    def test_locked_sdk_paths_and_livemode_checks(self):
        requests = []

        def respond(request):
            requests.append(request)
            if request.url.path == "/v1/balance":
                return httpx.Response(200, json={
                    "object": "balance", "livemode": False,
                    "available": [], "pending": [],
                })
            if request.url.path == "/v1/test_helpers/test_clocks":
                body = parse_qs(request.content.decode())
                self.assertEqual(body["frozen_time"], ["1800000000"])
                return httpx.Response(200, json={
                    "id": "clock_fixture", "object": "test_helpers.test_clock",
                    "livemode": False, "status": "ready",
                    "frozen_time": 1_800_000_000,
                })
            if request.url.path == "/v1/customers":
                body = parse_qs(request.content.decode())
                self.assertEqual(body["test_clock"], ["clock_fixture"])
                return httpx.Response(200, json={
                    "id": "cus_fixture", "object": "customer",
                    "livemode": False, "test_clock": "clock_fixture",
                })
            self.fail(f"unexpected SDK path {request.url.path}")

        provider = StripeTestClockProvider(
            "sk_test_fixture", max_requests=3, max_seconds=5,
            transport=httpx.MockTransport(respond),
            base_address="https://stripe.mock",
        )
        provider.verify_test_environment()
        clock = provider.create_clock(
            name="safe", frozen_time=1_800_000_000, idempotency_key="clock-key"
        )
        customer = provider.create_customer(
            clock_id=clock, run_id="clock-run-001",
            scenario="monthly_renewal", idempotency_key="customer-key",
        )
        self.assertEqual(customer, "cus_fixture")
        self.assertEqual(
            [request.url.path for request in requests],
            ["/v1/balance", "/v1/test_helpers/test_clocks", "/v1/customers"],
        )
        self.assertTrue(all("Authorization" in request.headers for request in requests))

    def test_intercepted_monthly_scenario_uses_standard_mutations_and_verifier(self):
        requests = []

        def response(request):
            requests.append(request.url.path)
            path = request.url.path
            if path == "/v1/balance":
                body = {"object": "balance", "livemode": False,
                        "available": [], "pending": []}
            elif path == "/v1/test_helpers/test_clocks":
                body = {"id": "clock_fixture", "object": "test_helpers.test_clock",
                        "livemode": False, "status": "ready",
                        "frozen_time": 1_800_000_000}
            elif path == "/v1/customers":
                body = {"id": "cus_fixture", "object": "customer",
                        "livemode": False, "test_clock": "clock_fixture"}
            elif path == "/v1/payment_methods/pm_card_visa/attach":
                body = {"id": "pm_card_visa", "object": "payment_method"}
            elif path == "/v1/customers/cus_fixture":
                body = {"id": "cus_fixture", "object": "customer",
                        "livemode": False, "test_clock": "clock_fixture"}
            elif path == "/v1/subscriptions":
                body = {"id": "sub_fixture", "object": "subscription",
                        "livemode": False}
            elif path.endswith("/advance"):
                body = {"id": "clock_fixture", "object": "test_helpers.test_clock",
                        "livemode": False, "status": "advancing",
                        "frozen_time": 1_802_764_800}
            elif path == "/v1/test_helpers/test_clocks/clock_fixture":
                body = {"id": "clock_fixture", "object": "test_helpers.test_clock",
                        "livemode": False, "status": "ready",
                        "frozen_time": 1_802_764_800}
            else:
                self.fail(path)
            return httpx.Response(200, json=body)

        with tempfile.TemporaryDirectory() as directory:
            fixture_row = {
                "ownerId": "fixture-owner", "baseTier": "base",
                "targetTier": None, "cadence": "monthly",
                "targetCadence": None, "currency": "usd",
                "taxLocation": {"country": "US", "postalCode": "94107"},
                "zeroTaxLocation": None,
            }
            approval = self.load_approval(
                directory, scenarios=["monthly_renewal"],
                fixtures={"monthly_renewal": fixture_row},
            )
            provider = StripeTestClockProvider(
                "sk_test_fixture", max_requests=20, max_seconds=10,
                transport=httpx.MockTransport(response),
                base_address="https://stripe.mock", sleep=lambda _delay: None,
            )
            manifest = Path(directory) / "resources.json"
            harness = TestClockHarness(approval, provider, manifest)
            harness.prepare()
            conn = IsolatedFixtureConnection()
            offer = SimpleNamespace(
                tier="base", cadence="monthly", currency="usd",
                price_id="price_base",
            )
            settings = SimpleNamespace(
                enabled=True, environment="test",
                catalog=SimpleNamespace(
                    offer=lambda *_args, **_kwargs: offer
                ),
            )
            actions = StandardScenarioMutations(
                approval, provider, approval.fixtures, settings=settings,
                connection_factory=lambda: conn,
            )
            environment = {
                "SCENEIT_STRIPE_TEST_CLOCK_APPROVED": "true",
                "SCENEIT_TEST_CLOCK_DATABASE_URL":
                    "postgresql://local/billing_clock_run001",
                "DATABASE_URL": "postgresql://production/sceneit",
            }
            status = lambda _owner: {
                "membership": "active", "paymentProblem": None,
                "cancelAtPeriodEnd": False, "pendingChange": None,
                "effectiveTier": "base", "cadence": "monthly",
                "currency": "usd", "usage": {},
            }
            verifier = IsolatedDatabaseVerifier(
                approval, approval.fixtures,
                connection_factory=lambda: conn, status_function=status,
                sleep=lambda _delay: None, environ=environment,
            )
            phases = harness.run_scenario(
                "monthly_renewal", actions, verifier
            )
            self.assertEqual([row["status"] for row in phases], ["passed", "passed"])
            self.assertIn("/v1/subscriptions", requests)
            self.assertIn(
                "/v1/test_helpers/test_clocks/clock_fixture/advance", requests
            )

    def test_authoritative_annual_allowance_window_rolls_each_month(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture_row = {
                "ownerId": "fixture-owner", "baseTier": "base",
                "targetTier": None, "cadence": "yearly",
                "targetCadence": None, "currency": "usd", "taxLocation": None,
                "zeroTaxLocation": None,
            }
            approval = self.load_approval(
                directory, scenarios=["annual_monthly_allowances"],
                fixtures={"annual_monthly_allowances": fixture_row},
            )
            conn = IsolatedFixtureConnection()
            conn.execute = lambda query, params=None: (
                Rows({
                    "database": "billing_clock_run001", "schema": "public",
                    "has_coverage": True, "has_events": True,
                }) if "current_database()" in query else
                Rows({"customer_id": "cus_fixture", "allowance_anchor": object()})
                if "allowance_anchor" in query else
                Rows({"event_id": "evt_safe", "state": "completed",
                      "processed_at": object()})
                if "sceneit_billing_events" in query else
                Rows({"tier_key": "base", "cadence": "yearly", "currency": "usd",
                      "subtotal": 100, "tax": 0, "total": 100,
                      "amount_paid": 100, "tax_behavior": "exclusive"})
            )
            current = {"window": "2030-01-01", "used": 1}
            status = lambda _owner: {
                "membership": "active", "paymentProblem": None,
                "cancelAtPeriodEnd": False, "pendingChange": None,
                "effectiveTier": "base", "cadence": "yearly",
                "currency": "usd", "usage": {
                    "windowStart": current["window"],
                    "metrics": {"searches": {"used": current["used"]}},
                },
            }
            environment = {
                "SCENEIT_STRIPE_TEST_CLOCK_APPROVED": "true",
                "SCENEIT_TEST_CLOCK_DATABASE_URL":
                    "postgresql://local/billing_clock_run001",
                "DATABASE_URL": "postgresql://production/sceneit",
            }
            verifier = IsolatedDatabaseVerifier(
                approval, approval.fixtures,
                connection_factory=lambda: conn, status_function=status,
                sleep=lambda _delay: None, environ=environment,
            )
            first = verifier.checkpoint(
                "annual_monthly_allowances", "provision_yearly_paid_and_consume",
                approval.frozen_time, "cus_fixture", max_attempts=1,
                deadline=10**20,
            )
            current.update(window="2030-02-01", used=0)
            second = verifier.checkpoint(
                "annual_monthly_allowances", "first_allowance_boundary",
                approval.frozen_time + 32 * 86_400, "cus_fixture",
                max_attempts=1, deadline=10**20,
            )
            self.assertEqual((first["status"], second["status"]), ("passed", "passed"))

    def test_account_livemode_and_request_budget_fail_closed(self):
        def live(_request):
            return httpx.Response(200, json={
                "object": "balance", "livemode": True,
                "available": [], "pending": [],
            })

        provider = StripeTestClockProvider(
            "sk_test_fixture", max_requests=1, max_seconds=5,
            transport=httpx.MockTransport(live),
            base_address="https://stripe.mock",
        )
        with self.assertRaisesRegex(TestClockError, "live_stripe"):
            provider.verify_test_environment()
        with self.assertRaisesRegex(TestClockError, "budget"):
            provider.verify_test_environment()


if __name__ == "__main__":
    unittest.main()