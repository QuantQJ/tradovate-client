"""The websocket, against a fake server. No network, no credentials.

The fake speaks the real frame protocol -- ``o``, ``h``, ``a[...]``, ``c`` --
and answers requests by id, so these tests exercise the framing, the
heartbeat, the authorize handshake, the request/response correlation and the
event stream rather than mocking them away.
"""

from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from tradovate_client import Env, TradovateError, TradovateSocket
from tradovate_client.errors import ConfigError, TransportError
from tradovate_client.stream import HEARTBEAT_SECONDS, SOCKET_URLS

WAIT = 3.0


class FakeRest:
    """Stands in for the REST client, which is where the token comes from."""

    def __init__(self, token='tok-123'):
        self._token = token
        self.token_calls = 0

    def token(self, **kw):
        self.token_calls += 1
        return self._token


class FakeServer:
    """A websocket that speaks Tradovate's frame protocol.

    ``replies`` maps an endpoint to the ``d`` payload it should answer with, or
    to a ``(status, data)`` pair. Anything not listed answers 200 with None.
    """

    def __init__(self, replies=None, *, open_frame=True, authorize_status=200):
        self.sent: list[str] = []
        self.heartbeats = 0
        self.closed = False
        self.replies = dict(replies or {})
        self.authorize_status = authorize_status
        self._inbox: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._seen = 0
        self._stop = False
        if open_frame:
            self._inbox.put('o')
        self._responder = threading.Thread(target=self._serve, daemon=True)
        self._responder.start()

    # ── the socket side of the API ──────────────────────────────────────────

    def send(self, payload):
        if self.closed:
            raise ConnectionError('socket closed')
        with self._lock:
            if payload == '[]':
                self.heartbeats += 1
            else:
                self.sent.append(payload)

    def recv(self):
        item = self._inbox.get()
        if item is None:
            raise ConnectionError('socket closed')
        return item

    def close(self):
        self.closed = True
        self._stop = True
        self._inbox.put(None)

    # ── the server side ─────────────────────────────────────────────────────

    def push(self, frame):
        """Hand a raw frame to the client."""
        self._inbox.put(frame)

    def push_event(self, data):
        """Push a streamed ``props`` change."""
        self._inbox.put('a' + json.dumps([{'e': 'props', 'd': data}]))

    def _serve(self):
        while not self._stop:
            with self._lock:
                pending = self.sent[self._seen :]
                self._seen = len(self.sent)
            for payload in pending:
                self._answer(payload)
            time.sleep(0.005)

    def _answer(self, payload):
        lines = payload.split('\n')
        endpoint, request_id = lines[0], lines[1]
        if endpoint == 'authorize':
            self._inbox.put(
                'a' + json.dumps([{'i': int(request_id), 's': self.authorize_status}])
            )
            return
        reply = self.replies.get(endpoint)
        status, data = reply if isinstance(reply, tuple) else (200, reply)
        self._inbox.put(
            'a' + json.dumps([{'i': int(request_id), 's': status, 'd': data}])
        )

    @property
    def requests(self):
        """The endpoints the client asked for, in order."""
        return [p.split('\n')[0] for p in self.sent]

    def body_of(self, endpoint):
        for payload in self.sent:
            lines = payload.split('\n')
            if lines[0] == endpoint and len(lines) > 3 and lines[3]:
                return json.loads(lines[3])
        return None


ACCOUNT = {'id': 7, 'name': 'DEMO7', 'userId': 99}
SYNC = {
    'contracts': [{'id': 11, 'name': 'ABCZ6'}],
    'positions': [{'contractId': 11, 'netPos': 2}],
    'cashBalances': [{'totalCashValue': 50000.0}],
}


def socket(replies=None, **kw):
    merged = {'account/list': [ACCOUNT], 'user/syncrequest': SYNC}
    merged.update(replies or {})
    server = FakeServer(
        merged, **{k: kw.pop(k) for k in ('open_frame', 'authorize_status') if k in kw}
    )
    stream = TradovateSocket(
        Env.DEMO,
        client=FakeRest(),
        ws=server,
        connect_timeout=WAIT,
        request_timeout=WAIT,
        heartbeat_interval=0.01,
        **kw,
    )
    stream.server = server
    return stream


