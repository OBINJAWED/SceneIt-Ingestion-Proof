"""One-shot, redacted operator controls. Never invoked from browser requests."""
import argparse
import json

from .db import connection
from .quota import _audit, _lock, _storage_used, release_unused


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "stop", "resume", "release-unused"))
    parser.add_argument("--operator-approved", action="store_true")
    parser.add_argument("--evidence", help="Redacted evidence reference; never credentials or private content")
    parser.add_argument("--operation")
    args = parser.parse_args()
    if args.action != "status" and (
        not args.operator_approved or not args.evidence
        or not 1 <= len(args.evidence.strip()) <= 500
    ):
        parser.error("Changes require --operator-approved and --evidence")
    if args.action == "release-unused" and not args.operation:
        parser.error("release-unused requires --operation and proof of no external submission")
    with connection() as conn:
        if args.action in ("stop", "resume"):
            _lock(conn)
            conn.execute(
                "UPDATE sceneit_work_control SET stopped=%s,updated_at=clock_timestamp() WHERE singleton=true",
                (args.action == "stop",))
            _audit(conn, args.action, "application", args.evidence)
        if args.action == "release-unused":
            print(json.dumps({"released": release_unused(conn, args.operation, args.evidence)}))
            return
        state = conn.execute("SELECT stopped FROM sceneit_work_control WHERE singleton=true").fetchone()
        windows = conn.execute(
            "SELECT metric,used,allowance FROM sceneit_usage_windows WHERE scope='app' "
            "AND ends_at>clock_timestamp() ORDER BY metric").fetchall()
        storage = conn.execute(
            "SELECT count(*) AS pending_objects,COALESCE(sum(size_bytes),0) AS reserved_bytes "
            "FROM sceneit_storage_reservations WHERE state='reserved'").fetchone()
        storage["effective_bytes"] = _storage_used(conn)
        cleanup = conn.execute(
            "SELECT state,count(*) AS attempts FROM sceneit_upload_attempts "
            "WHERE state IN ('initiating','uncertain','revoke_requested') GROUP BY state"
        ).fetchall()
        print(json.dumps({"workStopped": not state or state["stopped"],
                          "application": windows, "storage": storage,
                          "uploadCleanup": cleanup}, default=str))


if __name__ == "__main__":
    main()