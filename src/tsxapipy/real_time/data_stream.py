# tsxapipy/real_time/data_stream.py

import logging
# import json # Not directly used in this version of the file
import threading
import time 
from typing import Dict, Any, Callable, Optional, List

from signalrcore.hub_connection_builder import HubConnectionBuilder
from tsxapipy.config import MARKET_HUB_URL
from tsxapipy.api import APIClient 
from .stream_state import StreamConnectionState
from signalrcore.messages.completion_message import CompletionMessage
import uuid

logger = logging.getLogger(__name__)

# Keep the import-time log lightweight so consumers do not see an unexpected
# critical message simply for importing the module.
logger.debug("Loading DataStream class definition (start method available).")

# Callback type aliases
# The DataStream user will provide callbacks that expect the actual data payload (e.g., Dict for quote)
QuoteCallback = Callable[[Dict[str, Any]], None]
TradeCallback = Callable[[Dict[str, Any]], None]
DepthCallback = Callable[[List[Dict[str, Any]]], None] # Depth payload is a list of depth level dicts
StreamErrorCallback = Callable[[Any], None]
StreamStateChangeCallback = Callable[[str], None]

class DataStream:
    """
    Handles real-time market data streaming from the TopStepX Market Hub
    using SignalR.
    Uses token in URL query and skip_negotiation=True.
    Correctly handles message_args from signalrcore for callbacks.
    """
    
    def __init__(
        self, 
        api_client: APIClient, 
        contract_id_to_subscribe: str,
        on_quote_callback: Optional[QuoteCallback] = None,
        on_trade_callback: Optional[TradeCallback] = None,
        on_depth_callback: Optional[DepthCallback] = None,
        on_error_callback: Optional[StreamErrorCallback] = None,
        on_state_change_callback: Optional[StreamStateChangeCallback] = None,
        auto_subscribe_quotes: bool = False,
        auto_subscribe_trades: bool = False,
        auto_subscribe_depth: bool = True # Depth defaults to True to power DOM/heatmaps
    ):
        self.logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}[{contract_id_to_subscribe}]") # Instance specific logger
        
        self.api_client = api_client
        self.contract_id_subscribed: str = contract_id_to_subscribe 
        
        self._callbacks = {
            "quote": on_quote_callback,
            "trade": on_trade_callback,
            "depth": on_depth_callback,
        }
        
        self.on_error_callback = on_error_callback
        self.on_state_change_callback = on_state_change_callback

        self.auto_subscribe_quotes = auto_subscribe_quotes
        self.auto_subscribe_trades = auto_subscribe_trades
        self.auto_subscribe_depth = auto_subscribe_depth

        self.connection: Optional[HubConnectionBuilder] = None
        self.connection_status = StreamConnectionState.NOT_INITIALIZED
        
        self._current_token_for_url: Optional[str] = None 
        self._wss_hub_url_no_token: str = "" 
        self._hub_url_with_token: Optional[str] = None # Full URL including token

        self._connection_lock = threading.RLock() 
        self._is_manually_stopping = False 

        self._prepare_websocket_url_base()
        try:
            self._refresh_auth_token_and_url() 
        except Exception as e:
            self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Failed to get initial token/URL during __init__: {e}", exc_info=True)
            self._set_connection_state(StreamConnectionState.ERROR, f"Initial token/URL fail: {type(e).__name__}")
        
        self._build_connection() 
        self.logger.info(f"DataStream initialized for contract: {self.contract_id_subscribed}")

    def _prepare_websocket_url_base(self):
        if MARKET_HUB_URL.startswith("https://"):
            self._wss_hub_url_no_token = "wss://" + MARKET_HUB_URL[len("https://"):]
        elif MARKET_HUB_URL.startswith("http://"):
            self._wss_hub_url_no_token = "ws://" + MARKET_HUB_URL[len("http://"):]
        else:
            self.logger.warning(f"MARKET_HUB_URL '{MARKET_HUB_URL}' does not start with http(s). Using as is for wss base.")
            self._wss_hub_url_no_token = MARKET_HUB_URL
        self.logger.debug(f"DataStream (Contract: {self.contract_id_subscribed}): Prepared base WebSocket URL: {self._wss_hub_url_no_token}")

    def _refresh_auth_token_and_url(self): # Renamed from _refresh_auth_token
        self.logger.debug(f"DataStream (Contract: {self.contract_id_subscribed}): Refreshing auth token and forming hub URL.")
        if not self.api_client:
            raise ConnectionError("DataStream: APIClient not available for token refresh.")
        
        self._current_token_for_url = self.api_client.current_token 
        if not self._current_token_for_url:
            raise ConnectionError("DataStream: Failed to obtain a valid token from APIClient.")
        
        if not self._wss_hub_url_no_token:
            self._prepare_websocket_url_base()
            if not self._wss_hub_url_no_token:
                 self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Base WSS URL not set, cannot form full hub URL.")
                 raise ConnectionError("DataStream: Base WSS Hub URL is not prepared.")

        self._hub_url_with_token = f"{self._wss_hub_url_no_token}?access_token={self._current_token_for_url}"
        self.logger.debug(f"DataStream (Contract: {self.contract_id_subscribed}): Auth token refreshed. Full Hub URL (token hidden): {self._wss_hub_url_no_token}?access_token=TOKEN_HIDDEN")

    def _build_connection(self):
        if not self._hub_url_with_token: # This should be set by _refresh_auth_token_and_url
            self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Cannot build connection, _hub_url_with_token is missing.")
            try:
                self._refresh_auth_token_and_url()
            except ConnectionError as e:
                self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Failed to refresh token/URL during _build_connection: {e}")
                return
            if not self._hub_url_with_token:
                 self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): _hub_url_with_token still missing after refresh attempt.")
                 return

        self.logger.debug(f"DataStream (Contract: {self.contract_id_subscribed}): Building new SignalR connection with token in URL and skip_negotiation=True.")
        
        self.connection = HubConnectionBuilder() \
            .with_url(self._hub_url_with_token, options={ # MODIFIED: Use URL WITH token query param
                "verify_ssl": True, 
                "skip_negotiation": True, # MODIFIED: As per documentation findings
                # "headers": {} # MODIFIED: No Authorization header needed here now
            }) \
            .with_automatic_reconnect({
                "type": "interval",
                "keep_alive_interval": 10,
                "intervals": [0, 2, 5, 10, 15, 30, 60, 120] 
            }) \
            .build()

        self.connection.on_open(self._on_open)
        self.connection.on_close(self._on_close)
        self.connection.on_error(self._on_signalr_error) 
        
        if self._callbacks["quote"]:
            self.connection.on("GatewayQuote", self._handle_quote_message)
        if self._callbacks["trade"]:
            self.connection.on("GatewayTrade", self._handle_trade_message)
        if self._callbacks["depth"]:
            self.connection.on("GatewayDepth", self._handle_depth_message)
        
        self.logger.debug(f"DataStream (Contract: {self.contract_id_subscribed}): SignalR event handlers registered.")

    def _set_connection_state(self, new_state: StreamConnectionState, reason: Optional[str] = None):
        with self._connection_lock:
            if self.connection_status != new_state:
                old_state_name = self.connection_status.name
                self.connection_status = new_state
                log_msg = f"DataStream (Contract: {self.contract_id_subscribed}): State changed from {old_state_name} to {new_state.name}."
                if reason: log_msg += f" Reason: {reason}"
                self.logger.info(log_msg)
                if self.on_state_change_callback:
                    try:
                        self.on_state_change_callback(new_state.name)
                    except Exception as e_cb:
                        self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Error in on_state_change_callback: {e_cb}", exc_info=True)
    
    def _on_open(self):
        self._set_connection_state(StreamConnectionState.CONNECTED, "SignalR connection opened")
        self._send_subscriptions()

    def _on_close(self):
        if not self._is_manually_stopping: 
            self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): SignalR connection closed unexpectedly. Current state: {self.connection_status.name}.")
            self._set_connection_state(StreamConnectionState.DISCONNECTED, "Connection closed (possibly after failed retries or server disconnect)")
        else: 
             self._set_connection_state(StreamConnectionState.DISCONNECTED, "Connection stopped by client")

    def _on_signalr_error(self, error_payload: Any):
        """
        Callback for fundamental SignalR connection errors or server-initiated error completions.
        Logs detailed information from CompletionMessage objects.
        """
        # Default error message string, will be updated if more specific info is found
        error_message_for_log_and_state = str(error_payload) if error_payload else "Unknown SignalR error"
        log_as_exception = isinstance(error_payload, Exception) # Should we log with exc_info?

        if isinstance(error_payload, CompletionMessage):
            invocation_id_str = getattr(error_payload, 'invocation_id', 'N/A')
            # Check if it's an error completion (type 3 usually, but better to check error attribute)
            if error_payload.error is not None:
                server_error_details = error_payload.error
                self.logger.error(
                    f"DataStream ({self.contract_id_subscribed}): SignalR CompletionMessage ERROR! "
                    f"Invocation ID: {invocation_id_str}, "
                    f"Server Error: '{server_error_details}', " # Specific server error string
                    f"Result (should be None if error): {getattr(error_payload, 'result', 'N/A')}"
                )
                error_message_for_log_and_state = f"HubInvocationError (ID: {invocation_id_str}): {server_error_details}"
                # Don't log this as an exception with traceback if it's a 'handled' server error string in CompletionMessage
                log_as_exception = False
            else:
                # This is a CompletionMessage but without an error string.
                # It could be a successful completion of a void method, or a non-error completion
                # that signalrcore still routes through its error handling for some reason.
                self.logger.info(
                    f"DataStream ({self.contract_id_subscribed}): SignalR CompletionMessage (no explicit error string found in .error attribute). "
                    f"Invocation ID: {invocation_id_str}, "
                    f"Result: {getattr(error_payload, 'result', 'N/A')}, Type: {error_payload.type}. "
                    f"Full object: {error_payload}" # Log the full object for inspection
                )
                # For state reason, use a generic message if it's not a Python exception
                if not log_as_exception: # If error_payload is not an Exception itself
                     error_message_for_log_and_state = (f"CompletionMessage (type {error_payload.type}) without explicit "
                                                        f"error string. InvId: {invocation_id_str}")
        else:
            # Not a CompletionMessage, treat as a general transport error or other SignalR client error
            self.logger.error(
                f"DataStream ({self.contract_id_subscribed}): Underlying SignalR transport error or unhandled message type: {error_message_for_log_and_state}",
                exc_info=log_as_exception # Log traceback if it's a Python exception object
            )

        # Set state to ERROR and invoke user's error callback
        # Truncate the reason string if it's too long for a state reason
        self._set_connection_state(StreamConnectionState.ERROR, error_message_for_log_and_state[:150])

        if self.on_error_callback:
            try:
                self.on_error_callback(error_payload) # Pass the original error payload
            except Exception as e_cb:
                self.logger.error(
                    f"DataStream ({self.contract_id_subscribed}): Error in user's on_error_callback: {e_cb}",
                    exc_info=True
                )
                
    # --- MODIFIED Message Handlers to accept List[Any] from signalrcore ---
    def _handle_quote_message(self, message_args: List[Any]):
        self.logger.debug(f"DataStream ({self.contract_id_subscribed}): _handle_quote_message raw args: {message_args}")
        if not isinstance(message_args, list) or len(message_args) < 2:
            self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayQuote unexpected message_args format: {message_args}")
            return
        
        contract_id_from_event = message_args[0]
        quote_payload = message_args[1]

        if not isinstance(quote_payload, dict) or not isinstance(contract_id_from_event, str):
            self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayQuote - Invalid types. ContractID: {type(contract_id_from_event)}, Payload: {type(quote_payload)}")
            return

        if contract_id_from_event == self.contract_id_subscribed and self._callbacks["quote"]:
            try: 
                self.logger.debug(f"DataStream ({self.contract_id_subscribed}): Invoking on_quote_callback with: {quote_payload}")
                self._callbacks["quote"](quote_payload)
            except Exception as e: 
                self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Error in on_quote_callback: {e}", exc_info=True)
        elif contract_id_from_event != self.contract_id_subscribed:
             self.logger.warning(f"DataStream: Received quote for mismatched contract_id: {contract_id_from_event} (expected {self.contract_id_subscribed})")

    def _handle_trade_message(self, message_args: List[Any]):
        self.logger.debug(f"DataStream ({self.contract_id_subscribed}): _handle_trade_message raw args: {message_args}")
        if not isinstance(message_args, list) or len(message_args) < 2:
            self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayTrade unexpected message_args format (initial check): {message_args}")
            return

        contract_id_from_event = message_args[0]
        trade_data_payload = message_args[1] # This is the part that's sometimes a list

        if not isinstance(contract_id_from_event, str):
            self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayTrade - Invalid type for ContractID: {type(contract_id_from_event)}")
            return
        
        # Check if the callback for trades is even set
        if not self._callbacks["trade"]:
            self.logger.debug(f"DataStream (Contract: {self.contract_id_subscribed}): No trade callback registered. Ignoring GatewayTrade message.")
            return

        # Check if the payload is a list (most common case from logs)
        if isinstance(trade_data_payload, list):
            self.logger.debug(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayTrade payload is a list. Iterating. Count: {len(trade_data_payload)}")
            for single_trade_dict in trade_data_payload:
                if not isinstance(single_trade_dict, dict):
                    self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayTrade - Item in trade list is not a dict: {type(single_trade_dict)}. Item: {single_trade_dict}")
                    continue 

                self.logger.info(f"DataStream ({self.contract_id_subscribed}): _handle_trade_message PROCESSING TRADE (from list): {single_trade_dict}")
                try: 
                    self._callbacks["trade"](single_trade_dict)
                except Exception as e: 
                    self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Error in on_trade_callback for trade {single_trade_dict}: {e}", exc_info=True)
        
        # Fallback: What if the payload is a single dictionary directly? (Less likely based on logs, but defensive)
        elif isinstance(trade_data_payload, dict):
            self.logger.info(f"DataStream ({self.contract_id_subscribed}): GatewayTrade payload is a single dict. PROCESSING TRADE (direct dict): {trade_data_payload}")
            try:
                self._callbacks["trade"](trade_data_payload)
            except Exception as e:
                self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Error in on_trade_callback for single trade dict {trade_data_payload}: {e}", exc_info=True)
        
        else: # Payload is neither a list nor a dict
            self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayTrade - Expected payload (message_args[1]) to be a list or dict of trades, got: {type(trade_data_payload)}. Data: {trade_data_payload}")
            return

        # No separate check for contract_id_from_event == self.contract_id_subscribed here
        # because the handler is registered only for the target "GatewayTrade".
        # If the server sends events for other contracts on this target, it's unexpected.
        # A check like `if contract_id_from_event != self.contract_id_subscribed:` could be added
        # inside the loop/processing block if necessary.
        
    def _handle_depth_message(self, message_args: List[Any]):
        self.logger.debug(f"DataStream ({self.contract_id_subscribed}): _handle_depth_message raw args: {message_args}")
        if not isinstance(message_args, list) or len(message_args) < 2:
            self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayDepth unexpected message_args format: {message_args}")
            return
            
        contract_id_from_event = message_args[0]
        depth_payload_list = message_args[1] # This is a List[Dict[str, Any]]

        if not isinstance(depth_payload_list, list) or not isinstance(contract_id_from_event, str):
            self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): GatewayDepth - Invalid types. ContractID: {type(contract_id_from_event)}, Payload: {type(depth_payload_list)}")
            return

        if contract_id_from_event == self.contract_id_subscribed and self._callbacks["depth"]:
            try: 
                self.logger.debug(f"DataStream ({self.contract_id_subscribed}): Invoking on_depth_callback with list of length {len(depth_payload_list)}.")
                self._callbacks["depth"](depth_payload_list)
            except Exception as e: 
                self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Error in on_depth_callback: {e}", exc_info=True)
        elif contract_id_from_event != self.contract_id_subscribed:
             self.logger.warning(f"DataStream: Received depth for mismatched contract_id: {contract_id_from_event} (expected {self.contract_id_subscribed})")

    def _send_subscriptions(self):
        """
        Sends market data subscriptions to the Market Hub.
        - Quotes, Trades, and Depth are explicitly subscribed if enabled, using
        method names aligned with the provided JavaScript example for Market Hub.
        - Each subscription now uses a manually generated unique invocationId.
        """
        if not self.connection or self.connection_status != StreamConnectionState.CONNECTED:
            self.logger.warning(
                f"DataStream ({self.contract_id_subscribed}): Cannot send subscriptions, "
                f"not in CONNECTED state (current: {self.connection_status.name})."
            )
            return

        self.logger.info(
            f"DataStream ({self.contract_id_subscribed}): >>> ENTERING _send_subscriptions (Manual InvocationIDs) <<<"
        )
        all_previous_sends_ok = True 
        active_subscription_being_sent = "None"

        try:
            # --- QUOTES ---
            if self.auto_subscribe_quotes and self._callbacks["quote"]:
                active_subscription_being_sent = "SubscribeContractQuotes"
                quote_invocation_id = str(uuid.uuid4()) # <--- Generate unique ID
                self.logger.info(
                    f"DataStream ({self.contract_id_subscribed}): Preparing to send '{active_subscription_being_sent}' "
                    f"for contract '{self.contract_id_subscribed}' with InvocationID: {quote_invocation_id}"
                )
                time.sleep(0.2) 
                try:
                    # Pass the unique ID as the third argument
                    self.connection.send(active_subscription_being_sent, [self.contract_id_subscribed], quote_invocation_id)
                    self.logger.info(
                        f"DataStream ({self.contract_id_subscribed}): '{active_subscription_being_sent}' SENT (ID: {quote_invocation_id}). "
                        f"Connection state: {self.connection_status.name}"
                    )
                except Exception as e_send_quotes:
                    self.logger.error(
                        f"DataStream ({self.contract_id_subscribed}): Exception during self.connection.send for {active_subscription_being_sent} (ID: {quote_invocation_id}): {e_send_quotes}",
                        exc_info=True
                    )
                    all_previous_sends_ok = False
                
                if all_previous_sends_ok:
                    time.sleep(0.5) 
                    if self.connection_status != StreamConnectionState.CONNECTED:
                        self.logger.error(
                            f"DataStream ({self.contract_id_subscribed}): Connection state changed to {self.connection_status.name} "
                            f"after attempting {active_subscription_being_sent}. Halting further subscriptions."
                        )
                        all_previous_sends_ok = False
            elif self.auto_subscribe_quotes and not self._callbacks["quote"]:
                self.logger.warning(f"DataStream ({self.contract_id_subscribed}): auto_subscribe_quotes is True, but no quote callback is registered. Skipping quote subscription.")

            # --- TRADES (Using SubscribeContractTrades) ---
            if all_previous_sends_ok and self.auto_subscribe_trades and self._callbacks["trade"]:
                active_subscription_being_sent = "SubscribeContractTrades"
                trade_invocation_id = str(uuid.uuid4()) # <--- Generate unique ID
                self.logger.info(
                    f"DataStream ({self.contract_id_subscribed}): Preparing to send '{active_subscription_being_sent}' "
                    f"for contract '{self.contract_id_subscribed}' with InvocationID: {trade_invocation_id}"
                )
                time.sleep(0.2)
                try:
                    # Pass the unique ID as the third argument
                    self.connection.send(active_subscription_being_sent, [self.contract_id_subscribed], trade_invocation_id)
                    self.logger.info(
                        f"DataStream ({self.contract_id_subscribed}): '{active_subscription_being_sent}' SENT (ID: {trade_invocation_id}). "
                        f"Connection state: {self.connection_status.name}"
                    )
                except Exception as e_send_trades:
                    self.logger.error(
                        f"DataStream ({self.contract_id_subscribed}): Exception during self.connection.send for {active_subscription_being_sent} (ID: {trade_invocation_id}): {e_send_trades}",
                        exc_info=True
                    )
                    all_previous_sends_ok = False

                if all_previous_sends_ok:
                    time.sleep(0.5)
                    if self.connection_status != StreamConnectionState.CONNECTED:
                        self.logger.error(
                            f"DataStream ({self.contract_id_subscribed}): Connection state changed to {self.connection_status.name} "
                            f"after attempting {active_subscription_being_sent}. Halting further subscriptions."
                        )
                        all_previous_sends_ok = False
            elif self.auto_subscribe_trades and not self._callbacks["trade"]:
                self.logger.warning(f"DataStream ({self.contract_id_subscribed}): auto_subscribe_trades is True, but no trade callback is registered. Skipping trade subscription.")
        
            # --- DEPTH ---
            # (Depth subscription logic remains the same, but would also benefit from a unique invocationId if used)
            # For now, focusing on quotes and trades as per the problem. If depth becomes an issue, apply the same pattern.
            self.logger.info( 
                f"DataStream ({self.contract_id_subscribed}): >>> EVALUATING DEPTH SUBSCRIPTION BLOCK <<< "
                f"all_previous_sends_ok={all_previous_sends_ok}, "
                f"auto_sub_depth={self.auto_subscribe_depth}, "
                f"depth_callback_exists={self._callbacks['depth'] is not None}"
            )
            if all_previous_sends_ok and self.auto_subscribe_depth and self._callbacks["depth"]:
                active_subscription_being_sent = "SubscribeContractMarketDepth"
                depth_invocation_id = str(uuid.uuid4()) # <--- Generate unique ID
                self.logger.info(
                    f"DataStream ({self.contract_id_subscribed}): >>> INSIDE DEPTH BLOCK - Preparing to send '{active_subscription_being_sent}' "
                    f"for contract '{self.contract_id_subscribed}' with InvocationID: {depth_invocation_id} <<<"
                )
                time.sleep(0.2)
                try:
                    # Pass the unique ID as the third argument
                    self.connection.send(active_subscription_being_sent, [self.contract_id_subscribed], depth_invocation_id)
                    self.logger.info(
                        f"DataStream ({self.contract_id_subscribed}): >>> DEPTH SUB '{active_subscription_being_sent}' SENT (ID: {depth_invocation_id}). "
                        f"Current connection state BEFORE sleep: {self.connection_status.name} <<<"
                    )
                except Exception as e_send_depth:
                    self.logger.error(
                        f"DataStream ({self.contract_id_subscribed}): Exception during self.connection.send for {active_subscription_being_sent} (ID: {depth_invocation_id}): {e_send_depth}",
                        exc_info=True
                    )
                    all_previous_sends_ok = False
                
                if all_previous_sends_ok:
                    time.sleep(0.5)
                    self.logger.info(
                        f"DataStream ({self.contract_id_subscribed}): >>> AFTER 0.5s sleep for DEPTH SUB. "
                        f"Current connection state: {self.connection_status.name} <<<"
                    )
                    if self.connection_status != StreamConnectionState.CONNECTED:
                        self.logger.error(
                            f"DataStream ({self.contract_id_subscribed}): Connection state changed to {self.connection_status.name} "
                            f"after attempting {active_subscription_being_sent}."
                        )
                        all_previous_sends_ok = False 
            elif self.auto_subscribe_depth and not self._callbacks["depth"]:
                self.logger.warning(f"DataStream ({self.contract_id_subscribed}): auto_subscribe_depth is True, but no depth callback is registered. SKIPPING depth subscription.")
            elif not (all_previous_sends_ok and self.auto_subscribe_depth):
                self.logger.warning(
                    f"DataStream ({self.contract_id_subscribed}): >>> SKIPPING DEPTH SUBSCRIPTION SEND (one or more prior conditions failed) <<< "
                    f"Condition details: all_previous_sends_ok={all_previous_sends_ok}, "
                    f"auto_sub_depth={self.auto_subscribe_depth}, "
                    f"depth_callback_exists={self._callbacks['depth'] is not None}"
                )
            
            if all_previous_sends_ok and self.connection_status == StreamConnectionState.CONNECTED:
                self.logger.info(
                    f"DataStream ({self.contract_id_subscribed}): All enabled subscription messages dispatched "
                    f"and connection remains CONNECTED."
                )
            elif self.connection_status != StreamConnectionState.CONNECTED:
                self.logger.warning(
                    f"DataStream ({self.contract_id_subscribed}): Subscription process completed, but connection is "
                    f"no longer CONNECTED (Final State: {self.connection_status.name}). "
                    f"One or more subscriptions likely failed and caused a disconnect/error."
                )

        except Exception as e_outer: 
            self.logger.error(
                f"DataStream ({self.contract_id_subscribed}): Unexpected outer exception during _send_subscriptions "
                f"(current/last attempted send was '{active_subscription_being_sent}'): {e_outer}",
                exc_info=True
            )
            if self.connection_status == StreamConnectionState.CONNECTED:
                self._set_connection_state(StreamConnectionState.ERROR, f"Outer subscription send exception for {active_subscription_being_sent}: {type(e_outer).__name__}")
        finally:
            self.logger.info(
                f"DataStream ({self.contract_id_subscribed}): <<< EXITING _send_subscriptions >>> "
                f"(Final Connection State in method: {self.connection_status.name})"
            )

    def start(self) -> bool:
        with self._connection_lock:
            self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Start called. Current state: {self.connection_status.name}")
            self._is_manually_stopping = False 

            if self.connection_status == StreamConnectionState.CONNECTED:
                self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): Already CONNECTED.")
                return True
            if self.connection_status == StreamConnectionState.CONNECTING:
                self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): Already CONNECTING.")
                return True 
            
            if not self.connection or not self._hub_url_with_token: 
                self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Connection or Hub URL not ready, re-initializing/building.")
                try:
                    self._refresh_auth_token_and_url() 
                    self._build_connection()  
                except Exception as e_prep: 
                    self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Prep failed in start(): {e_prep}", exc_info=True)
                    self._set_connection_state(StreamConnectionState.ERROR, f"Prep failed in start: {type(e_prep).__name__}")
                    return False
            
            if not self.connection: 
                self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Connection object is None after build. Cannot start.")
                self._set_connection_state(StreamConnectionState.ERROR, "Connection object None post-build")
                return False
            
            self._set_connection_state(StreamConnectionState.CONNECTING, "Start method initiated connection attempt")
            try:
                self.connection.start() 
                self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): SignalR connection.start() called. Waiting for _on_open or _on_error.")
                return True 
            except Exception as e_conn_start: 
                self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Exception calling connection.start(): {e_conn_start}", exc_info=True)
                self._set_connection_state(StreamConnectionState.ERROR, f"Exception during connection.start(): {str(e_conn_start)[:50]}")
                return False

    def stop(self, reason_for_stop: Optional[str] = "User requested stop"):
        with self._connection_lock:
            self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Stop called. Reason: '{reason_for_stop}'. Current state: {self.connection_status.name}")
            self._is_manually_stopping = True 

            # Allow stop attempt even if in error or intermediate states, to ensure cleanup
            self._set_connection_state(StreamConnectionState.STOPPING, reason_for_stop)
            
            if self.connection:
                try: 
                    self.connection.stop() 
                except Exception as e_conn_stop:
                    self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Exception during connection.stop(): {e_conn_stop}", exc_info=True)
                    # _on_close or _on_error might still fire and set final state
                    # If stop itself errors, we might already be in a bad state.
                    # Forcibly set to error if not already handled by _on_close.
                    if self.connection_status != StreamConnectionState.DISCONNECTED:
                        self._set_connection_state(StreamConnectionState.ERROR, f"Exception during stop: {str(e_conn_stop)[:50]}")
            else:
                self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): No connection object to stop. Setting state to DISCONNECTED.")
                self._set_connection_state(StreamConnectionState.DISCONNECTED, "No connection object was present to stop")


    def update_token(self, new_token: str):
        with self._connection_lock:
            if not new_token:
                self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Attempted to update token with an empty new token. Ignoring.")
                return
            
            self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Token update requested.")

            # If token string hasn't changed AND stream is healthy, no need to restart.
            # APIClient handles its own internal re-validation if its token is nearing expiry.
            if new_token == self._current_token_for_url and \
               self.connection_status == StreamConnectionState.CONNECTED:
                self.logger.debug(f"DataStream (Contract: {self.contract_id_subscribed}): New token string is same as current, and stream connected. No DataStream restart needed.")
                # Optionally, could ask APIClient to re-validate its token if this call implies a general refresh cycle.
                # For now, assume APIClient manages its token lifecycle independently of this specific call if the string is identical.
                return

            self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Proceeding with connection restart for token update.")
            
            original_manual_stop_state = self._is_manually_stopping # Preserve user's intent if stop was already called
            
            # Stop existing connection if it's active or attempting to connect
            if self.connection_status not in [StreamConnectionState.NOT_INITIALIZED, 
                                              StreamConnectionState.DISCONNECTED, 
                                              StreamConnectionState.ERROR, # If already errored, stop won't help much but won't hurt
                                              StreamConnectionState.STOPPING]: # If already stopping, let it finish
                self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Stopping existing connection (state: {self.connection_status.name}) before token update.")
                self.stop(reason_for_stop="Token update initiated stop")
                
                # Wait for stop to complete (state to become DISCONNECTED or ERROR)
                # This is a pragmatic way to handle the async nature of stop.
                # A more robust way would use an event/condition variable signaled by _on_close.
                stop_timeout_sec = 5 
                wait_start_time = time.monotonic()
                while self.connection_status not in [StreamConnectionState.DISCONNECTED, StreamConnectionState.ERROR] and \
                      (time.monotonic() - wait_start_time) < stop_timeout_sec:
                    time.sleep(0.1)
                if self.connection_status not in [StreamConnectionState.DISCONNECTED, StreamConnectionState.ERROR]:
                    self.logger.warning(f"DataStream (Contract: {self.contract_id_subscribed}): Timeout waiting for connection to fully stop during token update. Current state: {self.connection_status.name}")

            self._is_manually_stopping = original_manual_stop_state # Restore original intent
            
            try:
                # Now use the new_token to refresh the URL and rebuild
                self._current_token_for_url = new_token 
                self._hub_url_with_token = f"{self._wss_hub_url_no_token}?access_token={self._current_token_for_url}"
                
                self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Rebuilding connection with new token in URL.")
                self._build_connection() 
                
                if not self._is_manually_stopping: # Only restart if not already in a manual stop process
                    self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Attempting to start connection after token update.")
                    if not self.start(): 
                        self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Failed to restart stream after token update.")
                else:
                    self.logger.info(f"DataStream (Contract: {self.contract_id_subscribed}): Manual stop was in progress; not auto-restarting after token update.")

            except Exception as e_rebuild:
                self.logger.error(f"DataStream (Contract: {self.contract_id_subscribed}): Error rebuilding/restarting connection after token update: {e_rebuild}", exc_info=True)
                self._set_connection_state(StreamConnectionState.ERROR, f"Token update rebuild/restart fail: {type(e_rebuild).__name__}")