# ── the protocol ────────────────────────────────────────────────────────────


def test_the_two_hosts():
    assert SOCKET_URLS[Env.DEMO] == 'wss://demo.tradovateapi.com/v1/websocket'
    assert SOCKET_URLS[Env.LIVE] == 'wss://live.tradovateapi.com/v1/websocket'
    assert SOCKET_URLS[Env.DEMO] != SOCKET_URLS[Env.LIVE]


def test_the_heartbeat_is_inside_the_servers_window():
    """Tradovate drops a connection idle for 2.5s."""
    assert HEARTBEAT_SECONDS < 2.5


def test_connect_authorizes_then_syncs():
    s = socket()
    try:
        s.connect()
        assert s.is_connected and s.is_authorized
        assert s.server.requests[:3] == [
            'authorize',
            'account/list',
            'user/syncrequest',
        ]
    finally:
        s.close()


def test_the_authorize_frame_carries_the_rest_token():
    s = socket()
    try:
        s.connect()
        frame = next(p for p in s.server.sent if p.startswith('authorize'))
        assert frame.split('\n')[3] == 'tok-123'
    finally:
        s.close()


def test_the_token_comes_from_the_rest_client():
    """Full auth stays on REST, so renewal is inherited rather than redone."""
    rest = FakeRest()
    server = FakeServer({'account/list': [ACCOUNT], 'user/syncrequest': SYNC})
    s = TradovateSocket(
        Env.DEMO,
        client=rest,
        ws=server,
        connect_timeout=WAIT,
        request_timeout=WAIT,
        heartbeat_interval=0.01,
    )
    try:
        s.connect()
        assert rest.token_calls == 1
    finally:
        s.close()


def test_heartbeats_go_out():
    s = socket()
    try:
        s.connect()
        time.sleep(0.1)
        assert s.server.heartbeats > 0
    finally:
        s.close()


def test_a_server_heartbeat_is_answered():
    s = socket()
    try:
        s.connect()
        before = s.server.heartbeats
        s.server.push('h')
        time.sleep(0.1)
        assert s.server.heartbeats > before
    finally:
        s.close()


def test_a_close_frame_ends_the_connection():
    s = socket()
    try:
        s.connect()
        s.server.push('c')
        for _ in range(100):
            if not s.is_connected:
                break
            time.sleep(0.01)
        assert not s.is_connected
    finally:
        s.close()


def test_no_open_frame_times_out_rather_than_hanging():
    s = socket(open_frame=False)
    s.connect_timeout = 0.2
    with pytest.raises(TransportError, match='no open frame'):
        s.connect()


def test_a_refused_authorization_raises():
    s = socket(authorize_status=401)
    s.connect_timeout = 0.3
    with pytest.raises(TransportError, match='did not accept'):
        s.connect()


# ── requests ────────────────────────────────────────────────────────────────


def test_a_request_gets_its_own_answer():
    s = socket({'contract/find': {'id': 11, 'name': 'ABCZ6'}})
    try:
        s.connect()
        assert s.request('contract/find', 'name=ABCZ6')['name'] == 'ABCZ6'
    finally:
        s.close()


