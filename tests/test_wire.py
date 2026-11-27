"""Reconnection and penalties on the socket. No network, no credentials."""

from __future__ import annotations

import threading

import pytest

from fakes import WAIT, Factory, FakeRest, FakeServer, until
from tradovate_client import CaptchaRequired, Env, TimePenalty, TradovateSocket
from tradovate_client._wire import MAX_PENALTY_RETRIES, RECONNECT_DELAYS
from tradovate_client.errors import ConfigError, TransportError

ACCOUNT = {'id': 7, 'name': 'DEMO7', 'userId': 99}


def sync(**extra):
    base = {
        'contracts': [{'id': 11, 'name': 'ABCZ6'}],
        'positions': [{'contractId': 11, 'netPos': 2}],
        'cashBalances': [{'totalCashValue': 50000.0}],
    }
    base.update(extra)
    return base


def server(snapshot=None, **replies):
    merged = {'account/list': [ACCOUNT], 'user/syncrequest': snapshot or sync()}
    merged.update({k.replace('__', '/'): v for k, v in replies.items()})
    return FakeServer(merged)


def socket(*servers, rest=None, **kw):
    factory = Factory(*servers)
    sleeps = []
    stream = TradovateSocket(
        Env.DEMO,
        client=rest or FakeRest(),
        ws_factory=factory,
        connect_timeout=WAIT,
        request_timeout=kw.pop('request_timeout', WAIT),
        heartbeat_interval=0.01,
        sleep=sleeps.append,
        **kw,
    )
    stream.factory = factory
    stream.sleeps = sleeps
    return stream


# ── reconnection ────────────────────────────────────────────────────────────


def test_a_dropped_socket_is_reopened_and_resynced():
    first, second = server(), server(sync(positions=[{'contractId': 11, 'netPos': 5}]))
    s = socket(first, second)
    try:
        s.connect()
        assert s.position(11) == 2
        first.drop()
        assert until(lambda: s.reconnects == 1)
        assert s.is_connected and s.is_authorized
        assert second.requests[:3] == ['authorize', 'account/list', 'user/syncrequest']
        assert s.position(11) == 5
    finally:
        s.close()


def test_the_reconnect_waits_first_and_uses_a_fresh_token():
    rest = FakeRest()
    first, second = server(), server()
    s = socket(first, second, rest=rest)
    try:
        s.connect()
        first.drop()
        assert until(lambda: s.reconnects == 1)
        assert s.sleeps[0] == RECONNECT_DELAYS[0]
        assert rest.token_calls == 2  # one per connection, from the REST client
    finally:
        s.close()


def test_the_caller_chooses_the_delays():
    first, second = server(), server()
    s = socket(first, second, reconnect_delays=(0.25,))
    try:
        s.connect()
        first.drop()
        assert until(lambda: s.reconnects == 1)
        assert s.sleeps == [0.25]
    finally:
        s.close()


def test_a_position_closed_while_down_does_not_survive_the_reconnect():
    first, second = server(), server(sync(positions=[]))
    s = socket(first, second)
    try:
        s.connect()
        assert s.positions() == {'ABCZ6': 2}
        first.drop()
        assert until(lambda: s.reconnects == 1)
        assert s.positions() == {}
    finally:
        s.close()


def test_a_fill_missed_while_down_is_delivered_after_the_reconnect():
    old = {'id': 1, 'orderId': 50, 'contractId': 11, 'qty': 1, 'price': 100.0}
    new = {'id': 2, 'orderId': 51, 'contractId': 11, 'qty': 1, 'price': 101.5}
    first, second = server(sync(fills=[old])), server(sync(fills=[old, new]))
    s = socket(first, second)
    seen = []
    s.on('fill', seen.append)
    try:
        s.connect()
        assert seen == []  # the first snapshot is history, not news
        first.drop()
        assert until(lambda: s.reconnects == 1)
        assert seen == [new]
        assert s.last_fill().price == 101.5
        assert [f.id for f in s.fills()] == [1, 2]
    finally:
        s.close()


