"""Enumerations, with values exactly as Tradovate's API defines them.

Every value here is copied from Tradovate's own published material:

* ``OrderType`` -- ``tradovate/example-api-trading-strategy``,
  ``utils/orderType.js``, and the generated model docs in
  ``tradovate/example-api-csharp-trading`` (``PlaceOSO.md``, ``PlaceOCO.md``,
  ``ModifyOrder.md``), which all agree.
* ``Action`` and ``TimeInForce`` -- the same generated model docs.

Nothing in this module is inferred. If Tradovate adds a value, add it here;
do not guess at one.
"""

from enum import Enum

__all__ = ['Env', 'Action', 'OrderType', 'TimeInForce']


class Env(str, Enum):
    """Which Tradovate environment to talk to.

    These are genuinely different servers with separate credentials and
    separate tokens, so a demo token cannot reach a live account. There is
    deliberately no default anywhere in this library -- every client must say
    which one it means.
    """

    DEMO = 'demo'
    LIVE = 'live'

    @property
    def rest_url(self) -> str:
        return 'https://%s.tradovateapi.com/v1' % self.value

    @property
    def socket_url(self) -> str:
        return 'wss://%s.tradovateapi.com/v1/websocket' % self.value

    @property
    def market_data_url(self) -> str:
        return 'wss://md.tradovateapi.com/v1/websocket'

    @property
    def is_live(self) -> bool:
        return self is Env.LIVE


class Action(str, Enum):
    BUY = 'Buy'
    SELL = 'Sell'

    @property
    def opposite(self) -> 'Action':
        return Action.SELL if self is Action.BUY else Action.BUY


class OrderType(str, Enum):
    LIMIT = 'Limit'
    MIT = 'MIT'
    MARKET = 'Market'
    QTS = 'QTS'
    STOP = 'Stop'
    STOP_LIMIT = 'StopLimit'
    TRAILING_STOP = 'TrailingStop'
    TRAILING_STOP_LIMIT = 'TrailingStopLimit'


class TimeInForce(str, Enum):
    DAY = 'Day'
    FOK = 'FOK'
    GTC = 'GTC'
    GTD = 'GTD'
    IOC = 'IOC'
