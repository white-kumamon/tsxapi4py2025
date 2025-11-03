"""Coordinator that bridges the tsxapipy real-time stream with websocket clients.

This module is designed for example/educational usage.  It exposes a
``DomSurfaceCoordinator`` class that keeps track of recent trades, quote data and
order-book depth for a single contract.  Updates from the SignalR market hub are
translated into compact snapshots that a websocket server can broadcast to web
clients.

The coordinator is intentionally framework agnostic; the FastAPI server in
``app.py`` simply instantiates the coordinator, registers websocket queues, and
forwards the generated snapshots to connected browsers.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Set

from tsxapipy import (
    APIClient,
    DataStream,
    StreamConnectionState,
    authenticate,
    AuthenticationError,
    ConfigurationError,
)


LOGGER = logging.getLogger(__name__)


def _ensure_datetime(value: Any) -> Optional[datetime]:
    """Convert various timestamp representations into a timezone-aware datetime."""

    if value is None:
        return None
    if isinstance(value, datetime):
        ts = value
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        # Support epoch milliseconds/seconds encoded as strings.
        if candidate.isdigit():
            try:
                if len(candidate) > 10:  # millisecond epoch
                    return datetime.fromtimestamp(int(candidate) / 1000.0, tz=timezone.utc)
                return datetime.fromtimestamp(int(candidate), tz=timezone.utc)
            except (ValueError, OSError):
                pass
        try:
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            return datetime.fromisoformat(candidate)
        except ValueError:
            LOGGER.debug("Unable to parse timestamp string '%s'", value)
            return None

    LOGGER.debug("Unsupported timestamp type: %s", type(value))
    return None


def _to_float(value: Any) -> Optional[float]:
    """Best-effort conversion to float, returning ``None`` on failure."""

    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        LOGGER.debug("Unable to convert %s (%s) to float", value, type(value))
        return None


def _normalise_side(value: Any) -> Optional[str]:
    """Normalise side identifiers coming from the market depth feed."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        if int(value) == 0:
            return "bid"
        if int(value) == 1:
            return "ask"
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"bid", "b", "buy", "long", "0"}:
            return "bid"
        if lowered in {"ask", "a", "sell", "short", "1"}:
            return "ask"
    return None


def _normalise_depth_action(value: Any) -> str:
    """Map depth action codes to ``new``, ``update`` or ``delete``."""

    if value is None:
        return "update"
    if isinstance(value, (int, float)):
        mapping = {0: "new", 1: "update", 2: "delete"}
        return mapping.get(int(value), "update")
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"new", "add", "insert"}:
            return "new"
        if lowered in {"delete", "remove", "cancel"}:
            return "delete"
    return "update"