def test_disconnect_and_reconnect_are_announced():
    first, second = server(), server()
    s = socket(first, second)
    events = []
    s.on('disconnect', lambda p: events.append(('down', p['reconnecting'])))
    s.on('reconnect', lambda p: events.append(('up', p['attempts'])))
    try:
        s.connect()
        first.drop()
        assert until(lambda: len(events) == 2)
        assert events == [('down', True), ('up', 1)]
    finally:
        s.close()


def test_closing_on_purpose_does_not_reconnect():
    first, second = server(), server()
    s = socket(first, second)
    s.connect()
    s.close()
    assert not until(lambda: s.factory.made > 1, timeout=0.2)
    assert s.reconnects == 0


def test_reconnect_can_be_switched_off():
    first, second = server(), server()
    s = socket(first, second, reconnect=False)
    try:
        s.connect()
        first.drop()
        assert until(lambda: not s.is_connected)
        assert not until(lambda: s.factory.made > 1, timeout=0.2)
    finally:
        s.close()


def test_a_failed_attempt_is_followed_by_another():
    first, second = server(), server()
    s = socket(first, second)
    failures = []
    s.on('error', failures.append)
    # The first reconnection attempt cannot open a socket; the second can.
    calls = {'n': 0}
    real = s.factory

    def flaky():
        calls['n'] += 1
        if calls['n'] == 2:
            raise ConnectionError('network is down')
        return real()

    s._ws_factory = flaky
    try:
        s.connect()
        first.drop()
        assert until(lambda: s.reconnects == 1)
        assert len(failures) == 1
        assert s.sleeps[:2] == list(RECONNECT_DELAYS[:2])
    finally:
        s.close()


def test_it_gives_up_after_max_reconnects():
    first = server()
    s = socket(first, max_reconnects=2)
    errors = []
    s.on('error', errors.append)
    try:
        s.connect()
        first.drop()  # the factory has nothing more to hand out
        assert until(lambda: any('gave up reconnecting' in str(e) for e in errors))
        assert s.factory.made == 1
        assert len(s.sleeps) == 2
    finally:
        s.close()


@pytest.mark.parametrize(
    'error', [CaptchaRequired('captcha'), ConfigError('no credentials')]
)
def test_an_error_no_retry_can_fix_stops_the_reconnect(error):
    """Retrying past a captcha extends the penalty, so it is not retried."""
    rest = FakeRest()
    first, second = server(), server()
    s = socket(first, second, rest=rest)
    errors = []
    s.on('error', errors.append)
    try:
        s.connect()
        rest.token_error = error
        first.drop()
        assert until(lambda: errors == [error])
        assert not until(lambda: s.factory.made > 1, timeout=0.2)
        assert len(s.sleeps) == 1
    finally:
        s.close()


def test_a_request_in_flight_when_the_socket_drops_is_never_resent():
    """An order sent twice is two orders."""
    hold = threading.Event()

    def never(_body):
        hold.wait(WAIT)
        return {'orderId': 1}

    first = server(order__placeorder=never)
    second = server()
    s = socket(first, second)
    try:
        s.connect()
        outcome = {}

        def place():
            try:
                s.market('Buy', 'ABCZ6', 1)
            except TransportError as exc:
                outcome['error'] = exc

        thread = threading.Thread(target=place)
        thread.start()
        assert until(lambda: 'order/placeorder' in first.requests)
        first.drop()
        thread.join(WAIT)
        hold.set()
        assert outcome['error'].ambiguous is True
        assert until(lambda: s.reconnects == 1)
        assert 'order/placeorder' not in second.requests
    finally:
        s.close()


def test_a_socket_handed_in_cannot_be_reopened_without_a_factory():
    only = server()
    s = TradovateSocket(
        Env.DEMO,
        client=FakeRest(),
        ws=only,
        connect_timeout=WAIT,
        request_timeout=WAIT,
        heartbeat_interval=0.01,
    )
    notices = []
    s.on('disconnect', notices.append)
    try:
        s.connect()
        only.drop()
        assert until(lambda: notices == [{'reconnecting': False}])
    finally:
        s.close()


# ── time penalties over the socket ──────────────────────────────────────────


