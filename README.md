<!-- GitAds-Verify: BZVWHB1BJNQ48CSO49SROZ5MN7DOUECO -->

# tsxapipy: TopStep (ProjectX) API Python Library

**Version:** 0.5.0 (Pydantic Integration & Stream Stability Refactor)

## Overview

`tsxapipy` is a Python library designed for interacting with the TopStep (also referred to as ProjectX in some API documentation contexts) trading API. This suite provides a comprehensive toolkit for traders and developers looking to build automated trading strategies, perform data analysis, or manage their TopStep accounts programmatically.

Version 0.5.0 introduces a significant refactor, deeply integrating **Pydantic V2** for robust request payload construction and response parsing/validation across all core API interactions. This enhances data integrity, provides clear data contracts, and improves the developer experience.

## Core Features

*   **Pydantic-Driven API Client (`tsxapipy.api.client.APIClient`):**
    *   All public methods utilize Pydantic models (from `tsxapipy.api.schemas`) for constructing request payloads and for parsing, validating, and returning API responses as Pydantic model instances.
    *   Raises `APIResponseParsingError` on Pydantic validation failures.
*   **Robust Authentication & Session Management (`tsxapipy.auth`):**
    *   Supports API key-based authentication.
    *   `APIClient` manages HTTP sessions and includes automatic session token revalidation and re-authentication for long-running applications.
*   **Comprehensive HTTP REST API Coverage:**
    *   **Account Management:** Retrieve account details and lists (`List[api_schemas.Account]`).
    *   **Contract Information:** Search for contracts by text or ID (`List[api_schemas.Contract]`).
    *   **Historical Data Retrieval:** Fetch historical bar data (`api_schemas.HistoricalBarsResponse` containing `List[api_schemas.BarData]`).
    *   **Full Order Lifecycle:** Place, modify, cancel, and search orders using Pydantic models for requests and responses (e.g., `api_schemas.OrderPlacementResponse`, `api_schemas.OrderDetails`).
    *   **Position Management:** Search, close, and partially close positions (`List[api_schemas.Position]`, `api_schemas.PositionManagementResponse`).
    *   **Trade History:** Search and retrieve records of executed trades (`List[api_schemas.Trade]`).
*   **Real-Time Data Streaming (`tsxapipy.real_time` via SignalR):**
    *   `DataStream` for Market Hub (quotes, market trades, depth).
    *   `UserHubStream` for User Hub (account updates, orders, positions, user trades).
    *   **Initialization & Token Management:** Streams are initialized with an `APIClient` instance. The application is responsible for calling `stream_instance.update_token(new_api_token)` if the `APIClient`'s token is refreshed. Streams use direct WebSocket connections with the token embedded in the URL (`skip_negotiation: True`).
    *   **Clear State Management:** Uses a shared `StreamConnectionState` enum. The `on_state_change_callback` receives the string name of the state.
*   **Advanced Historical Data Management (`tsxapipy.historical.updater.HistoricalDataUpdater`):**
    *   Efficiently fetches, stores (in Parquet format), and updates historical bar data.
    *   Includes intelligent gap-filling logic and daily contract determination.
*   **Practical Trading Utilities (`tsxapipy.trading.order_handler.OrderPlacer`):**
    *   A higher-level class for simplified order submission, modification, and cancellation, using Pydantic models internally for API interactions.
*   **Data Processing Pipeline (`tsxapipy.pipeline`):**
    *   `LiveCandleAggregator` aggregates trades into candles, passing `pd.Series` to a callback.
    *   `DataManager` orchestrates `APIClient`, `DataStream`, and `LiveCandleAggregator` instances (one per timeframe) to provide processed `pd.DataFrame` candle data. Ideal for charting applications.
*   **Enhanced Error Handling & Resilience (`tsxapipy.api.exceptions`):**
    *   Defines a hierarchy of custom, specific exceptions for granular error identification.
    *   Includes `APIResponseParsingError` for Pydantic validation issues.
*   **Configuration Driven (`tsxapipy.config`):**
    *   Leverages `.env` files for managing API credentials, default parameters, and `LIVE`/`DEMO` environments.
    *   Issues warnings for missing global `API_KEY`/`USERNAME`.

## Project Structure (Simplified)


