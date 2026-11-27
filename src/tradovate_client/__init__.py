"""A Tradovate client for Python.

Covers authentication with token renewal, account and position reads, every
order type Tradovate's API defines, bracket orders and order strategies,
modify, cancel and liquidate, a user socket that reconnects and re-syncs, and
market data -- quotes, depth, bars and ticks. The REST layer uses nothing but
the standard library.

    from tradovate_client import TradovateClient, Env, Action

    client = TradovateClient(Env.DEMO, credential_path='~/my-key.json')
    print(client.snapshot())
    client.limit(Action.BUY, 'ABCZ6', 1, price=20150.25)

Demo and live are different hosts with different credentials. ``env`` is a
required argument with no default, so a client built for demo has no code path
to the live account.

There is no default credential path either. The client opens no file the
caller did not name.
"""

from .auth import Credentials, TokenStore
from .client import TradovateClient
from .enums import Action, ChartType, ElementSizeUnit, Env, OrderType, TimeInForce
from .errors import (
    AuthError,
    CaptchaRequired,
    ConfigError,
    NotFoundError,
    OrderRejected,
    RequestError,
    ServerError,
    ThrottledError,
    TimePenalty,
    TradovateError,
    TransportError,
)
from .marketdata import (
    Bar,
    Chart,
    DepthOfMarket,
    Histogram,
    MarketDataSocket,
    Quote,
    Subscription,
    Tick,
)
from .models import (
    Account,
    AutoLiq,
    CashBalance,
    Contract,
    ContractMaturity,
    Fill,
    FillFee,
    FillPair,
    MarginSnapshot,
    Order,
    OrderStrategy,
    Position,
    PositionView,
    Product,
    Snapshot,
)
from .orders import (
    BRACKET_STRATEGY_TYPE_ID,
    build_bracket_leg,
    build_bracket_params,
    build_modify,
    build_order,
    build_order_strategy,
)
from .stream import TERMINAL_STATUSES, TradovateSocket

__version__ = '0.2.0'

__all__ = [
    'TradovateClient',
    'TradovateSocket',
    'MarketDataSocket',
    'Credentials',
    'TokenStore',
    'Env',
    'Action',
    'OrderType',
    'TimeInForce',
    'ChartType',
    'ElementSizeUnit',
    'TradovateError',
    'ConfigError',
    'TransportError',
    'AuthError',
    'CaptchaRequired',
    'TimePenalty',
    'ThrottledError',
    'RequestError',
    'ServerError',
    'NotFoundError',
    'OrderRejected',
    'Account',
    'CashBalance',
    'Contract',
    'Position',
    'PositionView',
    'Fill',
    'FillFee',
    'FillPair',
    'Product',
    'Order',
    'Snapshot',
    'ContractMaturity',
    'MarginSnapshot',
    'AutoLiq',
    'OrderStrategy',
    'Quote',
    'DepthOfMarket',
    'Histogram',
    'Bar',
    'Tick',
    'Chart',
    'Subscription',
    'TERMINAL_STATUSES',
    'BRACKET_STRATEGY_TYPE_ID',
    'build_order',
    'build_bracket_leg',
    'build_modify',
    'build_bracket_params',
    'build_order_strategy',
    '__version__',
]
