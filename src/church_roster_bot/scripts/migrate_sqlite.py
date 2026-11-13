"""Operator CLI: explicit source/target, dry-run by default, exclusive sanitized report."""

import argparse
import json
import os
from pathlib import Path

from church_roster_bot.database.errors import TransferError
from church_roster_bot.database.transfer import transfer_sqlite


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--target-env", required=True, help="Environment variable containing target URL"
    )
    parser.add_argument("--schema", required=True)
    parser.add_argument(
        "--report", type=Path, required=True, help="New report file; never overwritten"
    )
    parser.add_argument("--upgrade-copy", action="store_true")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--verify", action="store_true")
    parser.add_argument("--confirm")
    parser.add_argument("--offline-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        handle = args.report.open("x", encoding="utf-8")
    except OSError:
        print("report_must_be_a_new_writable_file")
        return 2
    code = 0
    with handle:
        try:
            url = os.environ.get(args.target_env)
            if not url:
                raise TransferError("target_environment_variable_required")
            report = transfer_sqlite(
                args.source,
                url,
                schema=args.schema,
                mode="apply" if args.apply else "verify" if args.verify else "dry-run",
                confirmation=args.confirm,
                offline_confirmed=args.offline_confirmed,
                upgrade_copy=args.upgrade_copy,
            )
        except TransferError as exc:
            report, code = {"status": "rejected", "error": exc.report()}, 2
        except KeyboardInterrupt:
            report, code = {"status": "interrupted_verify_before_retry"}, 130
        except Exception as exc:
            report, code = (
                {"status": "failed_verify_before_retry", "error_type": type(exc).__name__},
                2,
            )
        try:
            json.dump(report, handle, ensure_ascii=True, sort_keys=True, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError:
            print("report_write_failed_verify_destination_before_retry")
            return 2
    print(report["status"])
    return code


if __name__ == "__main__":
    raise SystemExit(main())