your_project_root/
├── src/
│ └── tsxapipy/ # The core Python library
│ ├── init.py
│ ├── api/ # APIClient, contract utils, Pydantic schemas, exceptions, error_mapper
│ ├── auth.py # Authentication logic
│ ├── common/ # Shared utilities (e.g., time_utils)
│ ├── config.py # Configuration loading, URL management
│ ├── historical/ # HistoricalDataUpdater, Parquet I/O, gap_detector
│ ├── pipeline/ # DataManager, LiveCandleAggregator
│ ├── real_time/ # DataStream, UserHubStream, StreamConnectionState
│ └── trading/ # Indicators, trade logic, OrderPlacer
├── examples/ # Runnable example scripts demonstrating library use (updated for v0.5.0)
├── scripts/ # CLI applications (e.g., fetch_historical_cli.py) (updated for v0.5.0)
├── tests/ # Unit and integration tests
├── .env.example # Example environment file
├── requirements.txt # Python dependencies
├── README.md # This file
└── ...

## Installation

1.  **Prerequisites:**
    *   Python 3.8+
    *   Git (for cloning)

2.  **Clone the Repository (if not installing from PyPI once available):**
    ```bash
    git clone <your-repository-url> tsxapipy_project
    cd tsxapipy_project
    ```

3.  **Create and Activate a Virtual Environment (Recommended):**
    ```bash
    python -m venv .venv
    # Windows PowerShell:
    .\.venv\Scripts\activate
    # macOS/Linux:
    source .venv/bin/activate
    ```

4.  **Install Dependencies:**
    From the project root (`tsxapipy_project/`):
    ```bash
    pip install -r requirements.txt
    ```
    If you are developing `tsxapipy` itself, install it in editable mode:
    ```bash
    pip install -e .
    ```

## Configuration

The library uses a `.env` file located in your project root (e.g., `tsxapipy_project/.env`) to manage sensitive credentials and configuration settings.

1.  **Copy the example environment file:**
    ```bash
    cp .env.example .env
    ```

2.  **Edit `.env`** and replace placeholder values:
    ```dotenv
    # == Environment: "LIVE" or "DEMO" (defaults to "DEMO" if not set) ==
    TRADING_ENVIRONMENT="DEMO"

    # == Core API Credentials (Required for API Key Auth) ==
    API_KEY="YOUR_TOPSTEP_API_KEY"
    USERNAME="YOUR_TOPSTEP_USERNAME"

    # == Optional: Default values for scripts & library components ==
    # CONTRACT_ID="CON.F.US.NQ.M25" # Example
    # ACCOUNT_ID_TO_WATCH=""       # Example (set your specific account ID)

    # == Optional: Token Management (defaults usually fine) ==
    # TOKEN_EXPIRY_SAFETY_MARGIN_MINUTES="30"
    # DEFAULT_TOKEN_LIFETIME_HOURS="23.5"
    ```
    `tsxapipy.config` will load these. `API_KEY` and `USERNAME` are essential; warnings will be issued if they are missing.

## Core Library Usage

### 1. Authentication & `APIClient`

