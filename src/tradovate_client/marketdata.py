"""Market data: quotes, depth, histograms, bars and ticks.

Tradovate serves market data from its own websocket host,
``wss://md.tradovateapi.com/v1/websocket``, which speaks the same frame
protocol as the user socket. This module is that socket.

    from tradovate_client import MarketDataSocket, TradovateClient, Env

    rest = TradovateClient(Env.DEMO, credential_path='/secure/key.json')
    with MarketDataSocket(Env.DEMO, client=rest) as md:
        md.subscribe_quote('ABCZ6', lambda q: print(q.bid, q.ask, q.last))
        bars = md.bars('ABCZ6', element_size=5, count=100)

Everything here is read from Tradovate's own published material:

* the endpoints, their cancel counterparts and the message shapes --
  ``tradovate/example-api-js``, ``tutorial/WebSockets`` (EX-08 through EX-11
  and ``TradovateSocket.js``);
* the paging of a large chart request --
  ``tradovate/example-api-faq``, ``docs/HowToHandleDataSizeLimits.md``.

Subscriptions outlive the connection: after a reconnect each one is sent
again, so a quote handler keeps being called without the caller doing
anything.

What you can receive depends on the market data your Tradovate login is
entitled to. A subscription Tradovate refuses raises; this library does not
work around an entitlement.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ._wire import HEARTBEAT_SECONDS, RECONNECT_DELAYS, Wire
from .enums import ChartType, ElementSizeUnit
from .errors import ConfigError, TradovateError

__all__ = [
    'MarketDataSocket',
    'MARKET_DATA_URL',
    'Quote',
    'DepthOfMarket',
    'Histogram',
    'Bar',
    'Tick',
    'Chart',
    'Subscription',
]

MARKET_DATA_URL = 'wss://md.tradovateapi.com/v1/websocket'

#: How many chart packets to hold for a chart that has not yet been told its
#: ids. Only the gap between a request and its reply has to be covered.
UNCLAIMED_PACKETS = 256


# ── models ──────────────────────────────────────────────────────────────────


@dataclass
class Quote:
    """One quote update for one contract.

    ``entries`` is Tradovate's own mapping -- ``Bid``, ``Offer``, ``Trade``,
    ``TotalTradeVolume``, ``HighPrice``, ``LowPrice``, ``OpeningPrice``,
    ``SettlementPrice``, ``OpenInterest`` -- each with a ``price``, a ``size``,
    or both. Any entry, and either field of any entry, may be absent, so every
    property here returns None rather than a zero that would look like a price.
    """

    contract_id: int
    timestamp: str = ''
    entries: dict = field(default_factory=dict, repr=False)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> Quote:
        return cls(
            contract_id=d.get('contractId'),
            timestamp=d.get('timestamp') or '',
            entries=d.get('entries') or {},
            raw=d,
        )

    def _entry(self, name, key) -> float | None:
        value = (self.entries.get(name) or {}).get(key)
        return None if value is None else float(value)

    @property
    def bid(self) -> float | None:
        return self._entry('Bid', 'price')

    @property
    def bid_size(self) -> float | None:
        return self._entry('Bid', 'size')

    @property
    def ask(self) -> float | None:
        """The offer price. Tradovate calls this entry ``Offer``."""
        return self._entry('Offer', 'price')

    @property
    def ask_size(self) -> float | None:
        return self._entry('Offer', 'size')

    @property
    def last(self) -> float | None:
        """The price of the last trade."""
        return self._entry('Trade', 'price')

    @property
    def last_size(self) -> float | None:
        return self._entry('Trade', 'size')

    @property
    def volume(self) -> float | None:
        return self._entry('TotalTradeVolume', 'size')

    @property
    def high(self) -> float | None:
        return self._entry('HighPrice', 'price')

    @property
    def low(self) -> float | None:
        return self._entry('LowPrice', 'price')

    @property
    def open(self) -> float | None:
        return self._entry('OpeningPrice', 'price')

    @property
    def settlement(self) -> float | None:
        return self._entry('SettlementPrice', 'price')

    @property
    def open_interest(self) -> float | None:
        return self._entry('OpenInterest', 'size')


@dataclass
class DepthOfMarket:
    """The book for one contract: ``(price, size)`` levels on each side."""

    contract_id: int
    timestamp: str = ''
    bids: list = field(default_factory=list)
    offers: list = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> DepthOfMarket:
        return cls(
            contract_id=d.get('contractId'),
            timestamp=d.get('timestamp') or '',
            bids=_levels(d.get('bids')),
            offers=_levels(d.get('offers')),
            raw=d,
        )


def _levels(rows) -> list:
    return [
        (row.get('price'), row.get('size'))
        for row in (rows or [])
        if isinstance(row, dict)
    ]


@dataclass
class Histogram:
    """Volume at price for one contract.

    ``items`` maps an offset from ``base`` -- as Tradovate sends it -- to a
    volume. ``refresh`` is Tradovate's own flag on the message.
    """

    contract_id: int
    timestamp: str = ''
    trade_date: dict = field(default_factory=dict)
    base: float | None = None
    items: dict = field(default_factory=dict)
    refresh: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> Histogram:
        return cls(
            contract_id=d.get('contractId'),
            timestamp=d.get('timestamp') or '',
            trade_date=d.get('tradeDate') or {},
            base=d.get('base'),
            items=d.get('items') or {},
            refresh=bool(d.get('refresh')),
            raw=d,
        )


@dataclass
class Bar:
    """One bar, with the fields Tradovate reports for it."""

    timestamp: str
    open: float
    high: float
    low: float
    close: float
    up_volume: float = 0.0
    down_volume: float = 0.0
    up_ticks: float = 0.0
    down_ticks: float = 0.0
    bid_volume: float = 0.0
    offer_volume: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> Bar:
        return cls(
            timestamp=d.get('timestamp') or '',
            open=d.get('open'),
            high=d.get('high'),
            low=d.get('low'),
            close=d.get('close'),
            up_volume=d.get('upVolume') or 0.0,
            down_volume=d.get('downVolume') or 0.0,
            up_ticks=d.get('upTicks') or 0.0,
            down_ticks=d.get('downTicks') or 0.0,
            bid_volume=d.get('bidVolume') or 0.0,
            offer_volume=d.get('offerVolume') or 0.0,
            raw=d,
        )

    @property
    def volume(self) -> float:
        """``upVolume`` plus ``downVolume``."""
        return self.up_volume + self.down_volume

    @property
    def time(self) -> datetime:
        return _parse_time(self.timestamp)


@dataclass
class Tick:
    """One tick, decoded from Tradovate's packed tick packet.

    A packet carries a base price and base timestamp, and each tick is an
    offset from them counted in contract tick sizes. The decoding is the one
    Tradovate documents: ``timestamp = bt + t`` and
    ``price = (bp + p) * ts``, with bid and ask the same way.
    """

    id: Any
    timestamp_ms: int
    price: float
    size: float
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def time(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp_ms / 1000.0, tz=timezone.utc)


def decode_ticks(packet) -> list[Tick]:
    """Turn one tick packet into ``Tick`` objects."""
    base_price = packet.get('bp') or 0
    base_time = packet.get('bt') or 0
    tick_size = packet.get('ts') or 0

    def price(offset):
        return None if offset is None else (base_price + offset) * tick_size

    out = []
    for raw in packet.get('tks') or []:
        if not isinstance(raw, dict):
            continue
        out.append(
            Tick(
                id=raw.get('id'),
                timestamp_ms=base_time + (raw.get('t') or 0),
                price=(base_price + (raw.get('p') or 0)) * tick_size,
                size=raw.get('s') or 0,
                bid=price(raw.get('b')),
                ask=price(raw.get('a')),
                bid_size=raw.get('bs'),
                ask_size=raw.get('as'),
            )
        )
    return out


def _parse_time(text) -> datetime:
    return datetime.fromisoformat(str(text).replace('Z', '+00:00'))


# ── subscriptions ───────────────────────────────────────────────────────────


class Subscription:
    """A handle on one live subscription. ``cancel()`` ends it."""

    def __init__(self, socket, kind, symbol, handler) -> None:
        self._socket = socket
        self.kind = kind
        self.symbol = symbol
        self.handler = handler
        self.contract_id: int | None = None
        self.active = True

    def cancel(self) -> None:
        if self.active:
            self.active = False
            self._socket._drop(self)

    def __repr__(self) -> str:
        return 'Subscription(%s %r, active=%s)' % (self.kind, self.symbol, self.active)


class Chart:
    """A live chart subscription: history first, then real-time updates.

    Bars are held by timestamp, so the bar that is still forming is replaced
    as it updates rather than appended again, and history sent a second time
    after a reconnect does not duplicate anything. Ticks are held by id for
    the same reason.

    ``history_loaded`` is set when Tradovate sends its end-of-history marker;
    everything after that is real time.
    """

    def __init__(self, socket, body, *, on_bar=None, on_tick=None, on_eoh=None) -> None:
        self._socket = socket
        self.body = body
        self.on_bar = on_bar
        self.on_tick = on_tick
        self.on_eoh = on_eoh
        self.realtime_id: Any = None
        self.historical_id: Any = None
        self.history_loaded = threading.Event()
        self.active = True
        self._bars: dict[str, Bar] = {}
        self._ticks: dict[Any, Tick] = {}

    @property
    def symbol(self) -> Any:
        return self.body.get('symbol')

    @property
    def ids(self) -> tuple:
        """The subscription ids this chart's packets arrive under."""
        return tuple(i for i in (self.realtime_id, self.historical_id) if i is not None)

    def bars(self) -> list[Bar]:
        """Every bar held, oldest first."""
        return [self._bars[key] for key in sorted(self._bars, key=_parse_time)]

    def ticks(self) -> list[Tick]:
        """Every tick held, oldest first."""
        return sorted(self._ticks.values(), key=lambda t: (t.timestamp_ms, t.id or 0))

    def wait_for_history(self, timeout=None) -> bool:
        return self.history_loaded.wait(timeout)

    def cancel(self) -> None:
        if self.active:
            self.active = False
            self._socket._drop_chart(self)

    def _feed(self, packet) -> None:
        if packet.get('eoh'):
            self.history_loaded.set()
            _call(self.on_eoh, self)
            return
        for raw in packet.get('bars') or []:
            if isinstance(raw, dict):
                bar = Bar.from_api(raw)
                self._bars[bar.timestamp] = bar
                _call(self.on_bar, bar)
        if packet.get('tks'):
            for tick in decode_ticks(packet):
                self._ticks[tick.id] = tick
                _call(self.on_tick, tick)

    def __repr__(self) -> str:
        return 'Chart(%r, bars=%d, ticks=%d, active=%s)' % (
            self.symbol,
            len(self._bars),
            len(self._ticks),
            self.active,
        )


