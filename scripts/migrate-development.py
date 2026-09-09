"""Explicit development migration status/upgrade. Never called by web/worker/build."""
import argparse
import os
import sys
from pathlib import Path

# Make the checked-in Python package importable regardless of the caller's cwd.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "artifacts" / "api-server")
)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development", action="store_true", required=True)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--status", action="store_true")
    operation.add_argument("--upgrade", action="store_true")
    args = parser.parse_args()
    if os.environ.get("REPLIT_DEPLOYMENT"):
        raise SystemExit("Development migrations cannot run in a deployment.")
    from sceneit.migrate import status, upgrade

    if args.status:
        for item in status():
            print(f"{item['version']:03d} {item['status']:7s} {item['name']}")
    else:
        applied = upgrade()
        print("Schema is current." if not applied else "Applied: " + ", ".join(applied))