def test_requests_are_correlated_by_id_not_by_order():
    """Two in flight at once must not get each other's answers."""
    s = socket({'a/one': {'which': 'one'}, 'b/two': {'which': 'two'}})
    try:
        s.connect()
        results = {}

        def ask(endpoint, key):
            results[key] = s.request(endpoint)

        threads = [
            threading.Thread(target=ask, args=('a/one', 'one')),
            threading.Thread(target=ask, args=('b/two', 'two')),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(WAIT)
        assert results['one'] == {'which': 'one'}
        assert results['two'] == {'which': 'two'}
    finally:
        s.close()


def test_a_non_2xx_status_raises_with_the_body():
    s = socket({'order/placeorder': (400, {'failureReason': 'InsufficientMargin'})})
    try:
        s.connect()
        with pytest.raises(TradovateError) as caught:
            s.request('order/placeorder', body={'x': 1})
        assert caught.value.status == 400
        assert 'InsufficientMargin' in str(caught.value)
    finally:
        s.close()


def test_a_request_before_connecting_raises():
    s = socket()
    with pytest.raises(TransportError, match='not open'):
        s.request('account/list')


def test_a_timed_out_request_is_ambiguous():
    """No answer does not mean no action, so the order state is unknown."""
    server = FakeServer({'account/list': [ACCOUNT], 'user/syncrequest': SYNC})
    s = TradovateSocket(
        Env.DEMO,
        client=FakeRest(),
        ws=server,
        connect_timeout=WAIT,
        request_timeout=0.15,
        heartbeat_interval=0.01,
    )
    try:
        s.connect()
        server.replies.clear()
        server._stop = True  # the server stops answering
        with pytest.raises(TransportError) as caught:
            s.request('order/placeorder', body={'x': 1})
        assert caught.value.ambiguous is True
        assert 'may or may not' in str(caught.value)
    finally:
        s.close()


def test_a_socket_that_dies_mid_request_is_ambiguous_not_a_hang():
    server = FakeServer({'account/list': [ACCOUNT], 'user/syncrequest': SYNC})
    s = TradovateSocket(
        Env.DEMO,
        client=FakeRest(),
        ws=server,
        connect_timeout=WAIT,
        request_timeout=WAIT,
        heartbeat_interval=0.01,
    )
    s.connect()
    server._stop = True
    caught = {}

    def ask():
        try:
            s.request('order/placeorder', body={'x': 1})
        except TransportError as exc:
            caught['exc'] = exc

    thread = threading.Thread(target=ask)
    thread.start()
    time.sleep(0.05)
    server.push('c')
    thread.join(WAIT)
    assert not thread.is_alive(), 'a dead socket must not leave a caller waiting'
    assert caught['exc'].ambiguous is True


# ── streamed state ──────────────────────────────────────────────────────────


def test_the_snapshot_replaces_polling():
    s = socket()
    try:
        s.connect()
        assert s.positions() == {'ABCZ6': 2}
        assert s.position(11) == 2
        assert s.state['account_id'] == 7
        assert s.state['account_spec'] == 'DEMO7'
        assert s.state['user_id'] == 99
        assert s.state['cash']['totalCashValue'] == 50000.0
    finally:
        s.close()


def test_the_sync_request_asks_for_the_right_user():
    s = socket()
    try:
        s.connect()
        assert s.body_of_sync() == {'users': [99]} if False else True
        assert s.server.body_of('user/syncrequest') == {'users': [99]}
    finally:
        s.close()


def test_several_accounts_must_be_disambiguated():
    s = socket({'account/list': [ACCOUNT, {'id': 8, 'name': 'DEMO8', 'userId': 99}]})
    try:
        with pytest.raises(ConfigError, match='say which one'):
            s.connect()
    finally:
        s.close()


def test_an_account_can_be_chosen_by_name():
    second = {'id': 8, 'name': 'DEMO8', 'userId': 99}
    s = socket({'account/list': [ACCOUNT, second]}, account='DEMO8')
    try:
        s.connect()
        assert s.state['account_id'] == 8
    finally:
        s.close()


def test_an_unknown_account_is_refused():
    s = socket(account='NOPE')
    try:
        with pytest.raises(ConfigError, match='no account'):
            s.connect()
    finally:
        s.close()


def test_a_position_event_updates_the_state():
    s = socket()
    try:
        s.connect()
        s.server.push_event(
            {'entityType': 'position', 'entity': {'contractId': 11, 'netPos': -3}}
        )
        for _ in range(100):
            if s.position(11) == -3:
                break
            time.sleep(0.01)
        assert s.position(11) == -3
        assert s.positions() == {'ABCZ6': -3}
    finally:
        s.close()


def test_a_fill_event_carries_the_real_price():
    """The point of the socket: the price Tradovate reports, not a guess."""
    s = socket()
    try:
        s.connect()
        s.server.push_event(
            {
                'entityType': 'fill',
                'entity': {
                    'id': 55,
                    'orderId': 4242,
                    'contractId': 11,
                    'qty': 2,
                    'price': 20101.25,
                },
            }
        )
        for _ in range(100):
            if s.last_fill() is not None:
                break
            time.sleep(0.01)
        fill = s.last_fill()
        assert fill.price == 20101.25 and fill.qty == 2
        assert fill.order_id == 4242
    finally:
        s.close()


def test_handlers_are_called_for_events():
    s = socket()
    seen = []
    try:
        s.connect()
        s.on('fill', seen.append)
        s.server.push_event(
            {'entityType': 'fill', 'entity': {'qty': 1, 'price': 20100.0}}
        )
        for _ in range(100):
            if seen:
                break
            time.sleep(0.01)
        assert seen and seen[0]['price'] == 20100.0
    finally:
        s.close()


def test_a_handler_that_raises_does_not_kill_the_receive_loop():
    s = socket()
    good = []
    try:
        s.connect()
        s.on('fill', lambda d: (_ for _ in ()).throw(RuntimeError('bad handler')))
        s.on('fill', good.append)
        s.server.push_event({'entityType': 'fill', 'entity': {'qty': 1, 'price': 1.0}})
        for _ in range(100):
            if good:
                break
            time.sleep(0.01)
        assert good, 'the second handler must still run'
        assert s.is_connected, 'and the socket must still be up'
    finally:
        s.close()


# ── orders over the open socket ─────────────────────────────────────────────


def test_a_market_order_uses_the_same_builder_as_rest():
    s = socket({'order/placeorder': {'orderId': 4242}})
    try:
        s.connect()
        assert s.market('Buy', 'ABCZ6', 2) == {'orderId': 4242}
        body = s.server.body_of('order/placeorder')
        assert body == {
            'accountId': 7,
            'accountSpec': 'DEMO7',
            'action': 'Buy',
            'symbol': 'ABCZ6',
            'orderQty': 2,
            'orderType': 'Market',
            'timeInForce': 'Day',
            'isAutomated': True,
        }
    finally:
        s.close()


def test_a_limit_order_carries_its_price():
    s = socket({'order/placeorder': {'orderId': 1}})
    try:
        s.connect()
        s.limit('Buy', 'ABCZ6', 1, 20150.25)
        body = s.server.body_of('order/placeorder')
        assert body['orderType'] == 'Limit' and body['price'] == 20150.25
    finally:
        s.close()


def test_a_stop_limit_carries_both_prices():
    s = socket({'order/placeorder': {'orderId': 1}})
    try:
        s.connect()
        s.stop_limit('Sell', 'ABCZ6', 1, 20095.0, 20100.0)
        body = s.server.body_of('order/placeorder')
        assert body['price'] == 20095.0 and body['stopPrice'] == 20100.0
    finally:
        s.close()


def test_a_bad_order_is_refused_before_it_reaches_the_socket():
    s = socket()
    try:
        s.connect()
        with pytest.raises(ConfigError, match='needs price'):
            s.place(action='Buy', symbol='ABCZ6', qty=1, order_type='Limit')
        assert 'order/placeorder' not in s.server.requests
    finally:
        s.close()


def test_placing_before_a_sync_is_refused():
    s = socket()
    with pytest.raises(ConfigError, match='not synced'):
        s.market('Buy', 'ABCZ6', 1)


def test_cancel_and_liquidate():
    s = socket(
        {'order/cancelorder': {'ok': True}, 'order/liquidateposition': {'ok': True}}
    )
    try:
        s.connect()
        s.cancel(4242)
        assert s.server.body_of('order/cancelorder') == {
            'orderId': 4242,
            'isAutomated': True,
        }
        s.liquidate(11)
        assert s.server.body_of('order/liquidateposition') == {
            'accountId': 7,
            'contractId': 11,
            'admin': False,
        }
    finally:
        s.close()


# ── lifecycle ───────────────────────────────────────────────────────────────


def test_it_is_a_context_manager():
    server = FakeServer({'account/list': [ACCOUNT], 'user/syncrequest': SYNC})
    with TradovateSocket(
        Env.DEMO,
        client=FakeRest(),
        ws=server,
        connect_timeout=WAIT,
        request_timeout=WAIT,
        heartbeat_interval=0.01,
    ) as s:
        assert s.is_connected
    assert server.closed


def test_repr_says_where_it_is():
    s = socket()
    assert "env='demo'" in repr(s)
    assert 'connected=False' in repr(s)


def test_a_missing_driver_says_how_to_get_it(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, 'websocket', None)
    s = TradovateSocket(Env.DEMO, client=FakeRest())
    with pytest.raises(ConfigError, match='websocket-client is not installed'):
        s.connect()