def _call(handler, payload) -> None:
    if handler is None:
        return
    try:
        handler(payload)
    except Exception:
        # A handler that raises must not take the receive loop down.
        pass


#: For each kind of streamed subscription: the endpoint that starts it, the
#: one that stops it, the key its messages arrive under, and its model. From
#: ``TradovateSocket.js`` in Tradovate's tutorial.
_STREAMS: dict[str, tuple[str, str, str, Callable[[dict], Any]]] = {
    'quote': ('md/subscribequote', 'md/unsubscribequote', 'quotes', Quote.from_api),
    'dom': ('md/subscribedom', 'md/unsubscribedom', 'doms', DepthOfMarket.from_api),
    'histogram': (
        'md/subscribehistogram',
        'md/unsubscribehistogram',
        'histograms',
        Histogram.from_api,
    ),
}


class MarketDataSocket(Wire):
    """Tradovate's market data socket.

    Emits ``quote``, ``dom``, ``histogram`` and ``chart`` for every message of
    that kind, as well as calling the handler each subscription was given.

    Needs ``websocket-client``: ``pip install "tradovate-client[socket]"``.
    """

    def __init__(
        self,
        env,
        *,
        client=None,
        credentials=None,
        credential_path=None,
        token_dir=None,
        connect_timeout=12.0,
        request_timeout=8.0,
        heartbeat_interval=HEARTBEAT_SECONDS,
        ws=None,
        ws_factory=None,
        reconnect=True,
        reconnect_delays=RECONNECT_DELAYS,
        max_reconnects=None,
        **kw,
    ) -> None:
        super().__init__(
            env,
            client=client,
            credentials=credentials,
            credential_path=credential_path,
            token_dir=token_dir,
            connect_timeout=connect_timeout,
            request_timeout=request_timeout,
            heartbeat_interval=heartbeat_interval,
            ws=ws,
            ws_factory=ws_factory,
            reconnect=reconnect,
            reconnect_delays=reconnect_delays,
            max_reconnects=max_reconnects,
            **kw,
        )
        self._subs: list[Subscription] = []
        self._charts: list[Chart] = []
        self._contract_ids: dict[str, int] = {}
        self._latest: dict[tuple[str, Any], Any] = {}
        # Re-entrant, because a handler called while a packet is being routed
        # may cancel its own subscription.
        self._registry = threading.RLock()
        # Chart packets that arrived before their chart knew its ids.
        self._unclaimed: deque = deque(maxlen=UNCLAIMED_PACKETS)

    @property
    def _url(self) -> str:
        return MARKET_DATA_URL

    def _token(self) -> str:
        # Tradovate's authentication reply carries a separate market data
        # token, and their own examples authorize this socket with it. A REST
        # client that cannot supply one hands over the access token instead,
        # which is what their tutorial uses.
        source = getattr(self.rest, 'md_token', None)
        return source() if callable(source) else self.rest.token()

    def _after_authorize(self, resumed) -> None:
        if not resumed:
            return
        # The server forgot every subscription with the old connection.
        with self._registry:
            subs = [s for s in self._subs if s.active]
            charts = [c for c in self._charts if c.active]
        started: set = set()
        for sub in subs:
            key = (sub.kind, str(sub.symbol))
            if key not in started:
                started.add(key)
                self.request(_STREAMS[sub.kind][0], body={'symbol': sub.symbol})
        for chart in charts:
            self._start_chart(chart)

    # ── contracts ───────────────────────────────────────────────────────────

    def contract_id(self, symbol) -> int:
        """The contract id a symbol's messages will arrive under.

        Streamed quotes, depth and histograms name their contract by id, not
        by symbol, so a subscription has to know the id to recognise its own
        messages. An integer is taken to be the id already; a name is looked
        up once through the REST client and remembered.
        """
        if isinstance(symbol, int):
            return symbol
        key = str(symbol)
        if key not in self._contract_ids:
            self._contract_ids[key] = int(self.rest.find_contract(key).id)
        return self._contract_ids[key]

    # ── quotes, depth, histograms ───────────────────────────────────────────

    def _subscribe(self, kind, symbol, handler) -> Subscription:
        sub = Subscription(self, kind, symbol, handler)
        sub.contract_id = self.contract_id(symbol)
        with self._registry:
            already = any(
                s.active and s.kind == kind and s.contract_id == sub.contract_id
                for s in self._subs
            )
            self._subs.append(sub)
        if not already:
            try:
                self.request(_STREAMS[kind][0], body={'symbol': symbol})
            except BaseException:
                with self._registry:
                    self._subs.remove(sub)
                sub.active = False
                raise
        return sub

    def _drop(self, sub) -> None:
        with self._registry:
            if sub in self._subs:
                self._subs.remove(sub)
            still_wanted = any(
                s.active and s.kind == sub.kind and s.contract_id == sub.contract_id
                for s in self._subs
            )
        if not still_wanted and self.is_connected:
            self.request(_STREAMS[sub.kind][1], body={'symbol': sub.symbol})

    def subscribe_quote(self, symbol, handler=None) -> Subscription:
        """Stream quotes for one contract. ``handler`` gets a ``Quote``."""
        return self._subscribe('quote', symbol, handler)

    def subscribe_dom(self, symbol, handler=None) -> Subscription:
        """Stream depth of market. ``handler`` gets a ``DepthOfMarket``."""
        return self._subscribe('dom', symbol, handler)

    def subscribe_histogram(self, symbol, handler=None) -> Subscription:
        """Stream the volume histogram. ``handler`` gets a ``Histogram``."""
        return self._subscribe('histogram', symbol, handler)

    def quote(self, symbol) -> Quote | None:
        """The latest quote received for a subscribed contract, if any."""
        return self._latest.get(('quote', self.contract_id(symbol)))

    def dom(self, symbol) -> DepthOfMarket | None:
        """The latest book received for a subscribed contract, if any."""
        return self._latest.get(('dom', self.contract_id(symbol)))

    def histogram(self, symbol) -> Histogram | None:
        return self._latest.get(('histogram', self.contract_id(symbol)))

    # ── charts ──────────────────────────────────────────────────────────────

    def chart(
        self,
        symbol,
        *,
        underlying_type=ChartType.MINUTE_BAR,
        element_size=1,
        element_size_unit=ElementSizeUnit.UNDERLYING_UNITS,
        with_histogram=False,
        as_much_as_elements=None,
        closest_timestamp=None,
        as_far_as_timestamp=None,
        closest_tick_id=None,
        on_bar=None,
        on_tick=None,
        on_eoh=None,
    ) -> Chart:
        """Subscribe to a chart: history, then real-time updates.

        The four time-range arguments are Tradovate's own. Each is optional
        and at least one is required; this library picks none for you.
        ``underlying_type='Tick'`` gives ticks instead of bars.
        """
        time_range = {
            'closestTimestamp': _stamp(closest_timestamp),
            'closestTickId': closest_tick_id,
            'asFarAsTimestamp': _stamp(as_far_as_timestamp),
            'asMuchAsElements': as_much_as_elements,
        }
        time_range = {k: v for k, v in time_range.items() if v is not None}
        if not time_range:
            raise ConfigError(
                'say how much to load: give at least one of '
                'as_much_as_elements=, closest_timestamp=, '
                'as_far_as_timestamp=, closest_tick_id='
            )
        body = {
            'symbol': symbol,
            'chartDescription': {
                'underlyingType': ChartType(underlying_type).value,
                'elementSize': int(element_size),
                'elementSizeUnit': ElementSizeUnit(element_size_unit).value,
                'withHistogram': bool(with_histogram),
            },
            'timeRange': time_range,
        }
        chart = Chart(self, body, on_bar=on_bar, on_tick=on_tick, on_eoh=on_eoh)
        with self._registry:
            self._charts.append(chart)
        try:
            self._start_chart(chart)
        except BaseException:
            with self._registry:
                self._charts.remove(chart)
            chart.active = False
            raise
        return chart

    def _start_chart(self, chart) -> None:
        reply = self.request('md/getchart', body=chart.body)
        reply = reply if isinstance(reply, dict) else {}
        if reply.get('errorText'):
            raise TradovateError(
                'Tradovate refused the chart: %s' % reply['errorText'],
                endpoint='md/getchart',
                body=reply,
            )
        # The receive thread can read the first chart packets before this
        # thread has been handed the reply that says which ids they carry.
        # Those packets were set aside; claim them now, in order, under the
        # same lock the router takes so nothing slips between the two.
        with self._registry:
            chart.realtime_id = reply.get('realtimeId') or reply.get('subscriptionId')
            chart.historical_id = reply.get('historicalId')
            mine = [p for p in self._unclaimed if p.get('id') in chart.ids]
            for packet in mine:
                self._unclaimed.remove(packet)
                chart._feed(packet)

    def _drop_chart(self, chart) -> None:
        with self._registry:
            if chart in self._charts:
                self._charts.remove(chart)
        if chart.realtime_id is not None and self.is_connected:
            self.request('md/cancelchart', body={'subscriptionId': chart.realtime_id})

    def _collect(self, symbol, *, timeout, **kw) -> Chart:
        """Load a chart's history, cancel it, and hand it back."""
        chart = self.chart(symbol, **kw)
        try:
            if not chart.wait_for_history(timeout):
                raise TimeoutError(
                    'no end-of-history from Tradovate for %r within %ss'
                    % (symbol, timeout)
                )
        finally:
            chart.cancel()
        return chart

    def bars(
        self,
        symbol,
        *,
        count=None,
        element_size=1,
        underlying_type=ChartType.MINUTE_BAR,
        element_size_unit=ElementSizeUnit.UNDERLYING_UNITS,
        closest_timestamp=None,
        as_far_as_timestamp=None,
        timeout=30.0,
    ) -> list[Bar]:
        """Historical bars, oldest first, as one blocking call.

        ``count`` is Tradovate's ``asMuchAsElements``. Tradovate caps how much
        one request returns and the cap varies, so a long range may come back
        short; ``history()`` pages through that.
        """
        chart = self._collect(
            symbol,
            timeout=timeout,
            underlying_type=underlying_type,
            element_size=element_size,
            element_size_unit=element_size_unit,
            as_much_as_elements=count,
            closest_timestamp=closest_timestamp,
            as_far_as_timestamp=as_far_as_timestamp,
        )
        return chart.bars()

    def ticks(
        self,
        symbol,
        *,
        count=None,
        closest_timestamp=None,
        as_far_as_timestamp=None,
        closest_tick_id=None,
        timeout=30.0,
    ) -> list[Tick]:
        """Historical ticks, oldest first, as one blocking call."""
        chart = self._collect(
            symbol,
            timeout=timeout,
            underlying_type=ChartType.TICK,
            element_size=1,
            as_much_as_elements=count,
            closest_timestamp=closest_timestamp,
            as_far_as_timestamp=as_far_as_timestamp,
            closest_tick_id=closest_tick_id,
        )
        return chart.ticks()

    def history(
        self,
        symbol,
        *,
        since,
        until=None,
        element_size=1,
        underlying_type=ChartType.MINUTE_BAR,
        element_size_unit=ElementSizeUnit.UNDERLYING_UNITS,
        timeout=30.0,
    ) -> list[Bar]:
        """Every bar from ``since`` to ``until``, however many requests it takes.

        Tradovate limits how much one chart request returns, and says the
        limit is variable on purpose. This follows the procedure in their own
        FAQ: load a range, find the oldest bar that came back, and ask again
        for the range ending there, until the oldest bar reaches ``since`` or
        a request brings nothing older.

        ``until`` defaults to now. Returns the bars oldest first, each
        timestamp once.
        """
        target = _stamp(since)
        cursor = _stamp(until) if until is not None else _stamp(_now())
        held: dict[str, Bar] = {}
        while True:
            chart = self._collect(
                symbol,
                timeout=timeout,
                underlying_type=underlying_type,
                element_size=element_size,
                element_size_unit=element_size_unit,
                closest_timestamp=cursor,
                as_far_as_timestamp=target,
            )
            before = len(held)
            for bar in chart.bars():
                held[bar.timestamp] = bar
            if not held:
                break
            oldest = min(held, key=_parse_time)
            reached = _parse_time(oldest) <= _parse_time(target)
            if reached or len(held) == before or oldest == cursor:
                break
            cursor = oldest
        return [held[key] for key in sorted(held, key=_parse_time)]

    # ── the stream ──────────────────────────────────────────────────────────

    def _on_event(self, message) -> None:
        data = message.get('d')
        if not isinstance(data, dict):
            return
        if message.get('e') == 'chart' or 'charts' in data:
            for packet in data.get('charts') or []:
                if isinstance(packet, dict):
                    self._route_chart(packet)
        if message.get('e') not in (None, 'md'):
            return
        for kind, (_start, _stop, key, build) in _STREAMS.items():
            for raw in data.get(key) or []:
                if not isinstance(raw, dict):
                    continue
                item = build(raw)
                self._latest[(kind, item.contract_id)] = item
                with self._registry:
                    wanted = [
                        s
                        for s in self._subs
                        if s.active
                        and s.kind == kind
                        and s.contract_id == item.contract_id
                    ]
                for sub in wanted:
                    _call(sub.handler, item)
                self._emit(kind, item)

    def _route_chart(self, packet) -> None:
        packet_id = packet.get('id')
        with self._registry:
            charts = [c for c in self._charts if packet_id in c.ids]
            if not charts and packet_id is not None:
                self._unclaimed.append(packet)
            for chart in charts:
                chart._feed(packet)
        self._emit('chart', packet)

    def close(self) -> None:
        with self._registry:
            for sub in self._subs:
                sub.active = False
            for chart in self._charts:
                chart.active = False
            self._subs = []
            self._charts = []
        super().close()

    def __repr__(self) -> str:
        return 'MarketDataSocket(connected=%s, subscriptions=%d, charts=%d)' % (
            self.is_connected,
            len(self._subs),
            len(self._charts),
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value) -> Any:
    """An ISO-8601 timestamp for the wire, from a datetime or a string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        text = value.astimezone(timezone.utc).isoformat(timespec='milliseconds')
        return text.replace('+00:00', 'Z')
    return str(value)
