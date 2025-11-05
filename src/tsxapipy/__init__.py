# tsxapipy/__init__.py

"""
tsxapipy - Python library for interacting with the TopStep API.

This package provides tools for authentication, data retrieval, and real-time streaming
of market data from the TopStep API.
"""

# Version information
__version__ = '0.1.0'

# Import key components for easier access
from .auth import authenticate
from .api.client import APIClient, MAX_BARS_PER_REQUEST
from .api.contract_utils import get_futures_contract_details
from .real_time import DataStream, UserHubStream, StreamConnectionState
from .pipeline import LiveCandleAggregator, DataManager
from .monitor import (
    ContractMonitor,
    ContractUniverseManager,
    ContractSummary,
    MonitorResult,
    TimeframeDefinition,
)
from .trading import ( # <--- IMPORT FROM .trading
    OrderPlacer,
    ORDER_STATUS_TO_STRING_MAP,
    ORDER_STATUS_FILLED,
    ORDER_STATUS_CANCELLED,
    ORDER_STATUS_REJECTED,
    ORDER_STATUS_WORKING
    # Add other ORDER_STATUS_* constants if needed by examples or public API
)

from .api import schemas as api_schemas
from .common.time_utils import UTC_TZ
from .config import (
    CONTRACT_ID as DEFAULT_CONFIG_CONTRACT_ID,
    ACCOUNT_ID_TO_WATCH_STR as DEFAULT_CONFIG_ACCOUNT_ID_TO_WATCH,
    TOKEN_EXPIRY_SAFETY_MARGIN_MINUTES
)

from .api.exceptions import (
    APIError,
    AuthenticationError,
    ConfigurationError,
    LibraryError,
    APIResponseParsingError,
    ContractNotFoundError,
    InvalidParameterError,
    OrderNotFoundError,
    OrderRejectedError,
    MarketClosedError
)

# Export key components
__all__ = [
    'authenticate',
    'APIClient',
    'MAX_BARS_PER_REQUEST', 
    'get_futures_contract_details',
    'DataStream',
    'UserHubStream',
    'StreamConnectionState',
    'LiveCandleAggregator',
    'DataManager',
    'ContractMonitor',
    'ContractUniverseManager',
    'ContractSummary',
    'MonitorResult',
    'TimeframeDefinition',
    'OrderPlacer',
    'api_schemas', 
    'UTC_TZ', 
    'DEFAULT_CONFIG_CONTRACT_ID',
    'DEFAULT_CONFIG_ACCOUNT_ID_TO_WATCH', 
    'TOKEN_EXPIRY_SAFETY_MARGIN_MINUTES',

    # Exported Exceptions
    'APIError',
    'AuthenticationError',
    'ConfigurationError',
    'LibraryError',
    'APIResponseParsingError',
    'ContractNotFoundError',
    'InvalidParameterError',
    'OrderNotFoundError',
    'OrderRejectedError',
    'MarketClosedError',

    # Exported Order Status Constants
    'ORDER_STATUS_TO_STRING_MAP',   # <--- ADD
    'ORDER_STATUS_FILLED',          # <--- ADD
    'ORDER_STATUS_CANCELLED',       # <--- ADD
    'ORDER_STATUS_REJECTED',        # <--- ADD
    'ORDER_STATUS_WORKING',         # <--- ADD
]