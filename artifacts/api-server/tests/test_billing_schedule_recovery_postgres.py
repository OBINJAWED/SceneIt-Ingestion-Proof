import json
import os
import time
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx
import psycopg
import stripe
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


TEST_URL = os.environ.get("SCENEIT_TEST_DATABASE_URL", "")
SAFE = (
    bool(TEST_URL)
    and TEST_URL != os.environ.get("DATABASE_URL")
    and conninfo_to_dict(TEST_URL).get("dbname", "").startswith("sceneit_test")
)
MIGRATIONS = Path(__file__).parents[1] / "sceneit" / "migrations"


def _migration(version):
    path = next(MIGRATIONS.glob(f"{version:03d}_*.sql"))
    return "\n".join(
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip().upper() not in {"BEGIN;", "COMMIT;"}
    )


@unittest.skipUnless(SAFE, "requires a disposable sceneit_test* PostgreSQL database")
class BillingScheduleRecoveryPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = f"sceneit_schedule_recovery_{uuid.uuid4().hex}"
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
            conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
            later = tuple(sorted(
                int(path.name.split("_", 1)[0])
                for path in MIGRATIONS.glob("*.sql")
                if int(path.name.split("_", 1)[0]) > 14
            ))
            for version in (1, 2, 7, 10, 11, 13, 14) + later:
                conn.execute(_migration(version))

    def tearDown(self):
        with psycopg.connect(TEST_URL, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                sql.Identifier(self.schema)
            ))

    def _connect(self):
        conn = psycopg.connect(TEST_URL, autocommit=True, row_factory=dict_row)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(self.schema)))
        return conn

    @contextmanager
    def _billing_connection(self):
        with self._connect() as conn:
            yield conn

    def test_partial_schedule_is_released_before_fresh_schedule_is_configured(self):
        from sceneit import billing, billing_ops
        from sceneit.billing_config import (
            BillingCatalog, BillingOffer, BillingProblem, BillingSettings,
            BillingTier,
        )
        from sceneit.billing_provider import (
            StripeBillingProvider, _BoundedStripeHTTPClient,
        )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        period_end = now + timedelta(days=30)
        limits = {
            "imports": 10, "upload_attempts": 10, "analysis_seconds": 10,
            "searches": 10, "media_bytes": 10, "frames": 10,
            "storage_bytes": 10,
        }
        base = BillingTier("base", "Base", 2, ("imports",), limits)
        lower = BillingTier(
            "lower", "Lower", 1, ("imports",),
            {name: value // 2 for name, value in limits.items()},
        )
        old_offer = BillingOffer(
            "base", "monthly", "usd", "price_old", 2000,
            "exclusive", "txcd_10000000", True,
        )
        new_offer = BillingOffer(
            "lower", "yearly", "usd", "price_new", 1200,
            "exclusive", "txcd_10000000", True,
        )
        catalog = BillingCatalog(
            {"base": base, "lower": lower},
            {
                ("base", "monthly", "usd"): old_offer,
                ("lower", "yearly", "usd"): new_offer,
            },
            {"price_old": old_offer, "price_new": new_offer},
        )
        settings = BillingSettings(
            enabled=True, environment="test", catalog=catalog,
            prices={"monthly": "price_old", "yearly": "price_new"},
        )
        anchor = now - timedelta(days=4)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name) VALUES"
                "('owner-1','Owner')"
            )
            conn.execute(
                "INSERT INTO sceneit_billing_accounts(owner_id,environment,"
                "customer_id,customer_attempt_state,allowance_anchor) VALUES"
                "('owner-1','test','cus_1','created',%s)", (anchor,),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,"
                "customer_id,environment,price_id,status,current_period_end,tier_key,"
                "cadence,currency) VALUES('sub_1','owner-1','cus_1','test',"
                "'price_old','active',%s,'base','monthly','usd')", (period_end,),
            )
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                "starts_at,ends_at,tier_key,tier_rank,capabilities_snapshot,"
                "limits_snapshot,cadence,currency,price_id) VALUES"
                "('in_base','owner-1','sub_1',%s,%s,'base',2,'[\"imports\"]',"
                "%s,'monthly','usd','price_old')",
                (now - timedelta(days=2), period_end, Jsonb(limits)),
            )

        requests = []
        schedule_number = 0
        partial_released = False
        configured_updates = []

        def subscription():
            return {
                "id": "sub_1", "object": "subscription", "customer": "cus_1",
                "status": "active", "cancel_at_period_end": False,
                "livemode": False,
                "schedule": None if partial_released else "sub_sched_1",
                "metadata": {"sceneit_owner_id": "owner-1"},
                "items": {"data": [{
                    "id": "si_1", "current_period_end": int(period_end.timestamp()),
                    "price": {"id": "price_old", "currency": "usd"},
                }]},
            }

        def schedule(schedule_id, *, released=False, configured=False):
            phases = [{
                "start_date": int(now.timestamp()),
                "end_date": int(period_end.timestamp()),
                "items": [{"price": "price_old", "quantity": 1}],
            }]
            if configured:
                phases.append({
                    "start_date": int(period_end.timestamp()),
                    "items": [{"price": "price_new", "quantity": 1}],
                })
            return {
                "id": schedule_id, "object": "subscription_schedule",
                "status": "released" if released else "not_started",
                "customer": "cus_1",
                "subscription": None if released else "sub_1",
                "released_subscription": "sub_1" if released else None,
                "livemode": False,
                "metadata": (
                    {"sceneit_change_id": configured_updates[-1]["operation"]}
                    if configured else {}
                ),
                "phases": phases,
            }

        async def stripe_api(request):
            nonlocal schedule_number, partial_released
            body = parse_qs(request.content.decode())
            requests.append((request.method, request.url.path, body))
            if request.method == "GET" and request.url.path == "/v1/subscriptions/sub_1":
                return httpx.Response(200, json=subscription())
            if request.method == "GET" and request.url.path == "/v1/prices/price_new":
                return httpx.Response(200, json={
                    "id": "price_new", "object": "price", "active": True,
                    "livemode": False, "type": "recurring", "unit_amount": 1200,
                    "currency": "usd", "tax_behavior": "exclusive",
                    "product": {"tax_code": "txcd_10000000"},
                    "recurring": {
                        "interval": "year", "interval_count": 1,
                        "usage_type": "licensed",
                    },
                })
            if request.method == "POST" and request.url.path == "/v1/invoices/create_preview":
                return httpx.Response(200, json={
                    "id": f"upcoming_in_{len(requests)}", "object": "invoice",
                    "currency": "usd", "subtotal": 1200, "total": 1200,
                    "created": int(now.timestamp()), "total_taxes": [],
                    "automatic_tax": {"enabled": True, "status": "complete"},
                })
            if request.method == "POST" and request.url.path == "/v1/subscription_schedules":
                schedule_number += 1
                schedule_id = f"sub_sched_{schedule_number}"
                return httpx.Response(200, json=schedule(schedule_id))
            if request.method == "POST" and request.url.path.endswith("/release"):
                schedule_id = request.url.path.split("/")[-2]
                self.assertEqual("sub_sched_1", schedule_id)
                partial_released = True
                return httpx.Response(200, json=schedule(schedule_id, released=True))
            if request.method == "GET" and request.url.path.startswith(
                "/v1/subscription_schedules/"
            ):
                schedule_id = request.url.path.rsplit("/", 1)[-1]
                return httpx.Response(
                    200, json=schedule(
                        schedule_id,
                        released=partial_released and schedule_id == "sub_sched_1",
                    ),
                )
            if request.method == "POST" and request.url.path.startswith(
                "/v1/subscription_schedules/"
            ):
                schedule_id = request.url.path.rsplit("/", 1)[-1]
                if schedule_id == "sub_sched_1":
                    return httpx.Response(
                        429, json={"error": {
                            "type": "rate_limit_error", "code": "rate_limit",
                            "message": "fixture rejection",
                        }},
                    )
                configured_updates.append({
                    "schedule": schedule_id,
                    "operation": body["metadata[sceneit_change_id]"][0],
                    "body": body,
                })
                return httpx.Response(
                    200, json=schedule(schedule_id, configured=True),
                )
            return httpx.Response(500, json={"error": {
                "message": f"unexpected {request.method} {request.url.path}",
            }})

        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = stripe
        adapter._deadline = time.monotonic() + 10
        adapter._client = stripe.StripeClient(
            "sk_test_fixture",
            http_client=_BoundedStripeHTTPClient(
                adapter._deadline, transport=httpx.MockTransport(stripe_api),
            ),
            max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1

        patches = (
            patch.object(billing, "connection", self._billing_connection),
            patch.object(billing, "billing_settings", return_value=settings),
        )
        with patches[0], patches[1]:
            first_preview = billing.preview_change(
                "owner-1", "lower", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            with self.assertRaises(BillingProblem) as failed:
                billing.confirm_change(
                    "owner-1", first_preview["previewId"], uuid.uuid4(),
                    purchase_authorized=True, provider=adapter,
                )
        self.assertEqual("billing_outcome_unknown", failed.exception.code)
        with self._connect() as conn:
            uncertain = conn.execute(
                "SELECT o.operation_id,o.provider_object_id,o.state,c.change_id,"
                "c.provider_schedule_id,c.state change_state "
                "FROM sceneit_billing_operations o JOIN sceneit_billing_changes c "
                "ON c.operation_id=o.operation_id"
            ).fetchone()
        self.assertEqual("sub_sched_1", uncertain["provider_object_id"])
        self.assertEqual("sub_sched_1", uncertain["provider_schedule_id"])
        self.assertEqual(("uncertain", "uncertain"), (
            uncertain["state"], uncertain["change_state"],
        ))

        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            blocked_preview = billing.preview_change(
                "owner-1", "lower", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            with self.assertRaises(psycopg.errors.UniqueViolation):
                billing.confirm_change(
                    "owner-1", blocked_preview["previewId"], uuid.uuid4(),
                    purchase_authorized=True, provider=adapter,
                )
        self.assertEqual(1, schedule_number)

        with patch.object(billing_ops, "connection", self._billing_connection), \
                patch.object(billing_ops, "billing_settings", return_value=settings), \
                patch.object(billing_ops, "StripeBillingProvider", return_value=adapter), \
                patch.object(billing_ops, "_audit"):
            recovered = billing_ops.recover_operations(10, evidence="fixture recovery")
        self.assertEqual(1, recovered["resolved"], (recovered, requests))
        self.assertEqual([], recovered["unresolved"])
        self.assertEqual(1, schedule_number)
        with self._connect() as conn:
            terminal = conn.execute(
                "SELECT o.state,o.last_error_code,c.state change_state,"
                "a.allowance_anchor,v.reversed,v.price_id "
                "FROM sceneit_billing_operations o JOIN sceneit_billing_changes c "
                "ON c.operation_id=o.operation_id "
                "JOIN sceneit_billing_accounts a ON a.owner_id=o.owner_id "
                "JOIN sceneit_paid_coverage v ON v.owner_id=o.owner_id "
                "WHERE o.operation_id=%s", (uncertain["operation_id"],),
            ).fetchone()
        self.assertEqual(
            ("failed", "schedule_configuration_failed", "failed"),
            (terminal["state"], terminal["last_error_code"], terminal["change_state"]),
        )
        self.assertEqual(anchor, terminal["allowance_anchor"])
        self.assertEqual((False, "price_old"), (
            terminal["reversed"], terminal["price_id"],
        ))

        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            fresh_preview = billing.preview_change(
                "owner-1", "lower", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            fresh = billing.confirm_change(
                "owner-1", fresh_preview["previewId"], uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
        self.assertEqual("scheduled", fresh["state"])
        self.assertEqual(2, schedule_number)
        self.assertEqual(1, len(configured_updates))
        configured = configured_updates[0]
        with self._connect() as conn:
            fresh_operation = str(conn.execute(
                "SELECT operation_id FROM sceneit_billing_changes "
                "WHERE provider_schedule_id='sub_sched_2'"
            ).fetchone()["operation_id"])
        self.assertEqual("sub_sched_2", configured["schedule"])
        self.assertEqual(["price_old"], configured["body"]["phases[0][items][0][price]"])
        self.assertEqual(["price_new"], configured["body"]["phases[1][items][0][price]"])
        self.assertEqual(fresh_operation, configured["operation"])
        paths = [path for _method, path, _body in requests]
        self.assertFalse(any(method != "GET" and path.startswith(
            "/v1/subscriptions/") for method, path, _body in requests))
        self.assertEqual(1, paths.count(
            "/v1/subscription_schedules/sub_sched_1/release"
        ))

    def _compensation_loss_fixture(self, loss_mode):
        from sceneit.billing_config import (
            BillingCatalog, BillingOffer, BillingSettings, BillingTier,
        )
        from sceneit.billing_provider import (
            StripeBillingProvider, _BoundedStripeHTTPClient,
        )

        now = datetime.now(timezone.utc).replace(microsecond=0)
        period_end = now + timedelta(days=30)
        anchor = now - timedelta(days=5)
        limits = {name: 20 for name in (
            "imports", "upload_attempts", "analysis_seconds", "searches",
            "media_bytes", "frames", "storage_bytes",
        )}
        source_tier = BillingTier("source", "Source", 2, ("imports",), limits)
        target_tier = BillingTier(
            "target", "Target", 1, ("imports",),
            {name: 10 for name in limits},
        )
        source = BillingOffer(
            "source", "monthly", "usd", "price_source", 2000,
            "exclusive", "txcd_10000000", True,
        )
        target = BillingOffer(
            "target", "yearly", "usd", "price_target", 1200,
            "exclusive", "txcd_10000000", True,
        )
        settings = BillingSettings(
            enabled=True, environment="test",
            catalog=BillingCatalog(
                {"source": source_tier, "target": target_tier},
                {
                    ("source", "monthly", "usd"): source,
                    ("target", "yearly", "usd"): target,
                },
                {"price_source": source, "price_target": target},
            ),
            prices={"monthly": "price_source", "yearly": "price_target"},
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name) "
                "VALUES('owner-1','Owner')"
            )
            conn.execute(
                "INSERT INTO sceneit_billing_accounts(owner_id,environment,"
                "customer_id,customer_attempt_state,allowance_anchor) "
                "VALUES('owner-1','test','cus_1','created',%s)", (anchor,),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,"
                "customer_id,environment,price_id,status,current_period_end,tier_key,"
                "cadence,currency) VALUES('sub_1','owner-1','cus_1','test',"
                "'price_source','active',%s,'source','monthly','usd')",
                (period_end,),
            )
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                "starts_at,ends_at,tier_key,tier_rank,capabilities_snapshot,"
                "limits_snapshot,cadence,currency,price_id) VALUES"
                "('coverage_1','owner-1','sub_1',%s,%s,'source',2,"
                "'[\"imports\"]',%s,'monthly','usd','price_source')",
                (now - timedelta(days=2), period_end, Jsonb(limits)),
            )
            conn.execute(
                "INSERT INTO sceneit_import_usage(owner_id,imports_used,searches_used) "
                "VALUES('owner-1',3,2)"
            )

        state = {
            "released": False,
            "release_calls": 0,
            "creates": 0,
            "failed_updates": 0,
            "configured_updates": [],
            "lose_verification_get": False,
            "relationships_valid": True,
        }
        requests = []

        def subscription():
            return {
                "id": "sub_1", "object": "subscription", "customer": "cus_1",
                "status": "active", "cancel_at_period_end": False,
                "livemode": False,
                "schedule": None if state["released"] else "sub_sched_1",
                "metadata": {"sceneit_owner_id": "owner-1"},
                "items": {"data": [{
                    "id": "si_1", "current_period_end": int(period_end.timestamp()),
                    "price": {"id": "price_source", "currency": "usd"},
                }]},
            }

        def schedule(schedule_id, *, configured=False):
            phases = [{
                "start_date": int(now.timestamp()),
                "end_date": int(period_end.timestamp()),
                "items": [{"price": "price_source", "quantity": 1}],
            }]
            if configured:
                phases.append({
                    "start_date": int(period_end.timestamp()),
                    "items": [{"price": "price_target", "quantity": 1}],
                })
            released = state["released"] and schedule_id == "sub_sched_1"
            return {
                "id": schedule_id, "object": "subscription_schedule",
                "status": "released" if released else "not_started",
                "customer": "cus_wrong" if (
                    released and not state["relationships_valid"]
                ) else "cus_1",
                "subscription": None if released else "sub_1",
                "released_subscription": "sub_1" if released else None,
                "livemode": False,
                "metadata": (
                    {"sceneit_change_id": state["configured_updates"][-1]["operation"]}
                    if configured else {}
                ),
                "phases": phases,
            }

        async def raw(request):
            body = parse_qs(request.content.decode())
            requests.append((request.method, request.url.path, body))
            if request.method == "GET" and request.url.path == "/v1/subscriptions/sub_1":
                return httpx.Response(200, json=subscription())
            if request.method == "GET" and request.url.path == "/v1/prices/price_target":
                return httpx.Response(200, json={
                    "id": "price_target", "object": "price", "active": True,
                    "livemode": False, "type": "recurring", "unit_amount": 1200,
                    "currency": "usd", "tax_behavior": "exclusive",
                    "product": {"tax_code": "txcd_10000000"},
                    "recurring": {
                        "interval": "year", "interval_count": 1,
                        "usage_type": "licensed",
                    },
                })
            if request.method == "POST" and request.url.path == "/v1/invoices/create_preview":
                return httpx.Response(200, json={
                    "id": f"upcoming_in_{len(requests)}", "object": "invoice",
                    "currency": "usd", "subtotal": 1200, "total": 1200,
                    "created": int(now.timestamp()), "total_taxes": [],
                    "automatic_tax": {"enabled": True, "status": "complete"},
                })
            if request.method == "POST" and request.url.path == "/v1/subscription_schedules":
                state["creates"] += 1
                return httpx.Response(
                    200, json=schedule(f"sub_sched_{state['creates']}")
                )
            if request.method == "POST" and request.url.path.endswith("/release"):
                state["release_calls"] += 1
                state["released"] = True
                if loss_mode == "release_response":
                    raise httpx.ReadError(
                        "release response was lost", request=request
                    )
                if loss_mode == "verification_get":
                    state["lose_verification_get"] = True
                return httpx.Response(200, json=schedule("sub_sched_1"))
            if request.method == "GET" and request.url.path.startswith(
                "/v1/subscription_schedules/"
            ):
                if state["lose_verification_get"]:
                    state["lose_verification_get"] = False
                    raise httpx.ReadError(
                        "verification response was lost", request=request
                    )
                return httpx.Response(
                    200, json=schedule(request.url.path.rsplit("/", 1)[-1])
                )
            if request.method == "POST" and request.url.path.startswith(
                "/v1/subscription_schedules/"
            ):
                schedule_id = request.url.path.rsplit("/", 1)[-1]
                if schedule_id == "sub_sched_1":
                    state["failed_updates"] += 1
                    return httpx.Response(429, json={"error": {
                        "type": "rate_limit_error", "code": "rate_limit",
                        "message": "fixture configuration failure",
                    }})
                state["configured_updates"].append({
                    "schedule": schedule_id,
                    "operation": body["metadata[sceneit_change_id]"][0],
                })
                return httpx.Response(
                    200, json=schedule(schedule_id, configured=True)
                )
            return httpx.Response(500, json={"error": {
                "message": f"unexpected {request.method} {request.url.path}",
            }})

        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = stripe
        adapter._deadline = time.monotonic() + 10
        adapter._client = stripe.StripeClient(
            "sk_test_fixture",
            http_client=_BoundedStripeHTTPClient(
                adapter._deadline, transport=httpx.MockTransport(raw),
            ),
            max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1
        return settings, adapter, state, requests, anchor

    def _create_uncertain_schedule(self, settings, adapter):
        from sceneit import billing
        from sceneit.billing_config import BillingProblem

        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            preview = billing.preview_change(
                "owner-1", "target", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            with self.assertRaises(BillingProblem) as caught:
                billing.confirm_change(
                    "owner-1", preview["previewId"], uuid.uuid4(),
                    purchase_authorized=True, provider=adapter,
                )
        self.assertEqual("billing_outcome_unknown", caught.exception.code)
        with self._connect() as conn:
            return conn.execute(
                "SELECT o.operation_id,o.state,c.change_id,c.state change_state "
                "FROM sceneit_billing_operations o JOIN sceneit_billing_changes c "
                "ON c.operation_id=o.operation_id"
            ).fetchone()

    def _recover_schedules(self, settings, adapter):
        from sceneit import billing_ops

        with patch.object(billing_ops, "connection", self._billing_connection), \
                patch.object(billing_ops, "billing_settings", return_value=settings), \
                patch.object(billing_ops, "StripeBillingProvider", return_value=adapter), \
                patch.object(billing_ops, "_audit"):
            return billing_ops.recover_operations(10, evidence="fixture recovery")

    def _assert_lost_compensation_recovers_once(self, loss_mode):
        from sceneit import billing

        settings, adapter, state, requests, anchor = \
            self._compensation_loss_fixture(loss_mode)
        original = self._create_uncertain_schedule(settings, adapter)
        first = self._recover_schedules(settings, adapter)
        self.assertEqual(0, first["resolved"], first)
        self.assertEqual([str(original["operation_id"])], first["unresolved"])
        self.assertEqual(1, state["release_calls"])
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT o.state,o.last_error_code,c.state change_state "
                "FROM sceneit_billing_operations o JOIN sceneit_billing_changes c "
                "ON c.operation_id=o.operation_id WHERE o.operation_id=%s",
                (original["operation_id"],),
            ).fetchone()
        self.assertEqual(
            ("uncertain", "schedule_compensation_releasing", "uncertain"),
            tuple(pending.values()),
        )

        second = self._recover_schedules(settings, adapter)
        self.assertEqual(1, second["resolved"], second)
        self.assertEqual([], second["unresolved"])
        self.assertEqual(1, state["release_calls"])
        with self._connect() as conn:
            terminal = conn.execute(
                "SELECT o.state,o.last_error_code,c.state change_state "
                "FROM sceneit_billing_operations o JOIN sceneit_billing_changes c "
                "ON c.operation_id=o.operation_id WHERE o.operation_id=%s",
                (original["operation_id"],),
            ).fetchone()
        self.assertEqual(
            ("failed", "schedule_configuration_failed", "failed"),
            tuple(terminal.values()),
        )

        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            fresh_preview = billing.preview_change(
                "owner-1", "target", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            fresh = billing.confirm_change(
                "owner-1", fresh_preview["previewId"], uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
        self.assertEqual("scheduled", fresh["state"])
        self.assertEqual(2, state["creates"])
        self.assertEqual(1, state["failed_updates"])
        self.assertEqual(1, len(state["configured_updates"]))
        self.assertEqual(1, state["release_calls"])
        with self._connect() as conn:
            integrity = conn.execute(
                "SELECT (SELECT count(*) FROM sceneit_paid_coverage "
                "WHERE owner_id='owner-1') coverage_count,"
                "(SELECT count(*) FROM sceneit_billing_changes "
                "WHERE owner_id='owner-1') change_count,"
                "(SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-1') allowance_anchor,"
                "(SELECT imports_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') imports_used,"
                "(SELECT searches_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') searches_used"
            ).fetchone()
        self.assertEqual(
            (1, 2, anchor, 3, 2),
            tuple(integrity.values()),
        )
        release_path = "/v1/subscription_schedules/sub_sched_1/release"
        self.assertEqual(
            1,
            sum(path == release_path for _method, path, _body in requests),
        )

    def test_lost_compensation_release_response_recovers_already_released_schedule(self):
        self._assert_lost_compensation_recovers_once("release_response")

    def test_lost_compensation_verification_get_recovers_already_released_schedule(self):
        self._assert_lost_compensation_recovers_once("verification_get")

    def test_already_released_schedule_without_compensation_intent_stays_blocked(self):
        settings, adapter, state, _requests, anchor = \
            self._compensation_loss_fixture("none")
        original = self._create_uncertain_schedule(settings, adapter)
        state["released"] = True
        recovered = self._recover_schedules(settings, adapter)
        self.assertEqual(0, recovered["resolved"], recovered)
        self.assertEqual([str(original["operation_id"])], recovered["unresolved"])
        self.assertEqual(0, state["release_calls"])
        with self._connect() as conn:
            blocked = conn.execute(
                "SELECT o.state,c.state change_state,"
                "(SELECT count(*) FROM sceneit_paid_coverage "
                "WHERE owner_id='owner-1') coverage_count,"
                "(SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-1') allowance_anchor,"
                "(SELECT imports_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') imports_used,"
                "(SELECT searches_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') searches_used "
                "FROM sceneit_billing_operations o "
                "JOIN sceneit_billing_changes c ON c.operation_id=o.operation_id "
                "WHERE o.operation_id=%s",
                (original["operation_id"],),
            ).fetchone()
        self.assertEqual(
            ("uncertain", "uncertain", 1, anchor, 3, 2),
            tuple(blocked.values()),
        )

    def _renewal_boundary_fixture(self):
        from sceneit.billing_config import (
            BillingCatalog, BillingOffer, BillingSettings, BillingTier,
        )
        from sceneit.billing_provider import (
            StripeBillingProvider, _BoundedStripeHTTPClient,
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        old_end = now + timedelta(days=1)
        limits = {name: 10 for name in (
            "imports", "upload_attempts", "analysis_seconds", "searches",
            "media_bytes", "frames", "storage_bytes",
        )}
        source_tier = BillingTier("source", "Source", 2, ("imports",), limits)
        target_tier = BillingTier("target", "Target", 1, ("imports",), limits)
        source = BillingOffer(
            "source", "monthly", "usd", "price_source", 2000,
            "exclusive", "txcd_10000000", True,
        )
        target = BillingOffer(
            "target", "yearly", "usd", "price_target", 1200,
            "exclusive", "txcd_10000000", True,
        )
        settings = BillingSettings(
            enabled=True, environment="test",
            catalog=BillingCatalog(
                {"source": source_tier, "target": target_tier},
                {
                    ("source", "monthly", "usd"): source,
                    ("target", "yearly", "usd"): target,
                },
                {"price_source": source, "price_target": target},
            ),
        )
        anchor = now - timedelta(days=3)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sceneit_auth_users(id,first_name) "
                "VALUES('owner-1','Owner')"
            )
            conn.execute(
                "INSERT INTO sceneit_billing_accounts(owner_id,environment,"
                "customer_id,customer_attempt_state,allowance_anchor) "
                "VALUES('owner-1','test','cus_1','created',%s)", (anchor,),
            )
            conn.execute(
                "INSERT INTO sceneit_billing_subscriptions(subscription_id,owner_id,"
                "customer_id,environment,price_id,status,current_period_end,tier_key,"
                "cadence,currency) VALUES('sub_1','owner-1','cus_1','test',"
                "'price_source','active',%s,'source','monthly','usd')", (old_end,),
            )
            conn.execute(
                "INSERT INTO sceneit_paid_coverage(id,owner_id,subscription_id,"
                "starts_at,ends_at,tier_key,tier_rank,capabilities_snapshot,"
                "limits_snapshot,cadence,currency,price_id) VALUES"
                "('boundary_coverage','owner-1','sub_1',%s,%s,'source',2,"
                "'[\"imports\"]',%s,'monthly','usd','price_source')",
                (now - timedelta(days=2), old_end, Jsonb(limits)),
            )
            conn.execute(
                "INSERT INTO sceneit_import_usage(owner_id,imports_used,searches_used) "
                "VALUES('owner-1',4,3)"
            )
        state = {
            "period_end": old_end, "created_end": None, "creates": 0,
            "updates": [], "created_id": None, "schedule_ends": {},
            "released": set(), "release_calls": 0, "active_schedule": None,
        }
        requests = []

        def schedule_resource(schedule_id):
            released = schedule_id in state["released"]
            boundary = state["schedule_ends"][schedule_id]
            return {
                "id": schedule_id, "object": "subscription_schedule",
                "status": "released" if released else "not_started",
                "customer": "cus_1",
                "subscription": None if released else "sub_1",
                "released_subscription": "sub_1" if released else None,
                "livemode": False, "metadata": {},
                "phases": [{
                    "start_date": int(now.timestamp()),
                    "end_date": int(boundary.timestamp()),
                    "items": [{"price": "price_source", "quantity": 1}],
                }],
            }

        async def raw(request):
            from urllib.parse import parse_qs
            body = parse_qs(request.content.decode())
            requests.append((request.method, request.url.path, body))
            if request.method == "GET" and request.url.path == "/v1/subscriptions/sub_1":
                return httpx.Response(200, json={
                    "id": "sub_1", "object": "subscription", "customer": "cus_1",
                    "status": "active", "cancel_at_period_end": False,
                    "livemode": False,
                    "schedule": state["active_schedule"],
                    "metadata": {"sceneit_owner_id": "owner-1"},
                    "items": {"data": [{
                        "id": "si_1",
                        "current_period_end": int(state["period_end"].timestamp()),
                        "price": {"id": "price_source", "currency": "usd"},
                    }]},
                })
            if request.method == "GET" and request.url.path == "/v1/prices/price_target":
                return httpx.Response(200, json={
                    "id": "price_target", "object": "price", "active": True,
                    "livemode": False, "type": "recurring", "unit_amount": 1200,
                    "currency": "usd", "tax_behavior": "exclusive",
                    "product": {"tax_code": "txcd_10000000"},
                    "recurring": {"interval": "year", "interval_count": 1,
                                  "usage_type": "licensed"},
                })
            if request.method == "POST" and request.url.path == "/v1/invoices/create_preview":
                return httpx.Response(200, json={
                    "id": f"upcoming_in_{len(requests)}", "object": "invoice",
                    "currency": "usd", "subtotal": 1200, "total": 1200,
                    "created": int(now.timestamp()), "total_taxes": [],
                    "automatic_tax": {"enabled": True, "status": "complete"},
                })
            if request.method == "POST" and request.url.path == "/v1/subscription_schedules":
                state["creates"] += 1
                state["created_id"] = f"sub_sched_boundary_{state['creates']}"
                boundary = state["created_end"] or state["period_end"]
                state["schedule_ends"][state["created_id"]] = boundary
                state["active_schedule"] = state["created_id"]
                if state["created_end"]:
                    state["period_end"] = state["created_end"]
                return httpx.Response(
                    200, json=schedule_resource(state["created_id"])
                )
            if request.method == "GET" and request.url.path.startswith(
                "/v1/subscription_schedules/"
            ):
                schedule_id = request.url.path.rsplit("/", 1)[-1]
                return httpx.Response(200, json=schedule_resource(schedule_id))
            if request.method == "POST" and request.url.path.endswith("/release"):
                schedule_id = request.url.path.split("/")[-2]
                state["release_calls"] += 1
                state["released"].add(schedule_id)
                if state["active_schedule"] == schedule_id:
                    state["active_schedule"] = None
                return httpx.Response(200, json=schedule_resource(schedule_id))
            if request.method == "POST" and request.url.path.startswith(
                "/v1/subscription_schedules/"
            ):
                state["updates"].append({
                    "schedule": request.url.path.rsplit("/", 1)[-1],
                    "body": body,
                })
                return httpx.Response(200, json={"id": state["created_id"]})
            return httpx.Response(500, json={"error": {"message": "unexpected"}})

        adapter = object.__new__(StripeBillingProvider)
        adapter._stripe = stripe
        adapter._deadline = time.monotonic() + 10
        adapter._client = stripe.StripeClient(
            "sk_test_fixture",
            http_client=_BoundedStripeHTTPClient(
                adapter._deadline, transport=httpx.MockTransport(raw),
            ),
            max_network_retries=0,
            base_addresses={"api": "https://stripe.mock"},
        ).v1
        return settings, adapter, state, requests, old_end, anchor

    def test_scheduled_preview_expires_at_renewal_and_refreshes_effective_date(self):
        from sceneit import billing
        from sceneit.billing_config import BillingProblem
        settings, adapter, state, requests, old_end, anchor = \
            self._renewal_boundary_fixture()
        new_end = old_end + timedelta(days=30)
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            preview = billing.preview_change(
                "owner-1", "target", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            self.assertEqual(old_end, datetime.fromisoformat(
                preview["effectiveAt"].replace("Z", "+00:00")
            ))
            state["period_end"] = new_end
            with self._connect() as conn:
                conn.execute(
                    "UPDATE sceneit_billing_subscriptions SET current_period_end=%s "
                    "WHERE subscription_id='sub_1'", (new_end,),
                )
            with self.assertRaises(BillingProblem) as caught:
                billing.confirm_change(
                    "owner-1", preview["previewId"], uuid.uuid4(),
                    purchase_authorized=True, provider=adapter,
                )
            self.assertEqual("preview_expired", caught.exception.code)
            self.assertEqual(0, state["creates"])
            fresh = billing.preview_change(
                "owner-1", "target", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            self.assertEqual(new_end, datetime.fromisoformat(
                fresh["effectiveAt"].replace("Z", "+00:00")
            ))
            confirmed = billing.confirm_change(
                "owner-1", fresh["previewId"], uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
        self.assertEqual("scheduled", confirmed["state"])
        self.assertEqual(1, state["creates"])
        self.assertEqual(
            [str(int(new_end.timestamp()))],
            state["updates"][0]["body"]["phases[0][end_date]"],
        )
        with self._connect() as conn:
            self.assertEqual(anchor, conn.execute(
                "SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-1'"
            ).fetchone()["allowance_anchor"])

    def test_schedule_create_boundary_shift_never_updates_wrong_future_phase(self):
        from sceneit import billing, billing_ops
        from sceneit.billing_config import BillingProblem
        settings, adapter, state, requests, old_end, anchor = \
            self._renewal_boundary_fixture()
        state["created_end"] = old_end + timedelta(days=30)
        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            preview = billing.preview_change(
                "owner-1", "target", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            with self.assertRaises(BillingProblem) as caught:
                billing.confirm_change(
                    "owner-1", preview["previewId"], uuid.uuid4(),
                    purchase_authorized=True, provider=adapter,
                )
        self.assertEqual("billing_outcome_unknown", caught.exception.code)
        self.assertEqual(1, state["creates"])
        self.assertEqual([], state["updates"])
        with self._connect() as conn:
            row = conn.execute(
                "SELECT o.state,o.provider_object_id,c.state change_state,"
                "c.provider_schedule_id FROM sceneit_billing_operations o "
                "JOIN sceneit_billing_changes c ON c.operation_id=o.operation_id"
            ).fetchone()
        self.assertEqual(
            ("uncertain", state["created_id"], "uncertain", state["created_id"]),
            tuple(row.values()),
        )
        original_operation = row["provider_object_id"]

        with patch.object(billing_ops, "connection", self._billing_connection), \
                patch.object(billing_ops, "billing_settings", return_value=settings), \
                patch.object(billing_ops, "StripeBillingProvider", return_value=adapter), \
                patch.object(billing_ops, "_audit"):
            recovered = billing_ops.recover_operations(
                10, evidence="created phase boundary moved"
            )
        self.assertEqual(1, recovered["resolved"], recovered)
        self.assertEqual([], recovered["unresolved"])
        self.assertEqual(1, state["release_calls"])
        self.assertEqual(
            {"sub_sched_boundary_1"}, state["released"]
        )
        self.assertEqual([], state["updates"])
        with self._connect() as conn:
            terminal = conn.execute(
                "SELECT o.operation_id,o.state,o.last_error_code,"
                "o.expires_at compensation_phase_end,c.state change_state,"
                "p.effective_at approved_effective_at,"
                "p.subtotal,p.tax,p.total,p.target_price_id,"
                "(SELECT count(*) FROM sceneit_paid_coverage "
                "WHERE owner_id='owner-1') coverage_count,"
                "(SELECT imports_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') imports_used,"
                "(SELECT searches_used FROM sceneit_import_usage "
                "WHERE owner_id='owner-1') searches_used,"
                "(SELECT allowance_anchor FROM sceneit_billing_accounts "
                "WHERE owner_id='owner-1') allowance_anchor "
                "FROM sceneit_billing_operations o "
                "JOIN sceneit_billing_changes c ON c.operation_id=o.operation_id "
                "JOIN sceneit_billing_change_previews p ON p.preview_id=c.preview_id "
                "WHERE o.provider_object_id='sub_sched_boundary_1'"
            ).fetchone()
        self.assertEqual(
            ("failed", "schedule_configuration_failed", "failed"),
            (
                terminal["state"], terminal["last_error_code"],
                terminal["change_state"],
            ),
        )
        self.assertEqual(old_end, terminal["approved_effective_at"])
        self.assertEqual(state["created_end"], terminal["compensation_phase_end"])
        self.assertEqual(
            (1200, 0, 1200, "price_target"),
            (
                terminal["subtotal"], terminal["tax"], terminal["total"],
                terminal["target_price_id"],
            ),
        )
        self.assertEqual(
            (1, 4, 3, anchor),
            (
                terminal["coverage_count"], terminal["imports_used"],
                terminal["searches_used"], terminal["allowance_anchor"],
            ),
        )

        with patch.object(billing, "connection", self._billing_connection), \
                patch.object(billing, "billing_settings", return_value=settings):
            fresh_preview = billing.preview_change(
                "owner-1", "target", "yearly", "usd", uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
            self.assertEqual(
                state["created_end"],
                datetime.fromisoformat(
                    fresh_preview["effectiveAt"].replace("Z", "+00:00")
                ),
            )
            fresh = billing.confirm_change(
                "owner-1", fresh_preview["previewId"], uuid.uuid4(),
                purchase_authorized=True, provider=adapter,
            )
        self.assertEqual("scheduled", fresh["state"])
        self.assertEqual(2, state["creates"])
        self.assertEqual(1, state["release_calls"])
        self.assertEqual(1, len(state["updates"]))
        self.assertEqual("sub_sched_boundary_2", state["updates"][0]["schedule"])
        with self._connect() as conn:
            fresh_operation = str(conn.execute(
                "SELECT operation_id FROM sceneit_billing_changes "
                "WHERE provider_schedule_id='sub_sched_boundary_2'"
            ).fetchone()["operation_id"])
        self.assertEqual(
            [fresh_operation],
            state["updates"][0]["body"]["metadata[sceneit_change_id]"],
        )
        self.assertNotEqual(str(terminal["operation_id"]), fresh_operation)
        self.assertEqual("sub_sched_boundary_1", original_operation)
        release_path = (
            "/v1/subscription_schedules/sub_sched_boundary_1/release"
        )
        self.assertEqual(
            1,
            sum(path == release_path for _method, path, _body in requests),
        )