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
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import orders as _orders
from ._http import _drop_none
from .enums import Env, OrderType
from .errors import ConfigError, TradovateError, TransportError
from .models import Fill

__all__ = ['TradovateSocket', 'SOCKET_URLS', 'HEARTBEAT_SECONDS']

SOCKET_URLS = {
    Env.DEMO: 'wss://demo.tradovateapi.com/v1/websocket',
    Env.LIVE: 'wss://live.tradovateapi.com/v1/websocket',
}

#: Tradovate drops a connection that has not sent anything for 2.5 seconds, so
#: the keep-alive goes out comfortably inside that.
HEARTBEAT_SECONDS = 2.0

#: Frame types, as the protocol defines them.
FRAME_OPEN = 'o'
FRAME_HEARTBEAT = 'h'
FRAME_MESSAGES = 'a'
FRAME_CLOSE = 'c'


class TradovateSocket:
    """One open socket to Tradovate.

        from tradovate_client import TradovateClient, TradovateSocket, Env

        rest = TradovateClient(Env.DEMO, credential_path='/secure/key.json')
        with TradovateSocket(Env.DEMO, client=rest) as stream:
            print(stream.positions())
            stream.on('fill', lambda fill: print('filled at', fill['price']))
            stream.market('Buy', 'MNQZ6', 1)

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
        account=None,
    ) -> None:
        self.env = Env(env)
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.heartbeat_interval = heartbeat_interval
        self.account = account
        self._rest = client
        self._rest_kw = {
            'credentials': credentials,
            'credential_path': credential_path,
            'token_dir': token_dir,
        }
        self._ws = ws
        self._next = 0
        self._lock = threading.Lock()
        self._pending: dict[Any, dict] = {}
        self._opened = threading.Event()
        self._authorized = threading.Event()
        self._authorize_id: int | None = None
        self._stop = False
        self._threads: list[threading.Thread] = []
        self._handlers: dict[str, list] = {}
        self._last_fill: dict | None = None
        self.state: dict[str, Any] = {
            'account_id': None,
            'account_spec': None,
            'user_id': None,
            'positions': {},
            'contracts': {},
            'fills': [],
            'cash': {},
        }

    # ── the REST client, for the token ──────────────────────────────────────

    @property
    def rest(self) -> Any:
        """The REST client whose token this socket reuses.

        Full authentication is a REST call, and the renewal logic lives there,
        so the socket borrows it rather than reimplementing it.
        """
        if self._rest is None:
            from .client import TradovateClient

            kw = {k: v for k, v in self._rest_kw.items() if v is not None}
            self._rest = TradovateClient(self.env, **kw)
        return self._rest

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return bool(self._ws) and self._opened.is_set() and not self._stop

    @property
    def is_authorized(self) -> bool:
        return self._authorized.is_set()

    def connect(self) -> TradovateSocket:
        """Open, authorize, and take a first snapshot."""
        token = self.rest.token()
        if self._ws is None:
            try:
                import websocket
            except ImportError as exc:
                raise ConfigError(
                    'websocket-client is not installed; '
                    'pip install "tradovate-client[socket]"'
                ) from exc
            try:
                self._ws = websocket.create_connection(
                    SOCKET_URLS[self.env], timeout=self.connect_timeout
                )
            except Exception as exc:
                raise TransportError(
                    'could not open a socket to %s: %s' % (SOCKET_URLS[self.env], exc)
                ) from exc
        self._stop = False
        self._spawn(self._receive_loop, 'tradovate-recv')
        self._spawn(self._heartbeat_loop, 'tradovate-heartbeat')
        if not self._opened.wait(self.connect_timeout):
            self.close()
            raise TransportError(
                'no open frame from Tradovate within %ss' % self.connect_timeout,
                endpoint='websocket',
            )
        self._authorize(token)
        self.sync()
        return self

    def _spawn(self, target, name) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(thread)
        thread.start()

    def _authorize(self, token) -> None:
        with self._lock:
            self._next += 1
            request_id = self._next
            self._authorize_id = request_id
        self._send_raw('authorize\n%d\n\n%s' % (request_id, token))
        if not self._authorized.wait(self.connect_timeout):
            self.close()
            raise TransportError(
                'Tradovate did not accept the socket authorization within %ss'
                % self.connect_timeout,
                endpoint='authorize',
            )

    def close(self) -> None:
        self._stop = True
        self._opened.clear()
        self._authorized.clear()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        with self._lock:
            waiting = list(self._pending.values())
            self._pending.clear()
        for entry in waiting:
            entry['closed'] = True
            entry['event'].set()

    def __enter__(self) -> TradovateSocket:
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # ── frames ──────────────────────────────────────────────────────────────

    def _send_raw(self, payload) -> None:
        if self._ws is None:
            raise TransportError('socket is not open', endpoint='websocket')
        try:
            self._ws.send(payload)
        except Exception as exc:
            raise TransportError(
                'socket send failed: %s' % exc, endpoint='websocket'
            ) from exc

    def _heartbeat_loop(self) -> None:
        while not self._stop:
            try:
                if self._ws is not None:
                    self._ws.send('[]')
            except Exception:
                self._opened.clear()
                break
            time.sleep(self.heartbeat_interval)

    def _receive_loop(self) -> None:
        while not self._stop:
            try:
                raw = self._ws.recv()
            except Exception:
                self._opened.clear()
                break
            if not raw:
                continue
            kind, body = raw[0], raw[1:]
            if kind == FRAME_OPEN:
                self._opened.set()
            elif kind == FRAME_HEARTBEAT:
                try:
                    self._ws.send('[]')
                except Exception:
                    self._opened.clear()
                    break
            elif kind == FRAME_MESSAGES:
                try:
                    messages = json.loads(body)
                except ValueError:
                    messages = []
                for message in messages:
                    self._dispatch(message)
            elif kind == FRAME_CLOSE:
                self._opened.clear()
                break
        self._fail_waiters()

    def _fail_waiters(self) -> None:
        """Wake anything still waiting, so a dead socket is not a hang."""
        with self._lock:
            waiting = list(self._pending.values())
            self._pending.clear()
        for entry in waiting:
            entry['closed'] = True
            entry['event'].set()

    def _dispatch(self, message) -> None:
        if not isinstance(message, dict):
            return
        if 'i' in message:
            request_id = message.get('i')
            if request_id == self._authorize_id:
                if message.get('s') == 200:
                    self._authorized.set()
                else:
                    self._authorized.clear()
                return
            with self._lock:
                entry = self._pending.get(request_id)
            if entry is not None:
                entry['status'] = message.get('s')
                entry['data'] = message.get('d')
                entry['event'].set()
            return
        if message.get('e') == 'props':
            self._apply(message.get('d') or {})
            self._emit('props', message.get('d') or {})

    # ── requests ────────────────────────────────────────────────────────────

    def request(self, endpoint, query='', body=None, *, timeout=None) -> Any:
        """Send one request over the socket and wait for its answer.

        Raises on timeout or a non-2xx status. A request whose socket dies
        while it is in flight raises ``TransportError`` with
        ``ambiguous=True``: the server may have acted on it, so for an order
        the state has to be read rather than assumed.
        """
        if not self.is_connected:
            raise TransportError('socket is not open', endpoint=endpoint)
        timeout = self.request_timeout if timeout is None else timeout
        event = threading.Event()
        with self._lock:
            self._next += 1
            request_id = self._next
            entry: dict[str, Any] = {
                'event': event,
                'data': None,
                'status': None,
                'closed': False,
            }
            self._pending[request_id] = entry
        payload = '%s\n%d\n%s\n%s' % (
            endpoint,
            request_id,
            query or '',
            '' if body is None else json.dumps(_drop_none(body)),
        )
        try:
            self._send_raw(payload)
        except TransportError:
            with self._lock:
                self._pending.pop(request_id, None)
            raise
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TransportError(
                'no reply to %s within %ss -- the request may or may not have '
                'been acted on. Read the state before sending anything else.'
                % (endpoint, timeout),
                endpoint=endpoint,
                ambiguous=True,
            )
        with self._lock:
            self._pending.pop(request_id, None)
        if entry['closed']:
            raise TransportError(
                'the socket closed while %s was in flight -- it may or may not '
                'have been acted on. Read the state before sending anything '
                'else.' % endpoint,
                endpoint=endpoint,
                ambiguous=True,
            )
        status = entry['status']
        if status is not None and not 200 <= int(status) < 300:
            raise TradovateError(
                'Tradovate refused %s' % endpoint,
                endpoint=endpoint,
                status=status,
                body=entry['data'],
            )
        return entry['data']

    # ── streamed state ──────────────────────────────────────────────────────

    def sync(self) -> dict:
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
                self._load_snapshot(snapshot)
        return dict(self.state)

    def _load_snapshot(self, snapshot) -> None:
        for contract in snapshot.get('contracts') or []:
            if isinstance(contract, dict) and contract.get('id') is not None:
                self.state['contracts'][contract['id']] = contract.get('name')
        for position in snapshot.get('positions') or []:
            if isinstance(position, dict) and position.get('contractId') is not None:
                self.state['positions'][position['contractId']] = position.get(
                    'netPos', 0
                )
        for balance in snapshot.get('cashBalances') or []:
            if isinstance(balance, dict):
                self.state['cash'] = balance
        for fill in snapshot.get('fills') or []:
            if isinstance(fill, dict):
                self.state['fills'].append(fill)

    def _apply(self, event) -> None:
        """Fold one streamed change into the live state."""
        kind = event.get('entityType') or event.get('entity')
        data = event.get('entity')
        if not isinstance(data, dict):
            data = event
        if kind == 'position' or 'netPos' in data:
            contract_id = data.get('contractId')
            if contract_id is not None:
                self.state['positions'][contract_id] = data.get('netPos', 0)
                self._emit('position', data)
        if kind == 'fill' or ('price' in data and 'qty' in data):
            self.state['fills'].append(data)
            self._last_fill = data
            self._emit('fill', data)
        if kind == 'cashBalance':
            self.state['cash'] = data
            self._emit('cash', data)
        if kind == 'order':
            self._emit('order', data)

    def on(self, event_name, handler) -> TradovateSocket:
        """Subscribe to ``fill``, ``position``, ``order``, ``cash`` or ``props``.

        A handler that raises does not take the receive loop down with it.
        """
        self._handlers.setdefault(str(event_name), []).append(handler)
        return self

    def _emit(self, event_name, payload) -> None:
        for handler in self._handlers.get(event_name, ()):
            try:
                handler(payload)
            except Exception:
                pass

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

    # ── writes, over the socket that is already open ────────────────────────

    def place(self, *, action, symbol, qty, order_type, **kw) -> Any:
        """Place an order over the socket.

        The body is built by the same function the REST client uses and
        unset fields are stripped the same way, so a socket order and a REST
        order are byte-for-byte identical. Sending an explicit null where the
        caller simply did not set a field is a good way to get a confusing
        rejection.
        """
        if self.state['account_id'] is None:
            raise ConfigError('not synced; call connect() or sync() first')
        body = _orders.build_order(
            account_id=self.state['account_id'],
            account_spec=self.state['account_spec'],
            action=action,
            symbol=symbol,
            qty=qty,
            order_type=order_type,
            **kw,
        )
        return self.request('order/placeorder', body=body)

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

    def cancel(self, order_id, *, is_automated=True) -> Any:
        return self.request(
            'order/cancelorder',
            body={'orderId': int(order_id), 'isAutomated': bool(is_automated)},
        )

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
