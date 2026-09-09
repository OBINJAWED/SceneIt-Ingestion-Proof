"""Provider-free notification policy and SMTP classification tests."""
import os
import smtplib
import unittest
from unittest.mock import Mock, patch

from sceneit.billing_notifications import InvoiceNotificationFact, _message_id
from sceneit.billing_smtp import (
    NotificationSettings, SMTPDeliveryError, TLSMailer,
    notification_settings, reset_notification_settings,
)
from sceneit.config import ConfigError


class NotificationPolicyTests(unittest.TestCase):
    def tearDown(self):
        reset_notification_settings()

    def test_notifications_are_fully_disabled_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            reset_notification_settings()
            settings = notification_settings()
        self.assertFalse(settings.enabled)
        self.assertFalse(settings.scheduler_enabled)

    def test_activation_requires_all_approvals(self):
        with patch.dict(os.environ, {
            "SCENEIT_BILLING_NOTIFICATIONS_ENABLED": "true",
        }, clear=True):
            reset_notification_settings()
            with self.assertRaises(ConfigError):
                notification_settings()

    def test_paid_void_reversed_and_obsolete_are_ineligible(self):
        base = dict(
            invoice_id="in_safe", recipient="billing@example.test",
            state="past_due", tier="fixture", currency="usd",
            amount_due=100, attempts=1,
        )
        self.assertTrue(InvoiceNotificationFact(**base).eligible)
        for changes in (
            {"state": "paid"}, {"state": "void"}, {"reversed": True},
            {"obsolete": True}, {"amount_due": 0},
        ):
            self.assertFalse(InvoiceNotificationFact(**{**base, **changes}).eligible)

    def test_message_identity_is_stable_and_step_specific(self):
        settings = NotificationSettings(message_domain="mail.example.test")
        first = _message_id(settings, "dunning", "source", 0, "dunning")
        self.assertEqual(first, _message_id(
            settings, "dunning", "source", 0, "dunning"
        ))
        self.assertNotEqual(first, _message_id(
            settings, "dunning", "source", 1, "dunning"
        ))


class SMTPTests(unittest.TestCase):
    def setUp(self):
        self.settings = NotificationSettings(
            host="smtp.example.test", port=465, tls_mode="implicit",
            username="user", password="secret", sender="sender@example.test",
            timeout_seconds=10,
        )

    @patch("sceneit.billing_smtp.smtplib.SMTP_SSL")
    def test_verified_tls_and_server_acceptance(self, smtp_type):
        smtp = Mock()
        smtp.mail.return_value = (250, b"ok")
        smtp.rcpt.return_value = (250, b"ok")
        smtp.data.return_value = (250, b"queued")
        smtp_type.return_value = smtp
        TLSMailer(self.settings).send(
            "billing@example.test", "Subject", "Body",
            "<stable@mail.example.test>",
        )
        self.assertTrue(smtp_type.call_args.kwargs["context"].check_hostname)
        smtp.data.assert_called_once()

    @patch("sceneit.billing_smtp.smtplib.SMTP_SSL")
    def test_disconnect_during_data_is_ambiguous(self, smtp_type):
        smtp = Mock()
        smtp.mail.return_value = (250, b"ok")
        smtp.rcpt.return_value = (250, b"ok")
        smtp.data.side_effect = smtplib.SMTPServerDisconnected()
        smtp_type.return_value = smtp
        with self.assertRaises(SMTPDeliveryError) as raised:
            TLSMailer(self.settings).send(
                "billing@example.test", "Subject", "Body",
                "<stable@mail.example.test>",
            )
        self.assertEqual("ambiguous", raised.exception.disposition)

    @patch("sceneit.billing_smtp.smtplib.SMTP_SSL")
    def test_definite_recipient_rejection_is_permanent(self, smtp_type):
        smtp = Mock()
        smtp.mail.return_value = (250, b"ok")
        smtp.rcpt.return_value = (550, b"no")
        smtp_type.return_value = smtp
        with self.assertRaises(SMTPDeliveryError) as raised:
            TLSMailer(self.settings).send(
                "billing@example.test", "Subject", "Body",
                "<stable@mail.example.test>",
            )
        self.assertEqual("permanent", raised.exception.disposition)

    @patch("sceneit.billing_smtp.smtplib.SMTP_SSL")
    def test_aggregate_deadline_after_data_remains_ambiguous(self, smtp_type):
        smtp = Mock()
        smtp.mail.return_value = (250, b"ok")
        smtp.rcpt.return_value = (250, b"ok")
        monotonic = [0]

        def accepted_too_late(_message):
            monotonic[0] = 11
            return 250, b"queued"

        smtp.data.side_effect = accepted_too_late
        smtp_type.return_value = smtp
        with patch(
            "sceneit.billing_smtp.time.monotonic",
            side_effect=lambda: monotonic[0],
        ):
            with self.assertRaises(SMTPDeliveryError) as raised:
                TLSMailer(self.settings).send(
                    "billing@example.test", "Subject", "Body",
                    "<stable@mail.example.test>", deadline=20,
                )
        self.assertEqual("ambiguous", raised.exception.disposition)
        smtp.close.assert_called_once_with()
        smtp.quit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