class DomSurfaceCoordinator:
    """Maintain a rolling snapshot of trades, quotes and order-book depth."""

    def __init__(
        self,
        contract_id: str,
        loop: asyncio.AbstractEventLoop,
        *,
        trade_history: int = 500,
        depth_levels: int = 20,
        demo_mode: bool = False,
        demo_seed: Optional[int] = None,
    ) -> None:
        self.contract_id = contract_id
        self.loop = loop
        self.trade_history = trade_history
        self.depth_levels = depth_levels
        self.demo_mode = demo_mode

        self._rng = random.Random(demo_seed)
        self._demo_thread: Optional[threading.Thread] = None

        self._trades: Deque[Dict[str, Any]] = deque(maxlen=trade_history)
        self._volume_trades: Deque[Dict[str, Any]] = deque(maxlen=trade_history)
        self._order_book: Dict[str, Dict[float, float]] = {"bid": {}, "ask": {}}
        self._quote: Dict[str, float] = {}

        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()

        self._latest_snapshot: Optional[Dict[str, Any]] = None
        self._connection_state: str = StreamConnectionState.NOT_INITIALIZED.name
        self._last_error: Optional[str] = None

        self._api_client: Optional[APIClient] = None
        self._data_stream: Optional[DataStream] = None
        self._stream_thread: Optional[threading.Thread] = None
        self._heartbeat_thread: Optional[threading.Thread] = None

        self.logger = logging.getLogger(f"{__name__}.DomSurfaceCoordinator[{contract_id}]")

    # ------------------------------------------------------------------
    # Lifecycle management
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Authenticate and start the underlying :class:`DataStream`."""

        if self.demo_mode:
            if self._demo_thread and self._demo_thread.is_alive():
                self.logger.info("Demo generator already running for %s", self.contract_id)
                return

            self.logger.info("Starting demo mode data generator for %s", self.contract_id)
            self._last_error = None
            self._connection_state = "DEMO"
            self._stop_event.clear()

            self._demo_thread = threading.Thread(target=self._demo_loop, daemon=True)
            self._demo_thread.start()

            self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
            self._heartbeat_thread.start()
            return

        if self._stream_thread and self._stream_thread.is_alive():
            self.logger.info("Stream already running for %s", self.contract_id)
            return

        try:
            token, acquired_at = authenticate()
        except (AuthenticationError, ConfigurationError) as exc:
            self.logger.error("Authentication failed: %s", exc)
            raise

        if not token or not acquired_at:
            raise AuthenticationError("Authentication did not return a token.")

        self._api_client = APIClient(initial_token=token, token_acquired_at=acquired_at)
        self._data_stream = DataStream(
            api_client=self._api_client,
            contract_id_to_subscribe=self.contract_id,
            on_quote_callback=self._handle_quote,
            on_trade_callback=self._handle_trade,
            on_depth_callback=self._handle_depth,
            on_error_callback=self._handle_stream_error,
            on_state_change_callback=self._handle_state_change,
            auto_subscribe_quotes=True,
            auto_subscribe_trades=True,
            auto_subscribe_depth=True,
        )

        self._stop_event.clear()

        self._stream_thread = threading.Thread(target=self._run_stream, daemon=True)
        self._stream_thread.start()

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

    def _run_stream(self) -> None:
        assert self._data_stream is not None
        try:
            started = self._data_stream.start()
            if not started:
                self.logger.error("DataStream.start() returned False for %s", self.contract_id)
                self._last_error = "Unable to start SignalR connection"
                self._broadcast_snapshot(force=True)
        except Exception as exc:  # pragma: no cover - defensive logging
            self.logger.exception("Unhandled exception while starting DataStream: %s", exc)
            self._last_error = str(exc)
            self._broadcast_snapshot(force=True)

    def stop(self) -> None:
        """Stop the data stream and heartbeat threads."""

        self._stop_event.set()

        if self._data_stream:
            try:
                self._data_stream.stop("Dashboard shutdown")
            except Exception as exc:  # pragma: no cover - defensive logging
                self.logger.warning("Error stopping DataStream: %s", exc)

        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=5)
        if self._demo_thread and self._demo_thread.is_alive():
            self._demo_thread.join(timeout=5)
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            self._heartbeat_thread.join(timeout=5)

    async def register(self) -> asyncio.Queue:
        """Register a websocket subscriber and return its queue."""

        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.add(queue)
            snapshot = self._latest_snapshot

        if snapshot:
            await queue.put(snapshot)
        return queue

    def unregister(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    # ------------------------------------------------------------------
    # Stream callbacks
    # ------------------------------------------------------------------
    def _handle_state_change(self, state_name: str) -> None:
        self.logger.info("Stream state changed to %s", state_name)
        with self._lock:
            self._connection_state = state_name
        self._broadcast_snapshot(force=True)

    def _handle_stream_error(self, error: Any) -> None:
        error_str = str(error)
        self.logger.error("Stream error: %s", error_str)
        with self._lock:
            self._last_error = error_str
        self._broadcast_snapshot(force=True)

    def _handle_quote(self, payload: Dict[str, Any]) -> None:
        bid = _to_float(payload.get("bid") or payload.get("Bid") or payload.get("b"))
        ask = _to_float(payload.get("ask") or payload.get("Ask") or payload.get("a"))
        last_price = _to_float(
            payload.get("last") or payload.get("Last") or payload.get("lastPrice") or payload.get("price")
        )

        with self._lock:
            self._quote = {
                key: value
                for key, value in {"bid": bid, "ask": ask, "last": last_price}.items()
                if value is not None
            }

        self._broadcast_snapshot(force=True)

    def _handle_trade(self, trade: Dict[str, Any]) -> None:
        timestamp = (
            _ensure_datetime(trade.get("timestamp") or trade.get("Timestamp") or trade.get("ts") or trade.get("time"))
            or datetime.now(timezone.utc)
        )
        price = _to_float(trade.get("price") or trade.get("Price") or trade.get("p"))
        volume = _to_float(trade.get("volume") or trade.get("Volume") or trade.get("v") or trade.get("qty"))
        side = _normalise_side(trade.get("side") or trade.get("Side") or trade.get("s"))

        if price is None:
            return
        if volume is None:
            volume = 0.0

        trade_entry = {
            "timestamp": timestamp,
            "price": price,
            "volume": max(volume, 0.0),
            "side": side,
        }

        with self._lock:
            self._trades.append(trade_entry)
            if trade_entry["volume"] > 0:
                self._volume_trades.append(trade_entry)

        self._broadcast_snapshot(force=True)

    def _handle_depth(self, updates: List[Dict[str, Any]]) -> None:
        if not isinstance(updates, list):
            return

        with self._lock:
            book = self._order_book
            for entry in updates:
                price = _to_float(entry.get("price") or entry.get("Price") or entry.get("p"))
                if price is None:
                    continue
                side = _normalise_side(entry.get("side") or entry.get("Side") or entry.get("s"))
                if side not in {"bid", "ask"}:
                    continue
                volume = _to_float(entry.get("volume") or entry.get("Volume") or entry.get("v") or entry.get("qty"))
                if volume is None:
                    volume = 0.0
                action = _normalise_depth_action(entry.get("type") or entry.get("Type") or entry.get("t"))

                rounded_price = round(price, 6)
                side_book = book[side]

                if action == "delete" or volume <= 0:
                    side_book.pop(rounded_price, None)
                else:
                    side_book[rounded_price] = volume

        self._broadcast_snapshot(force=True)

    # ------------------------------------------------------------------
    # Demo data generator
    # ------------------------------------------------------------------
    def _demo_loop(self) -> None:
        """Generate synthetic snapshots when running without live market data."""

        price = self._rng.uniform(90.0, 110.0)
        spread = 0.25
        volume_threshold = 5.0

        while not self._stop_event.is_set():
            price = max(0.01, price + self._rng.gauss(0, 0.35))
            last_price = round(price, 2)
            bid_price = round(last_price - spread, 2)
            ask_price = round(last_price + spread, 2)

            trade_volume = max(0.1, self._rng.lognormvariate(1.2, 0.45))
            trade_side = "bid" if self._rng.random() > 0.5 else "ask"
            timestamp = datetime.now(timezone.utc)

            with self._lock:
                trade_entry = {
                    "timestamp": timestamp,
                    "price": last_price,
                    "volume": round(trade_volume, 2),
                    "side": trade_side,
                }
                self._quote = {"bid": bid_price, "ask": ask_price, "last": last_price}
                self._trades.append(trade_entry)
                if trade_entry["volume"] >= volume_threshold:
                    self._volume_trades.append(trade_entry)

                bid_levels: Dict[float, float] = {}
                ask_levels: Dict[float, float] = {}
                for level in range(1, self.depth_levels + 1):
                    level_variance = self._rng.uniform(0.5, 3.0)
                    bid_levels[round(bid_price - level * spread, 2)] = round(
                        max(0.1, self._rng.lognormvariate(1.0, 0.55) * level_variance), 2
                    )
                    ask_levels[round(ask_price + level * spread, 2)] = round(
                        max(0.1, self._rng.lognormvariate(1.0, 0.55) * level_variance), 2
                    )

                self._order_book = {"bid": bid_levels, "ask": ask_levels}
                self._connection_state = "DEMO"
                self._last_error = None

            self._broadcast_snapshot(force=True)
            time.sleep(0.75)

    # ------------------------------------------------------------------
    # Snapshot + broadcast helpers
    # ------------------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(1.0)
            self._broadcast_snapshot(force=False)

    def _broadcast_snapshot(self, *, force: bool) -> None:
        with self._lock:
            if force or self._latest_snapshot is None:
                snapshot = self._build_snapshot_locked()
                self._latest_snapshot = snapshot
            else:
                snapshot = self._latest_snapshot

        if snapshot is None:
            return

        def _dispatch() -> None:
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(snapshot)
                except asyncio.QueueFull:
                    self.logger.warning("Subscriber queue full; dropping snapshot")

        self.loop.call_soon_threadsafe(_dispatch)

    def _build_snapshot_locked(self) -> Dict[str, Any]:
        trades_payload = [
            {
                "timestamp": trade["timestamp"].isoformat(),
                "price": trade["price"],
                "volume": trade["volume"],
                "side": trade["side"],
            }
            for trade in list(self._trades)
        ]

        volume_bubbles = [
            {
                "timestamp": trade["timestamp"].isoformat(),
                "price": trade["price"],
                "volume": trade["volume"],
                "side": trade["side"],
            }
            for trade in list(self._volume_trades)
        ]

        bids_sorted = sorted(self._order_book["bid"].items(), key=lambda kv: kv[0], reverse=True)
        asks_sorted = sorted(self._order_book["ask"].items(), key=lambda kv: kv[0])

        bids_payload = [
            {"price": price, "volume": volume, "level": idx + 1}
            for idx, (price, volume) in enumerate(bids_sorted[: self.depth_levels])
        ]
        asks_payload = [
            {"price": price, "volume": volume, "level": idx + 1}
            for idx, (price, volume) in enumerate(asks_sorted[: self.depth_levels])
        ]

        snapshot = {
            "contract_id": self.contract_id,
            "state": self._connection_state,
            "last_error": self._last_error,
            "last_update": datetime.now(timezone.utc).isoformat(),
            "quote": self._quote,
            "trades": trades_payload,
            "volume_bubbles": volume_bubbles,
            "order_book": {
                "bids": bids_payload,
                "asks": asks_payload,
            },
        }

        return snapshot

    @property
    def latest_snapshot(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._latest_snapshot

