"""Core monitoring loop for displaying contract snapshots."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Mapping, MutableMapping, Optional

import pandas as pd

from tsxapipy.api.client import APIClient
from tsxapipy.api.exceptions import APIError, APIResponseParsingError
from tsxapipy.common.time_utils import UTC_TZ
from tsxapipy.monitor.contract_universe import ContractSummary, ContractUniverseManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimeframeDefinition:
    """Configuration for a timeframe column in the monitor."""

    label: str
    unit: int
    unit_number: int
    bars_to_fetch: int = 64


@dataclass
class MonitorResult:
    """Stores derived values for a single contract/timeframe combination."""

    close: float
    previous_close: float
    percent_change: float
    volume: float
    volume_average: Optional[float]
    volume_multiple: Optional[float]
    highlight: bool

    def to_display_strings(self) -> Mapping[str, str]:
        pct_str = "N/A"
        if self.previous_close:
            pct_str = f"{self.percent_change:+.2f}%"
        volume_avg_str = "N/A" if self.volume_average is None else f"{self.volume_average:,.0f}"
        volume_multiple_str = (
            "N/A" if self.volume_multiple is None else f"{self.volume_multiple:.2f}x"
        )
        return {
            "close": f"{self.close:,.2f}",
            "pct": pct_str,
            "vol": f"{self.volume:,.0f}",
            "vol_avg": volume_avg_str,
            "vol_mult": volume_multiple_str,
            "flag": "!!" if self.highlight else "",
        }


class ContractMonitor:
    """Periodically fetch OHLCV snapshots and render them as a table."""

    def __init__(
        self,
        api_client: APIClient,
        universe_manager: ContractUniverseManager,
        *,
        timeframes: Optional[Iterable[TimeframeDefinition]] = None,
        account_id: Optional[int] = None,
        update_interval_seconds: int = 300,
        volume_average_period: int = 14,
        highlight_volume_multiple: float = 2.0,
    ) -> None:
        self._api_client = api_client
        self._universe_manager = universe_manager
        self._account_id = account_id
        self._update_interval_seconds = update_interval_seconds
        self._volume_average_period = max(1, volume_average_period)
        self._highlight_volume_multiple = highlight_volume_multiple
        self._timeframes: List[TimeframeDefinition] = list(timeframes) if timeframes else [
            TimeframeDefinition("3m", unit=2, unit_number=3),
            TimeframeDefinition("15m", unit=2, unit_number=15),
            TimeframeDefinition("1h", unit=3, unit_number=1),
            TimeframeDefinition("1d", unit=4, unit_number=1),
        ]

    # ------------------------------------------------------------------
    def run_forever(
        self,
        *,
        exclude_micro: bool = True,
        limit: Optional[int] = None,
    ) -> None:
        """Continuously refresh the monitor until interrupted."""

        logger.info(
            "Starting contract monitor loop. Interval=%ss, timeframes=%s",
            self._update_interval_seconds,
            ", ".join(tf.label for tf in self._timeframes),
        )
        try:
            while True:
                start = time.time()
                self.run_once(exclude_micro=exclude_micro, limit=limit)
                elapsed = time.time() - start
                sleep_seconds = max(0.0, self._update_interval_seconds - elapsed)
                if sleep_seconds:
                    time.sleep(sleep_seconds)
        except KeyboardInterrupt:
            logger.info("Monitor stopped by user.")

    # ------------------------------------------------------------------
    def run_once(
        self,
        *,
        exclude_micro: bool = True,
        limit: Optional[int] = None,
    ) -> None:
        """Fetch a single snapshot and print it to stdout."""

        contracts = self._universe_manager.get_contracts(
            exclude_micro=exclude_micro, limit=limit
        )
        if not contracts:
            logger.warning("No contracts available for monitoring.")
            return

        logger.info("Fetching data for %d contracts.", len(contracts))
        snapshot: Dict[str, Dict[str, MonitorResult]] = {}
        for contract in contracts:
            snapshot[contract.id] = {}
            for timeframe in self._timeframes:
                try:
                    result = self._fetch_timeframe(contract.id, timeframe)
                except APIError as exc:
                    logger.warning(
                        "Failed to fetch %s data for %s: %s", timeframe.label, contract.id, exc
                    )
                    continue
                except APIResponseParsingError as exc:
                    logger.warning(
                        "Parsing error for %s %s: %s", contract.id, timeframe.label, exc
                    )
                    continue

                if result:
                    snapshot[contract.id][timeframe.label] = result

        self._render_snapshot(contracts, snapshot)

    # ------------------------------------------------------------------
    def _fetch_timeframe(
        self, contract_id: str, timeframe: TimeframeDefinition
    ) -> Optional[MonitorResult]:
        bars_to_fetch = max(timeframe.bars_to_fetch, self._volume_average_period + 1)
        end_time = datetime.now(UTC_TZ)
        start_time = end_time - self._timeframe_to_delta(timeframe, bars_to_fetch + 4)
        response = self._api_client.get_historical_bars(
            contract_id=contract_id,
            start_time_iso=start_time.isoformat(),
            end_time_iso=end_time.isoformat(),
            unit=timeframe.unit,
            unit_number=timeframe.unit_number,
            limit=bars_to_fetch,
            account_id=self._account_id,
            include_partial_bar=False,
        )
        bars = response.bars
        if len(bars) < 2:
            return None

        latest = bars[-1]
        previous = bars[-2]
        previous_close = previous.c
        percent_change = 0.0
        if previous_close:
            percent_change = ((latest.c - previous_close) / previous_close) * 100.0

        recent_volumes = [bar.v for bar in bars[-(self._volume_average_period + 1) : -1]]
        volume_average = sum(recent_volumes) / len(recent_volumes) if recent_volumes else None
        volume_multiple: Optional[float] = None
        highlight = False
        if volume_average and volume_average > 0:
            volume_multiple = latest.v / volume_average
            highlight = volume_multiple >= self._highlight_volume_multiple

        return MonitorResult(
            close=latest.c,
            previous_close=previous_close,
            percent_change=percent_change,
            volume=latest.v,
            volume_average=volume_average,
            volume_multiple=volume_multiple,
            highlight=highlight,
        )

    # ------------------------------------------------------------------
    def _render_snapshot(
        self,
        contracts: Iterable[ContractSummary],
        snapshot: Mapping[str, Mapping[str, MonitorResult]],
    ) -> None:
        columns: List[str] = ["Contract", "Name"]
        for timeframe in self._timeframes:
            base = timeframe.label
            columns.extend(
                [
                    f"{base} Close",
                    f"{base} %",
                    f"{base} Vol",
                    f"{base} VolAvg",
                    f"{base} Volx",
                    f"{base} Alert",
                ]
            )

        rows: List[MutableMapping[str, str]] = []
        for contract in contracts:
            data: MutableMapping[str, str] = {
                "Contract": contract.id,
                "Name": contract.name or "",
            }
            contract_snapshot = snapshot.get(contract.id, {})
            for timeframe in self._timeframes:
                result = contract_snapshot.get(timeframe.label)
                if not result:
                    data.update(
                        {
                            f"{timeframe.label} Close": "-",
                            f"{timeframe.label} %": "-",
                            f"{timeframe.label} Vol": "-",
                            f"{timeframe.label} VolAvg": "-",
                            f"{timeframe.label} Volx": "-",
                            f"{timeframe.label} Alert": "",
                        }
                    )
                    continue

                formatted = result.to_display_strings()
                data.update(
                    {
                        f"{timeframe.label} Close": formatted["close"],
                        f"{timeframe.label} %": formatted["pct"],
                        f"{timeframe.label} Vol": formatted["vol"],
                        f"{timeframe.label} VolAvg": formatted["vol_avg"],
                        f"{timeframe.label} Volx": formatted["vol_mult"],
                        f"{timeframe.label} Alert": formatted["flag"],
                    }
                )

            rows.append(data)

        if not rows:
            logger.info("No data retrieved for monitor.")
            return

        df = pd.DataFrame(rows, columns=columns)
        pd.set_option("display.width", 0)
        print("\n" + df.to_string(index=False))

    # ------------------------------------------------------------------
    @staticmethod
    def _timeframe_to_delta(timeframe: TimeframeDefinition, bars: int) -> timedelta:
        multiplier = timeframe.unit_number * max(1, bars)
        if timeframe.unit == 2:  # Minutes
            return timedelta(minutes=multiplier)
        if timeframe.unit == 3:  # Hours
            return timedelta(hours=multiplier)
        if timeframe.unit == 4:  # Days
            return timedelta(days=multiplier)
        # Fallback to seconds if an unknown unit is provided
        return timedelta(minutes=multiplier)
