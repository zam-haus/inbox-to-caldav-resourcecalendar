"""CLI entry point for inbox-to-caldav-resourcecalendar."""

import argparse
import imaplib
import logging
import os
import signal
import sys
import threading

from inbox_to_caldav.config import ConfigError, load_config
from inbox_to_caldav.pipeline import Pipeline


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read iMIP scheduling mail from an IMAP inbox and maintain CalDAV resource calendars."
    )
    parser.add_argument(
        "--config",
        default=os.getenv("INBOX2CALDAV_CONFIG", "config.toml"),
        help="path to the TOML configuration (default: $INBOX2CALDAV_CONFIG or ./config.toml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="process the inbox once and exit instead of running until SIGINT/SIGTERM",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        metavar="SECONDS",
        help="poll interval while running continuously (default: 60)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and decide, but do not write to CalDAV, send mail, or mark mails seen",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        logging.info("received %s, shutting down", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    pipeline = Pipeline(config, dry_run=args.dry_run)
    try:
        if args.once:
            try:
                pipeline.run()
            except (OSError, imaplib.IMAP4.error, ConnectionError) as exc:
                logging.error("run failed: %s", exc)
                return 1
        else:
            pipeline.run_forever(args.interval, stop)
    finally:
        pipeline.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
