"""Pure policy fixtures; no credentials, billing or media provider calls."""
import os
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from sceneit.billing_config import (
    ALL_METRICS, BillingSettings, billing_settings, reset_billing_settings,
)
from sceneit.config import ConfigError
from sceneit.quota import (
    _membership_required, _reserve, check_work, monthly_window, reserve,
    reserve_storage,
)


def commercial_environment():
    result = {
        "SCENEIT_BILLING_ENABLED": "true",
        "SCENEIT_BILLING_ENVIRONMENT": "test",
        "SCENEIT_STRIPE_PRICE_MONTHLY": "price_fixtureMonth",
        "SCENEIT_STRIPE_PRICE_YEARLY": "price_fixtureYear",
        "SCENEIT_BILLING_RETURN_URL": "https://fixture.invalid/",
        "SCENEIT_STRIPE_PORTAL_CONFIGURATION": "bpc_fixture",
        "STRIPE_SECRET_KEY": "sk_test_fixture_only",
        "STRIPE_WEBHOOK_SECRET": "whsec_fixture_only_signing_secret",
    }
    for prefix in ("MEMBER", "APP"):
        result.update({f"SCENEIT_{prefix}_{metric.upper()}": "100"
                       for metric in ALL_METRICS})
    return result


class QuotaPolicyTests(unittest.TestCase):
    def tearDown(self):
        reset_billing_settings()

    def test_disabled_needs_no_credentials_or_database(self):
        with patch.dict(os.environ, {}, clear=True):
            reset_billing_settings()
            self.assertFalse(billing_settings().enabled)
            conn = Mock()
            reserve(conn, "owner", "id", {"searches": 1})
            reserve_storage(conn, "owner", "/path", 123)
            check_work(conn, "owner")
            conn.execute.assert_not_called()

    def test_firebase_durable_trial_uses_app_only_worker_policy(self):
        conn = Mock()
        with patch(
            "sceneit.trial_identity.usage_owner",
            return_value="firebase-email-v1:durable-ledger",
        ) as usage_owner:
            self.assertFalse(
                _membership_required(conn, "firebase:private-owner", True)
            )
        usage_owner.assert_called_once_with(conn, "firebase:private-owner")

    def test_paid_replit_worker_policy_still_requires_membership(self):
        conn = Mock()
        with patch("sceneit.trial_identity.usage_owner") as usage_owner:
            self.assertTrue(_membership_required(conn, "replit-owner", True))
        usage_owner.assert_not_called()

    def test_check_work_applies_trial_policy_without_request_context(self):
        fixture = commercial_environment()
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = {"stopped": False}
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.dict(os.environ, fixture, clear=True), \
                patch(
                    "sceneit.trial_identity.usage_owner",
                    return_value="firebase-email-v1:durable-ledger",
                ), \
                patch("sceneit.quota._lock"), \
                patch("sceneit.quota._now", return_value=now), \
                patch("sceneit.quota._account") as account:
            reset_billing_settings()
            check_work(conn, "firebase:private-owner")
        account.assert_called_once_with(
            conn, "firebase:private-owner", now, require_membership=False
        )

    def test_reserve_keeps_firebase_trial_out_of_owner_paid_window(self):
        fixture = commercial_environment()
        conn = Mock()
        conn.execute.return_value.fetchone.return_value = None
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with patch.dict(os.environ, fixture, clear=True), \
                patch(
                    "sceneit.trial_identity.usage_owner",
                    return_value="firebase-email-v1:durable-ledger",
                ), \
                patch("sceneit.quota.check_work") as work, \
                patch("sceneit.quota._now", return_value=now), \
                patch("sceneit.quota._window", return_value=(0, 100)):
            reset_billing_settings()
            _reserve(
                conn, "firebase:private-owner", "worker-media-read",
                {"media_bytes": 10},
            )
        work.assert_called_once_with(
            conn, "firebase:private-owner", require_membership=False
        )
        reservation = [
            call for call in conn.execute.call_args_list
            if "INSERT INTO sceneit_usage_reservations" in call.args[0]
        ]
        self.assertEqual(1, len(reservation))
        self.assertIsNone(reservation[0].args[1][2])

    def test_explicit_finite_limits_and_credential_environment(self):
        fixture = commercial_environment()
        with patch.dict(os.environ, fixture, clear=True):
            reset_billing_settings()
            policy = billing_settings()
            self.assertTrue(policy.enabled)
            self.assertNotIn("sk_test_", repr(policy))
            self.assertNotIn("whsec_", repr(policy))
            for key, value in (
                ("SCENEIT_MEMBER_SEARCHES", "0"), ("SCENEIT_APP_MEDIA_BYTES", "unlimited"),
                ("SCENEIT_MEMBER_FRAMES", "-1"), ("SCENEIT_BILLING_ENVIRONMENT", "live"),
                ("SCENEIT_STRIPE_PRICE_YEARLY", "price_fixtureMonth"),
                ("SCENEIT_BILLING_RETURN_URL", "https://user:password@fixture.invalid/"),
                ("SCENEIT_BILLING_RETURN_URL", "https://fixture.invalid/?next=evil"),
                ("STRIPE_SECRET_KEY", "sk_live_fixture_only"),
            ):
                with self.subTest(key=key, value=value), patch.dict(os.environ, {key: value}):
                    reset_billing_settings()
                    with self.assertRaises(ConfigError):
                        billing_settings()

    def test_anniversary_leap_year_eom_and_utc(self):
        anchor = datetime(2024, 1, 31, 10, 30, tzinfo=timezone.utc)
        start, end = monthly_window(anchor, datetime(2024, 2, 29, 10, 30, tzinfo=timezone.utc))
        self.assertEqual((2, 29, 10, 30), (start.month, start.day, start.hour, start.minute))
        self.assertEqual((3, 31), (end.month, end.day))
        start, end = monthly_window(anchor, datetime(2025, 2, 28, 10, 29, tzinfo=timezone.utc))
        self.assertEqual((1, 31), (start.month, start.day))
        self.assertEqual((2, 28), (end.month, end.day))
        with self.assertRaises(ValueError):
            monthly_window(anchor, anchor.replace(year=2023))