def penalised(times, then, **penalty):
    """Answer with a penalty ``times`` times, then with ``then``."""
    seen = []

    def reply(body):
        seen.append(body)
        if len(seen) <= times:
            return dict({'p-ticket': 'T%d' % len(seen), 'p-time': 3}, **penalty)
        return then

    reply.seen = seen
    return reply


def test_a_penalty_is_waited_out_and_the_ticket_sent_back():
    reply = penalised(1, {'orderId': 9})
    only = server(order__placeorder=reply)
    s = socket(only)
    try:
        s.connect()
        assert s.market('Buy', 'ABCZ6', 1) == {'orderId': 9}
        assert s.sleeps == [3.0]
        first, second = reply.seen
        assert 'p-ticket' not in first
        assert second['p-ticket'] == 'T1'
        assert {k: v for k, v in second.items() if k != 'p-ticket'} == first
    finally:
        s.close()


def test_a_captcha_is_never_retried():
    reply = penalised(5, {'orderId': 9}, **{'p-captcha': True})
    only = server(order__placeorder=reply)
    s = socket(only)
    try:
        s.connect()
        with pytest.raises(CaptchaRequired):
            s.market('Buy', 'ABCZ6', 1)
        assert len(reply.seen) == 1
        assert s.sleeps == []
    finally:
        s.close()


def test_a_penalty_that_does_not_lift_raises_with_the_wait():
    reply = penalised(99, {'orderId': 9})
    only = server(order__placeorder=reply)
    s = socket(only)
    try:
        s.connect()
        with pytest.raises(TimePenalty) as caught:
            s.market('Buy', 'ABCZ6', 1)
        assert caught.value.seconds == 3.0
        assert len(reply.seen) == MAX_PENALTY_RETRIES + 1
    finally:
        s.close()


# ── handlers run off the reading thread ─────────────────────────────────────


def test_a_handler_can_place_an_order_from_a_fill():
    """Were handlers called on the thread that reads the socket, the order's
    reply could never be read and this would stall until it timed out."""
    only = server(order__placeorder={'orderId': 77})
    s = socket(only, request_timeout=1.0)
    placed = []

    def on_fill(fill):
        placed.append(s.limit('Sell', 'ABCZ6', fill['qty'], fill['price'] + 5))

    s.on('fill', on_fill)
    try:
        s.connect()
        only.push_event(
            {
                'entityType': 'fill',
                'entity': {'id': 9, 'orderId': 1, 'qty': 1, 'price': 100.0},
            }
        )
        assert until(lambda: placed == [{'orderId': 77}])
        sent = only.body_of('order/placeorder')
        assert (sent['action'], sent['price']) == ('Sell', 105.0)
    finally:
        s.close()


def test_a_slow_handler_does_not_hold_up_requests():
    only = server()
    s = socket(only)
    release = threading.Event()
    entered = threading.Event()

    def slow(_event):
        entered.set()
        release.wait(WAIT)

    s.on('props', slow)
    try:
        s.connect()
        only.push_event(
            {'entityType': 'position', 'entity': {'contractId': 11, 'netPos': 1}}
        )
        assert entered.wait(WAIT)
        # the handler is still running; a request is answered regardless
        assert s.request('account/list') == [ACCOUNT]
    finally:
        release.set()
        s.close()


def test_closing_retires_a_reconnect_that_was_still_waiting():
    """Close, then connect again: the old reconnect must not open a second socket."""
    gate = threading.Event()
    first, second, third = server(), server(), server()
    factory = Factory(first, second, third)
    s = TradovateSocket(
        Env.DEMO,
        client=FakeRest(),
        ws_factory=factory,
        connect_timeout=WAIT,
        request_timeout=WAIT,
        heartbeat_interval=0.01,
        sleep=lambda _seconds: gate.wait(WAIT),
    )
    try:
        s.connect()
        first.drop()
        assert until(lambda: s._reconnecting)  # waiting out its delay
        s.close()
        s.connect()  # a new session, on the second server
        assert factory.made == 2
        gate.set()  # the old reconnect wakes up now
        assert until(lambda: not s._reconnecting)
        assert not until(lambda: factory.made > 2, timeout=0.2)
        assert s.is_connected and s.reconnects == 0
    finally:
        gate.set()
        s.close()