```python
import logging
from typing import List, Optional
from tsxapipy import (
    authenticate, APIClient, ConfigurationError, AuthenticationError,
    APIError, api_schemas
)
from tsxapipy.api.exceptions import APIResponseParsingError

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s [%(levelname)s]: %(message)s')
logger = logging.getLogger("MyApp")

api_client: Optional[APIClient] = None
try:
    token_str, acquired_at_dt = authenticate() # Uses .env by default
    logger.info(f"Authenticated. Token acquired: {acquired_at_dt.isoformat()}")

    api_client = APIClient(initial_token=token_str, token_acquired_at=acquired_at_dt)
    logger.info("APIClient initialized.")

    # Example: Get active accounts (returns List[api_schemas.Account])
    accounts: List[api_schemas.Account] = api_client.get_accounts(only_active=True)
    for acc_model in accounts:
        logger.info(f"  Account ID: {acc_model.id}, Name: {acc_model.name}, Balance: {acc_model.balance}")

except ConfigurationError as e: logger.error(f"SETUP ERROR: {e}")
except AuthenticationError as e: logger.error(f"AUTHENTICATION FAILED: {e}")
except APIResponseParsingError as e: logger.error(f"RESPONSE PARSING ERROR: {e}. Raw: {e.raw_response_text}")
except APIError as e: logger.error(f"API Call Failed: {e}")
except Exception as e: logger.error(f"Unexpected error: {e}", exc_info=True)

2. Fetching Historical Data (HistoricalDataUpdater)
# Assuming api_client is initialized
from tsxapipy.historical import HistoricalDataUpdater

SYMBOL_ROOT = "NQ"
MAIN_PARQUET_FILE = f"data/{SYMBOL_ROOT.lower()}_1min_data.parquet" # Ensure 'data' dir exists

try:
    updater = HistoricalDataUpdater(
        api_client=api_client, symbol_root=SYMBOL_ROOT,
        main_parquet_file=MAIN_PARQUET_FILE, temp_file_suffix="_update_temp",
        api_bar_unit=2, api_bar_unit_number=1, # 1-minute bars
        fetch_days_if_new=90
    )
    logger.info(f"Updating historical data for {SYMBOL_ROOT}...")
    updater.update_data()
    logger.info(f"Historical data update complete for {SYMBOL_ROOT}.")
except Exception as e:
    logger.error(f"Error during historical update: {e}", exc_info=True)

3. Real-Time Market Data Streaming (DataStream)
# Assuming api_client is initialized
from tsxapipy import DataStream, StreamConnectionState

def my_market_quote_handler(quote_data: dict): logger.info(f"Quote: {quote_data}")
def my_market_state_handler(state_name: str): logger.info(f"Market Stream State: {state_name}")

MARKET_CONTRACT_ID = "CON.F.US.NQ.M25" # Example
market_stream: Optional[DataStream] = None
try:
    market_stream = DataStream(
        api_client=api_client, contract_id_to_subscribe=MARKET_CONTRACT_ID,
        on_quote_callback=my_market_quote_handler,
        on_state_change_callback=my_market_state_handler
    )
    if market_stream.start():
        logger.info(f"Market stream started for {MARKET_CONTRACT_ID}. Status: {market_stream.connection_status.name}")
        # Keep alive (e.g., time.sleep(60)) and periodically call:
        # market_stream.update_token(api_client.current_token)
        # For this example, we'll stop it after a short delay
        # time.sleep(10); market_stream.stop()
    else:
        logger.error(f"Failed to start market stream. Status: {market_stream.connection_status.name}")
except Exception as e: logger.error(f"Error with DataStream: {e}", exc_info=True)
# finally:
#     if market_stream: market_stream.stop()

4. Real-Time User Data Streaming (UserHubStream)
# Assuming api_client is initialized
from tsxapipy import UserHubStream

def my_user_order_handler(order_data: dict): logger.info(f"Order Update: {order_data}")
def my_user_state_handler(state_name: str): logger.info(f"User Stream State: {state_name}")

ACCOUNT_ID_TO_MONITOR = 12345 # Replace with your actual account ID from .env or config
user_stream: Optional[UserHubStream] = None
try:
    if ACCOUNT_ID_TO_MONITOR > 0 : # Ensure valid account ID
        user_stream = UserHubStream(
            api_client=api_client, account_id_to_watch=ACCOUNT_ID_TO_MONITOR,
            on_order_update=my_user_order_handler,
            on_state_change_callback=my_user_state_handler
        )
        if user_stream.start():
            logger.info(f"User stream started for account {ACCOUNT_ID_TO_MONITOR}. Status: {user_stream.connection_status.name}")
            # Keep alive and periodically call:
            # user_stream.update_token(api_client.current_token)
            # time.sleep(10); user_stream.stop()
        else:
            logger.error(f"Failed to start user stream. Status: {user_stream.connection_status.name}")
except Exception as e: logger.error(f"Error with UserHubStream: {e}", exc_info=True)
# finally:
#     if user_stream: user_stream.stop()

Important Note on Stream Token Refresh: For long-lived stream connections, your application must periodically get the latest token from api_client.current_token and call your_stream_instance.update_token(new_token).

5. Placing and Managing Orders (OrderPlacer)
# Assuming api_client and ACCOUNT_ID_TO_MONITOR are initialized
from tsxapipy.trading import OrderPlacer

try:
    order_placer = OrderPlacer(api_client=api_client, account_id=ACCOUNT_ID_TO_MONITOR)
    buy_order_id: Optional[int] = order_placer.place_market_order(side="BUY", size=1, contract_id=MARKET_CONTRACT_ID)
    if buy_order_id:
        logger.info(f"Market BUY order submitted, API Order ID: {buy_order_id}")
    else:
        logger.error("Failed to place market BUY order.")
except Exception as e: logger.error(f"OrderPlacer error: {e}", exc_info=True)

6. Data Processing Pipeline (DataManager)

tsxapipy.pipeline.DataManager is designed for applications (like charting) that need managed, multi-timeframe candle data.

from tsxapipy.pipeline import DataManager
import pandas as pd

# Assuming api_client is available
my_data_manager = DataManager()
try:
    if my_data_manager.initialize_components(contract_id=MARKET_CONTRACT_ID): # Uses internal APIClient
        my_data_manager.load_initial_history(num_candles_to_load=100) # Optional
        if my_data_manager.start_streaming():
            logger.info("DataManager streaming started.")
            # In an app, you'd periodically call get_chart_data in a callback
            # time.sleep(10) # Let it stream for a bit
            df_5min: pd.DataFrame = my_data_manager.get_chart_data(timeframe_seconds=300)
            if not df_5min.empty:
                logger.info(f"DataManager: Latest 5-min candle:\n{df_5min.iloc[-1] if not df_5min.empty else 'None'}")
            # Remember periodic my_data_manager.update_stream_token_if_needed()
        my_data_manager.stop_streaming()
except Exception as e: logger.error(f"DataManager error: {e}", exc_info=True)

Error Handling

The library defines custom exceptions in tsxapipy.api.exceptions. Key ones include:

ConfigurationError

AuthenticationError

APIError (and its subclasses like ContractNotFoundError, InvalidParameterError)

APIResponseParsingError (when API response fails Pydantic validation)

Wrap library calls in try...except blocks:

try:
    # ... tsxapipy calls ...
    pass
except APIResponseParsingError as e_parse: logger.error(f"Parsing Error: {e_parse}. Raw: {e_parse.raw_response_text}")
except APIError as e_api: logger.error(f"API Error: {e_api}")
except ConfigurationError as e_conf: logger.error(f"Config Error: {e_conf}")
# ... other specific exceptions or general Exception

Pydantic Schemas (tsxapipy.api.schemas)

All API request and response data structures are defined as Pydantic V2 models in tsxapipy.api.schemas. This provides:

Clear data contracts.

Automatic validation of incoming API data.

Type-hinted access to response data.
APIClient methods return instances of these Pydantic models (e.g., api_schemas.Account, api_schemas.HistoricalBarsResponse).

CLI Scripts & Examples

scripts/ directory: Contains ready-to-run CLI applications (e.g., fetch_historical_cli.py, trading_bot_cli.py).

examples/ directory: Contains focused code snippets demonstrating specific library features (e.g., 01_authenticate_and_get_accounts.py, 12_dedicated_market_data_stream.py).
All scripts and examples are updated to reflect v0.5.0 changes.

### Real-Time DOM Surface Dashboard Example

The `examples/dom_surface_dashboard/` folder contains a full-stack example that
streams live market data into an interactive browser dashboard.  It exposes a
FastAPI application (`app.py`) that:

* authenticates with the TopStep Market Hub using `tsxapipy`,
* relays quotes, trades, and depth updates via a websocket, and
* renders a Plotly-powered DOM surface (heatmap), rolling price chart, and
  volume bubble overlay in `index.html`.

To run the dashboard:

```bash
pip install ".[dashboard]"  # adds FastAPI & uvicorn for the dashboard example
uvicorn examples.dom_surface_dashboard.app:app --reload
```

By default the example subscribes to the contract declared in your `.env`
(`DEFAULT_CONFIG_CONTRACT_ID`). Override it with
`TSXAPIPY_DASHBOARD_CONTRACT_ID` if you want to point the dashboard at a
different instrument.

Development and Testing

Tests: Located in the tests/ directory. Use pytest to run.

Linters/Formatters: flake8 and black are recommended. mypy for type checking.

Key Known Points / TODOs (v0.5.0)

API_NUMERIC_ID_FIELD_HYPOTHESIS: The instrument_id field in api.schemas.Contract (API instrumentId) needs live API verification for its role as a numeric ID for history calls.

OrderBase.linked_order_id Alias: The Pydantic alias linkedOrderld needs verification against API documentation for correct casing.

Pydantic Model Refinements: Review Optional fields and Any type hints in api.schemas.py for more specificity.

Contributing

Contributions are welcome! Please see CONTRIBUTING.md (if available) or open an issue to discuss your ideas.

License

This project is licensed under the Apache License 2.0. See the LICENSE file.
