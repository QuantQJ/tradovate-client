"""The persistent websocket: real fills, streamed state, no per-order handshake.

The REST client opens a fresh TLS connection for every call and polls for
position. This holds one socket open instead, which buys three things:

* **Orders skip the handshake.** They go over a connection that is already up.
* **Fill prices are the real ones.** A fill arrives as an event carrying its
  price, rather than being inferred after the fact.
* **No polling.** ``user/syncrequest`` gives a full snapshot once and the
  server streams changes after that, so there is nothing to rate-limit.

Full authentication stays on REST. The socket reuses the token the REST client
already holds, which means it also inherits the renewal behaviour -- the thing
that keeps a long-running session from being throttled out.

The protocol, as Tradovate defines it
-------------------------------------
Frames are a single character followed by a body:

====  ===========================================================
``o``  the socket is open; authorize now
``h``  server heartbeat
``a``  a JSON array of messages
``c``  the server is closing
====  ===========================================================

A request is four newline-separated fields::

    <endpoint>\\n<id>\\n<query>\\n<body>

A response comes back inside an ``a`` frame as
``{"i": <id>, "s": <status>, "d": <data>}``, and a streamed change arrives as
``{"e": "props", "d": {...}}``.

The client must send ``[]`` at least every 2.5 seconds or the server drops the
connection. That is the detail every other Tradovate library misses.

When the connection does drop it is reopened and re-synced, and a fill that
happened while it was down is delivered as a ``fill`` event once the new
snapshot arrives. A request that was in flight is never repeated.
"""

from __future__ import annotations

import threading
from typing import Any

from . import orders as _orders
from ._wire import HEARTBEAT_SECONDS, RECONNECT_DELAYS, Wire
from .enums import Action, Env, OrderType
from .errors import ConfigError, OrderRejected, raise_for_result
from .models import Fill, Order

__all__ = ['TradovateSocket', 'SOCKET_URLS', 'HEARTBEAT_SECONDS', 'TERMINAL_STATUSES']

SOCKET_URLS = {
    Env.DEMO: 'wss://demo.tradovateapi.com/v1/websocket',
    Env.LIVE: 'wss://live.tradovateapi.com/v1/websocket',
}

#: Order statuses after which an order does nothing further. The names are
#: from Tradovate's ``Order`` model (``ordStatus``); which of them are final
#: is this library's reading of their plain meaning.
TERMINAL_STATUSES = frozenset(
    {'Filled', 'Canceled', 'Rejected', 'Expired', 'Completed'}
)


