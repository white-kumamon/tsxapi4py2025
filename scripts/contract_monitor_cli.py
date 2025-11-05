"""CLI entry point for the contract monitoring dashboard."""

# pylint: disable=too-many-locals

import argparse
import logging
import os
import sys
from pathlib import Path

# -- Ensure project src/ is on path --------------------------------------------------
SRC_PATH = Path(__file__).resolve().parents[1] / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))
# ------------------------------------------------------------------------------------

from tsxapipy.auth import authenticate, AuthenticationError
from tsxapipy.api import APIClient, APIError
from tsxapipy.api.exceptions import (
    APIResponseParsingError,
    ConfigurationError,
    LibraryError,
)
from tsxapipy.monitor import (
    ContractMonitor,
    ContractUniverseManager,
    TimeframeDefinition,
)

LOG_FORMAT = "%(asctime)s - %(name)s [%(levelname)s]: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("contract_monitor_cli")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a multi-timeframe contract monitoring dashboard.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.getenv("TSXAPIPY_CACHE_DIR", Path.home() / ".tsxapipy")),
        help="Directory where the contract universe cache should be stored.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        help="Refresh interval for the monitor (seconds).",
    )
    parser.add_argument(
        "--volume-period",
        type=int,
        default=14,
        help="Number of bars to include in the rolling average volume calculation.",
    )
    parser.add_argument(
        "--volume-multiple",
        type=float,
        default=2.0,
        help="Highlight threshold expressed as a multiple of the rolling volume average.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum number of contracts to display (after filtering).",
    )
    parser.add_argument(
        "--include-micro",
        action="store_true",
        help="Include micro contracts in the monitor output.",
    )
    parser.add_argument(
        "--run-once",
        action="store_true",
        help="Fetch and display a single snapshot, then exit.",
    )
    parser.add_argument(
        "--account-id",
        type=int,
        default=None,
        help="Optional account identifier passed to historical data requests.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--timeframe",
        action="append",
        metavar="LABEL:UNIT:NUMBER",
        help=(
            "Additional timeframe definition in the format label:unit:unitNumber. "
            "Example: 30m:2:30 would add a 30-minute column."
        ),
    )
    return parser.parse_args()


def build_timeframes(user_args: argparse.Namespace):
    timeframes = [
        TimeframeDefinition("3m", unit=2, unit_number=3),
        TimeframeDefinition("15m", unit=2, unit_number=15),
        TimeframeDefinition("1h", unit=3, unit_number=1),
        TimeframeDefinition("1d", unit=4, unit_number=1),
    ]
    if not user_args.timeframe:
        return timeframes

    for tf_arg in user_args.timeframe:
        try:
            label, unit_str, number_str = tf_arg.split(":")
            timeframes.append(
                TimeframeDefinition(
                    label.strip(),
                    unit=int(unit_str),
                    unit_number=int(number_str),
                )
            )
        except ValueError:
            logger.warning("Ignoring invalid timeframe definition: %s", tf_arg)
    return timeframes


def main() -> None:
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        logger.debug("Debug logging enabled.")

    try:
        token, acquired_at = authenticate()
    except ConfigurationError as exc:
        logger.error("Configuration error: %s", exc)
        return
    except AuthenticationError as exc:
        logger.error("Authentication failed: %s", exc)
        return
    except APIResponseParsingError as exc:
        logger.error("Authentication response parsing error: %s", exc)
        if exc.raw_response_text:
            logger.error("Raw response: %s", exc.raw_response_text[:400])
        return

    api_client = APIClient(initial_token=token, token_acquired_at=acquired_at)
    universe_manager = ContractUniverseManager(api_client, cache_directory=args.cache_dir)
    timeframes = build_timeframes(args)
    monitor = ContractMonitor(
        api_client,
        universe_manager,
        timeframes=timeframes,
        account_id=args.account_id,
        update_interval_seconds=args.interval,
        volume_average_period=args.volume_period,
        highlight_volume_multiple=args.volume_multiple,
    )

    try:
        if args.run_once:
            monitor.run_once(exclude_micro=not args.include_micro, limit=args.limit)
        else:
            monitor.run_forever(exclude_micro=not args.include_micro, limit=args.limit)
    except APIError as exc:
        logger.error("API error while running monitor: %s", exc)
    except LibraryError as exc:
        logger.error("Library error while running monitor: %s", exc)


if __name__ == "__main__":
    main()
