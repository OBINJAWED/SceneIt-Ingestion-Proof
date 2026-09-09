"""Explicit bounded operator commands for billing notifications."""
import argparse

from .billing_config import billing_settings
from .billing_notification_provider import StripeInvoiceNotificationResolver
from .billing_notifications import (
    dispatch, enqueue_due_dunning, notification_health,
    scan_webhook_incidents,
)
from .billing_provider import StripeBillingProvider
from .billing_smtp import notification_settings


def _limit(value):
    value = int(value)
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError("limit must be between 1 and 100")
    return value


def _resolver():
    settings = billing_settings()
    if not settings.enabled:
        raise RuntimeError("billing is disabled")
    return StripeInvoiceNotificationResolver(StripeBillingProvider(settings))


def run_once(*, limit=25):
    """Run one bounded pass; this function does not install a scheduler."""
    resolver = _resolver()
    enqueued = enqueue_due_dunning(resolver, limit=limit)
    incidents = scan_webhook_incidents(limit=limit)
    delivered = dispatch(resolver, limit=limit)
    return {
        "enqueued": enqueued,
        "incidents": incidents,
        **delivered,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Bounded, explicitly approved billing notifications"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("enqueue", "scan", "dispatch", "run-once"):
        command = commands.add_parser(name)
        command.add_argument("--limit", type=_limit, default=25)
        command.add_argument("--operator-approved", action="store_true")
    commands.add_parser("health")
    args = parser.parse_args(argv)

    configured = notification_settings()
    if args.command == "health":
        if not configured.enabled:
            print("Billing notifications are disabled.")
            return 0
        result = notification_health()
        states = ",".join(
            f"{row['state']}={row['count']}" for row in result["deliveries"]
        ) or "none"
        runners = ",".join(row["runner"] for row in result["runners"]) or "none"
        print(f"Delivery states: {states}; runners: {runners}.")
        return 0
    if not args.operator_approved:
        parser.error(f"{args.command} requires --operator-approved")
    if not (
        configured.enabled
        and configured.scheduler_enabled
        and (configured.dunning_enabled or configured.alerts_enabled)
    ):
        parser.error("notification activation gates are not enabled")

    if args.command == "enqueue":
        count = enqueue_due_dunning(_resolver(), limit=args.limit)
        print(f"Enqueued {count} dunning notification(s).")
    elif args.command == "scan":
        count = scan_webhook_incidents(limit=args.limit)
        print(f"Changed {count} webhook incident notification(s).")
    elif args.command == "dispatch":
        result = dispatch(_resolver(), limit=args.limit)
        print(
            f"Claimed {result['claimed']}; accepted {result['accepted']}; "
            f"ambiguous {result['ambiguous']}."
        )
    else:
        result = run_once(limit=args.limit)
        print(
            f"Enqueued {result['enqueued']}; incidents {result['incidents']}; "
            f"claimed {result['claimed']}; accepted {result['accepted']}; "
            f"ambiguous {result['ambiguous']}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())