class TradovateSocket(Wire):
    """One open socket to Tradovate.

        from tradovate_client import TradovateClient, TradovateSocket, Env

        rest = TradovateClient(Env.DEMO, credential_path='/secure/key.json')
        with TradovateSocket(Env.DEMO, client=rest) as stream:
            print(stream.positions())
            stream.on('fill', lambda fill: print('filled at', fill['price']))
            stream.market('Buy', 'ABCZ6', 1)

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
        account=None,
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
        self.account = account
        self._last_fill: dict | None = None
        self._order_changed = threading.Condition()
        self.state: dict[str, Any] = self._empty_state()

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            'account_id': None,
            'account_spec': None,
            'user_id': None,
            'positions': {},
            'position_details': {},
            'contracts': {},
            'fills': [],
            'cash': {},
            'orders': {},
            'order_versions': {},
        }

    @property
    def _url(self) -> str:
        return SOCKET_URLS[self.env]

    def _after_authorize(self, resumed) -> None:
        self.sync(_resumed=resumed)

    # ── streamed state ──────────────────────────────────────────────────────

    def sync(self, *, _resumed=False) -> dict:
        """Take a full snapshot, then let the server stream the changes.

        One ``user/syncrequest`` replaces polling entirely.
        """
        accounts = self.request('account/list')
        chosen = None
        if isinstance(accounts, list) and accounts:
            if self.account is not None:
                chosen = next(
                    (
                        a
                        for a in accounts
                        if a.get('name') == str(self.account)
                        or a.get('id') == self.account
                    ),
                    None,
                )
                if chosen is None:
                    raise ConfigError(
                        'no account %r on this login; it has: %s'
                        % (
                            self.account,
                            ', '.join(str(a.get('name')) for a in accounts),
                        )
                    )
            elif len(accounts) > 1:
                raise ConfigError(
                    'this login has %d accounts (%s); say which one with '
                    'account='
                    % (len(accounts), ', '.join(str(a.get('name')) for a in accounts))
                )
            else:
                chosen = accounts[0]
        if chosen is not None:
            self.state['account_id'] = chosen.get('id')
            self.state['account_spec'] = chosen.get('name')
            self.state['user_id'] = chosen.get('userId')
        if self.state['user_id'] is not None:
            snapshot = self.request(
                'user/syncrequest', body={'users': [self.state['user_id']]}
            )
            if isinstance(snapshot, dict):
                self._load_snapshot(snapshot, resumed=_resumed)
        return dict(self.state)

    def _mine(self, entity) -> bool:
        """Whether an entity belongs to the account this socket is bound to.

        The snapshot covers every account on the login. An entity that names
        an account other than this one is not this socket's business; one that
        names no account is kept.
        """
        owner = entity.get('accountId')
        mine = self.state['account_id']
        return owner is None or mine is None or owner == mine

    def _load_snapshot(self, snapshot, *, resumed=False) -> None:
        known_fills = {
            f.get('id') for f in self.state['fills'] if f.get('id') is not None
        }
        # A snapshot is the whole truth as of now, so it replaces what was
        # held rather than being merged into it: a position closed while the
        # socket was down must not survive the reconnect.
        fresh = self._empty_state()
        for key in ('account_id', 'account_spec', 'user_id'):
            fresh[key] = self.state[key]
        for contract in snapshot.get('contracts') or []:
            if isinstance(contract, dict) and contract.get('id') is not None:
                fresh['contracts'][contract['id']] = contract.get('name')
        for position in snapshot.get('positions') or []:
            if (
                isinstance(position, dict)
                and position.get('contractId') is not None
                and self._mine(position)
            ):
                fresh['positions'][position['contractId']] = position.get('netPos', 0)
                fresh['position_details'][position['contractId']] = position
        for balance in snapshot.get('cashBalances') or []:
            if isinstance(balance, dict) and self._mine(balance):
                fresh['cash'] = balance
        for order in snapshot.get('orders') or []:
            if (
                isinstance(order, dict)
                and order.get('id') is not None
                and self._mine(order)
            ):
                fresh['orders'][order['id']] = order
        for version in snapshot.get('orderVersions') or []:
            if isinstance(version, dict) and version.get('orderId') is not None:
                fresh['order_versions'][version['orderId']] = version
        missed = []
        for fill in snapshot.get('fills') or []:
            if isinstance(fill, dict):
                fresh['fills'].append(fill)
                if resumed and fill.get('id') not in known_fills:
                    missed.append(fill)
        self.state = fresh
        with self._order_changed:
            self._order_changed.notify_all()
        # A fill that happened while the socket was down is in the new
        # snapshot but was never streamed. Deliver it now, so a handler that
        # acts on fills does not silently miss one.
        for fill in missed:
            self._last_fill = fill
            self._emit('fill', fill)

    def _on_event(self, message) -> None:
        if message.get('e') == 'props':
            self._apply(message.get('d') or {})
            self._emit('props', message.get('d') or {})

    def _apply(self, event) -> None:
        """Fold one streamed change into the live state."""
        kind = event.get('entityType') or event.get('entity')
        data = event.get('entity')
        if not isinstance(data, dict):
            data = event
        if kind == 'contract':
            if data.get('id') is not None:
                self.state['contracts'][data['id']] = data.get('name')
            return
        if kind == 'order':
            if data.get('id') is not None and self._mine(data):
                self.state['orders'][data['id']] = data
                with self._order_changed:
                    self._order_changed.notify_all()
            self._emit('order', data)
            return
        if kind == 'orderVersion':
            if data.get('orderId') is not None:
                self.state['order_versions'][data['orderId']] = data
            return
        if kind == 'cashBalance':
            if self._mine(data):
                self.state['cash'] = data
                self._emit('cash', data)
            return
        if kind == 'position' or (kind is None and 'netPos' in data):
            contract_id = data.get('contractId')
            if contract_id is not None and self._mine(data):
                self.state['positions'][contract_id] = data.get('netPos', 0)
                self.state['position_details'][contract_id] = data
                self._emit('position', data)
            return
        if kind == 'fill' or (kind is None and 'price' in data and 'qty' in data):
            fill_id = data.get('id')
            if fill_id is not None:
                for index, held in enumerate(self.state['fills']):
                    if held.get('id') == fill_id:
                        # The same fill again -- an update, not a new trade.
                        self.state['fills'][index] = data
                        return
            self.state['fills'].append(data)
            self._last_fill = data
            self._emit('fill', data)

    # ── reads off the streamed state, no network ────────────────────────────

    def position(self, contract_id) -> int:
        return int(self.state['positions'].get(contract_id, 0) or 0)

    def positions(self) -> dict:
        """Held positions by contract name, from the streamed state."""
        out = {}
        for contract_id, net in self.state['positions'].items():
            if not net:
                continue
            name = self.state['contracts'].get(contract_id, str(contract_id))
            out[name] = int(net)
        return out

    def last_fill(self) -> Fill | None:
        """The most recent fill, with the price Tradovate actually reported."""
        if self._last_fill is None:
            return None
        return Fill.from_api(self._last_fill)

    def fills(self) -> list[Fill]:
        return [Fill.from_api(f) for f in self.state['fills']]

    def fills_for_order(self, order_id) -> list[Fill]:
        """The streamed fills belonging to one order."""
        return [
            Fill.from_api(f)
            for f in self.state['fills']
            if f.get('orderId') == int(order_id)
        ]

    def order(self, order_id) -> Order | None:
        """One order from the streamed state, or None if it is not held.

        The order and its latest version arrive as separate entities; they are
        joined here so the size, type and prices are on the same object as the
        status.
        """
        held = self.state['orders'].get(int(order_id))
        if held is None:
            return None
        version = self.state['order_versions'].get(int(order_id)) or {}
        merged = {k: v for k, v in version.items() if k != 'id'}
        merged.update(held)
        return Order.from_api(merged)

    def orders(self) -> list[Order]:
        found = (self.order(order_id) for order_id in list(self.state['orders']))
        return [o for o in found if o is not None]

    def working_orders(self) -> list[Order]:
        return [o for o in self.orders() if o.is_working]

    def wait_for_order(self, order_id, *, timeout=None) -> Order:
        """Block until an order reaches a final status, and return it.

        Final means one of ``TERMINAL_STATUSES``. The answer comes from the
        stream, so nothing is polled. Raises ``TimeoutError`` if the order is
        still live when ``timeout`` seconds have passed -- which says nothing
        about the order except that it has not finished.
        """
        order_id = int(order_id)

        def done():
            held = self.state['orders'].get(order_id)
            return held is not None and held.get('ordStatus') in TERMINAL_STATUSES

        with self._order_changed:
            if not self._order_changed.wait_for(done, timeout):
                raise TimeoutError(
                    'order %d has not reached a final status within %ss'
                    % (order_id, timeout)
                )
        found = self.order(order_id)
        assert found is not None
        return found

    # ── writes, over the socket that is already open ────────────────────────

    def _order_body(self, *, action, symbol, qty, order_type, **kw) -> dict:
        if self.state['account_id'] is None:
            raise ConfigError('not synced; call connect() or sync() first')
        return _orders.build_order(
            account_id=self.state['account_id'],
            account_spec=self.state['account_spec'],
            action=action,
            symbol=symbol,
            qty=qty,
            order_type=order_type,
            **kw,
        )

    def _order_request(self, endpoint, body) -> Any:
        """Send an order and check the result Tradovate puts in the payload.

        The socket answers status 200 and reports the outcome in
        ``failureReason``, exactly as REST does, so a delivered request is not
        an accepted order.
        """
        return raise_for_result(self.request(endpoint, body=body), endpoint=endpoint)

    def place(self, *, action, symbol, qty, order_type, **kw) -> Any:
        """Place an order over the socket.

        The body is built by the same function the REST client uses and
        unset fields are stripped the same way, so a socket order and a REST
        order are byte-for-byte identical. Sending an explicit null where the
        caller simply did not set a field is a good way to get a confusing
        rejection.
        """
        body = self._order_body(
            action=action, symbol=symbol, qty=qty, order_type=order_type, **kw
        )
        return self._order_request('order/placeorder', body)

    def market(self, action, symbol, qty, **kw) -> Any:
        return self.place(
            action=action, symbol=symbol, qty=qty, order_type=OrderType.MARKET, **kw
        )

    def limit(self, action, symbol, qty, price, **kw) -> Any:
        return self.place(
            action=action,
            symbol=symbol,
            qty=qty,
            order_type=OrderType.LIMIT,
            price=price,
            **kw,
        )

    def stop(self, action, symbol, qty, stop_price, **kw) -> Any:
        return self.place(
            action=action,
            symbol=symbol,
            qty=qty,
            order_type=OrderType.STOP,
            stop_price=stop_price,
            **kw,
        )

    def stop_limit(self, action, symbol, qty, price, stop_price, **kw) -> Any:
        return self.place(
            action=action,
            symbol=symbol,
            qty=qty,
            order_type=OrderType.STOP_LIMIT,
            price=price,
            stop_price=stop_price,
            **kw,
        )

    @staticmethod
    def bracket_leg(action, order_type, **kw) -> dict:
        """Build one child leg for ``place_oso`` or ``place_oco``."""
        return _orders.build_bracket_leg(action=action, order_type=order_type, **kw)

    def place_oco(self, *, action, symbol, qty, order_type, other, **kw) -> Any:
        """One-cancels-other. ``other`` comes from ``bracket_leg``."""
        body = self._order_body(
            action=action, symbol=symbol, qty=qty, order_type=order_type, **kw
        )
        body['other'] = other
        return self._order_request('order/placeoco', body)

    def place_oso(
        self, *, action, symbol, qty, order_type, bracket1, bracket2=None, **kw
    ) -> Any:
        """One-sends-other: an entry with one or two child orders attached."""
        body = self._order_body(
            action=action, symbol=symbol, qty=qty, order_type=order_type, **kw
        )
        body['bracket1'] = bracket1
        if bracket2 is not None:
            body['bracket2'] = bracket2
        return self._order_request('order/placeoso', body)

    def bracketed_market(
        self, action, symbol, qty, *, take_profit=None, stop_loss=None, **kw
    ) -> Any:
        """A market entry with a profit target and a protective stop attached.

        Both child prices are the caller's own. This library never invents a
        stop distance or a target.
        """
        if take_profit is None and stop_loss is None:
            raise ConfigError('give take_profit=, stop_loss=, or both')
        exit_side = Action(action).opposite
        legs = []
        if take_profit is not None:
            legs.append(self.bracket_leg(exit_side, OrderType.LIMIT, price=take_profit))
        if stop_loss is not None:
            legs.append(
                self.bracket_leg(exit_side, OrderType.STOP, stop_price=stop_loss)
            )
        return self.place_oso(
            action=action,
            symbol=symbol,
            qty=qty,
            order_type=OrderType.MARKET,
            bracket1=legs[0],
            bracket2=legs[1] if len(legs) > 1 else None,
            **kw,
        )

    def start_order_strategy(
        self,
        action,
        symbol,
        *,
        order_strategy_type_id,
        params=None,
        uuid=None,
        custom_tag_50=None,
    ) -> Any:
        """Start a Tradovate order strategy over the socket.

        This is the transport Tradovate's own examples use for
        ``orderStrategy/startOrderStrategy``.
        """
        if self.state['account_id'] is None:
            raise ConfigError('not synced; call connect() or sync() first')
        body = _orders.build_order_strategy(
            account_id=self.state['account_id'],
            account_spec=self.state['account_spec'],
            action=action,
            symbol=symbol,
            order_strategy_type_id=order_strategy_type_id,
            params=params,
            uuid=uuid,
            custom_tag_50=custom_tag_50,
        )
        return _strategy_result(
            self.request('orderStrategy/startorderstrategy', body=body)
        )

    def bracket_strategy(self, action, symbol, qty, *, brackets, **entry) -> Any:
        """A bracket order strategy: an entry and the brackets that manage it.

        ``brackets`` is yours; see ``build_bracket_params`` for the shape.
        """
        return self.start_order_strategy(
            action,
            symbol,
            order_strategy_type_id=_orders.BRACKET_STRATEGY_TYPE_ID,
            params=_orders.build_bracket_params(qty=qty, brackets=brackets, **entry),
        )

    def modify(self, order_id, **kw) -> Any:
        """Change a working order.

        ``modifyorder`` replaces the order, so the size and type go back with
        every call. Whatever is not passed is read off the order's current
        version -- from the streamed state when it is held, otherwise with one
        request.
        """
        current = None
        if kw.get('qty') is None or kw.get('order_type') is None:
            current = self.state['order_versions'].get(int(order_id))
            if current is None:
                versions = self.request(
                    'orderVersion/deps', 'masterid=%d' % int(order_id)
                )
                if isinstance(versions, list) and versions:
                    current = versions[-1] or {}
        body = _orders.build_modify(order_id, current=current, **kw)
        return self._order_request('order/modifyorder', body)

    def cancel(self, order_id, *, is_automated=True) -> Any:
        return self.request(
            'order/cancelorder',
            body={'orderId': int(order_id), 'isAutomated': bool(is_automated)},
        )

    def cancel_working(self) -> list[int]:
        """Cancel this account's working orders. Returns the ids it cancelled."""
        cancelled = []
        for order in self.working_orders():
            self.cancel(order.id)
            cancelled.append(order.id)
        return cancelled

    def liquidate(self, contract_id, *, admin=False) -> Any:
        return self.request(
            'order/liquidateposition',
            body={
                'accountId': self.state['account_id'],
                'contractId': int(contract_id),
                'admin': bool(admin),
            },
        )

    def __repr__(self) -> str:
        return 'TradovateSocket(env=%r, connected=%s, authorized=%s)' % (
            self.env.value,
            self.is_connected,
            self.is_authorized,
        )


def _strategy_result(reply) -> Any:
    """Raise if an order-strategy reply carries ``errorText``."""
    if isinstance(reply, dict) and reply.get('errorText'):
        raise OrderRejected(
            'Tradovate refused the order strategy: %s' % reply['errorText'],
            reason=reply['errorText'],
            text=reply['errorText'],
            endpoint='orderStrategy/startorderstrategy',
            body=reply,
        )
    return reply
