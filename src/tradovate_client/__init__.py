"""A Tradovate client for Python.

Covers authentication with token renewal, account and position reads, every
order type Tradovate's API defines, bracket orders, modify, cancel and
liquidate. The REST layer uses nothing but the standard library.

    from tradovate_client import TradovateClient, Env, Action

    client = TradovateClient(Env.DEMO)
    print(client.snapshot())
    client.limit(Action.BUY, 'MNQZ6', 1, price=20150.25)

Demo and live are different hosts with different credentials. ``env`` is a
required argument with no default, so a client built for demo has no code path
to the live account.
"""

from .auth import Credentials, TokenStore
from .client import TradovateClient
from .enums import Action, Env, OrderType, TimeInForce
from .errors import (AuthError, ConfigError, NotFoundError, RequestError,
                     ServerError, ThrottledError, TradovateError,
                     TransportError)
from .models import (Account, CashBalance, Contract, Fill, Order, Position,
                     PositionView, Snapshot)
from .orders import build_bracket_leg, build_order

__version__ = '0.1.0'

__all__ = [
    'TradovateClient', 'Credentials', 'TokenStore',
    'Env', 'Action', 'OrderType', 'TimeInForce',
    'TradovateError', 'ConfigError', 'TransportError', 'AuthError',
    'ThrottledError', 'RequestError', 'ServerError', 'NotFoundError',
    'Account', 'CashBalance', 'Contract', 'Position', 'PositionView',
    'Fill', 'Order', 'Snapshot',
    'build_order', 'build_bracket_leg',
    '__version__',
]
