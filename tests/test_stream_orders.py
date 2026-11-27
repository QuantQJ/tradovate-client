"""Orders and order state over the user socket. No network, no credentials."""

from __future__ import annotations

import json
import threading

import pytest

from fakes import WAIT, FakeRest, FakeServer, until
from tradovate_client import (
    BRACKET_STRATEGY_TYPE_ID,
    TERMINAL_STATUSES,
    Action,
    Env,
    OrderRejected,
    OrderType,
    TradovateClient,
    TradovateSocket,
)
from tradovate_client.errors import ConfigError

ACCOUNT = {'id': 7, 'name': 'DEMO7', 'userId': 99}
SYNC = {
    'contracts': [{'id': 11, 'name': 'ABCZ6'}],
    'positions': [
        {'accountId': 7, 'contractId': 11, 'netPos': 2, 'netPrice': 100.25},
        {'accountId': 8, 'contractId': 12, 'netPos': 9},
    ],
    'cashBalances': [
        {'accountId': 8, 'totalCashValue': 1.0},
        {'accountId': 7, 'totalCashValue': 50000.0},
    ],
    'orders': [
        {
            'id': 501,
            'accountId': 7,
            'contractId': 11,
            'action': 'Sell',
            'ordStatus': 'Working',
        },
        {
            'id': 502,
            'accountId': 8,
            'contractId': 12,
            'action': 'Buy',
            'ordStatus': 'Working',
        },
        {
            'id': 503,
            'accountId': 7,
            'contractId': 11,
            'action': 'Buy',
            'ordStatus': 'Filled',
        },
    ],
    'orderVersions': [
        {
            'id': 9001,
            'orderId': 501,
            'orderQty': 2,
            'orderType': 'Limit',
            'price': 105.0,
            'timeInForce': 'GTC',
        },
    ],
    'fills': [{'id': 1, 'orderId': 503, 'contractId': 11, 'qty': 2, 'price': 100.25}],
}


def socket(replies=None, **kw):
    merged = {'account/list': [ACCOUNT], 'user/syncrequest': SYNC}
    merged.update(replies or {})
    server = FakeServer(merged)
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


# ── the account boundary ────────────────────────────────────────────────────


def test_another_accounts_entities_are_left_out():
    """The snapshot covers the whole login; this socket is bound to one account."""
    s = socket()
    try:
        s.connect()
        assert s.positions() == {'ABCZ6': 2}
        assert s.position(12) == 0
        assert s.state['cash']['totalCashValue'] == 50000.0
        assert [o.id for o in s.orders()] == [501, 503]
    finally:
        s.close()


def test_a_streamed_event_for_another_account_is_ignored():
    s = socket()
    seen = []
    s.on('position', seen.append)
    try:
        s.connect()
        s.server.push_event(
            {
                'entityType': 'position',
                'entity': {'accountId': 8, 'contractId': 11, 'netPos': 40},
            }
        )
        s.server.push_event(
            {
                'entityType': 'position',
                'entity': {'accountId': 7, 'contractId': 11, 'netPos': 3},
            }
        )
        assert until(lambda: s.position(11) == 3)
        assert len(seen) == 1
    finally:
        s.close()


# ── orders in the streamed state ────────────────────────────────────────────


def test_an_order_is_joined_to_its_latest_version():
    s = socket()
    try:
        s.connect()
        order = s.order(501)
        assert (order.id, order.status, order.action) == (501, 'Working', 'Sell')
        assert (order.qty, order.order_type, order.price) == (2.0, 'Limit', 105.0)
        assert order.time_in_force == 'GTC'
        assert s.order(999) is None
        assert [o.id for o in s.working_orders()] == [501]
    finally:
        s.close()


def test_order_events_update_the_state():
    s = socket()
    seen = []
    s.on('order', seen.append)
    try:
        s.connect()
        s.server.push_event(
            {
                'entityType': 'order',
                'eventType': 'Updated',
                'entity': {
                    'id': 501,
                    'accountId': 7,
                    'contractId': 11,
                    'action': 'Sell',
                    'ordStatus': 'Canceled',
                },
            }
        )
        assert until(lambda: s.order(501).status == 'Canceled')
        assert s.working_orders() == []
        assert len(seen) == 1
    finally:
        s.close()


def test_fills_for_one_order():
    s = socket()
    try:
        s.connect()
        assert [f.price for f in s.fills_for_order(503)] == [100.25]
        assert s.fills_for_order(501) == []
    finally:
        s.close()


def test_the_same_fill_twice_is_one_fill():
    s = socket()
    seen = []
    s.on('fill', seen.append)
    fill = {'id': 77, 'orderId': 501, 'contractId': 11, 'qty': 1, 'price': 105.0}
    try:
        s.connect()
        s.server.push_event(
            {'entityType': 'fill', 'eventType': 'Created', 'entity': fill}
        )
        s.server.push_event(
            {'entityType': 'fill', 'eventType': 'Updated', 'entity': fill}
        )
        s.server.push_event(
            {'entityType': 'position', 'entity': {'contractId': 11, 'netPos': 1}}
        )
        assert until(lambda: s.position(11) == 1)
        assert len(seen) == 1
        assert [f.id for f in s.fills()] == [1, 77]
    finally:
        s.close()


def test_the_terminal_statuses_are_tradovates_names():
    documented = {
        'Canceled', 'Completed', 'Expired', 'Filled', 'PendingCancel', 'PendingNew',
        'PendingReplace', 'Rejected', 'Suspended', 'Unknown', 'Working',
    }  # fmt: skip
    assert documented.issuperset(TERMINAL_STATUSES)
    assert not TERMINAL_STATUSES & {'Working', 'PendingNew', 'Suspended'}


def test_waiting_for_an_order_returns_when_it_finishes():
    s = socket()
    try:
        s.connect()
        result = {}
        thread = threading.Thread(
            target=lambda: result.update(order=s.wait_for_order(501, timeout=WAIT))
        )
        thread.start()
        s.server.push_event(
            {
                'entityType': 'order',
                'entity': {'id': 501, 'accountId': 7, 'ordStatus': 'Filled'},
            }
        )
        thread.join(WAIT)
        assert result['order'].status == 'Filled'
        assert result['order'].qty == 2.0  # still joined to its version
    finally:
        s.close()


def test_an_order_already_finished_returns_at_once():
    s = socket()
    try:
        s.connect()
        assert s.wait_for_order(503, timeout=0).status == 'Filled'
    finally:
        s.close()


def test_an_order_still_working_times_out():
    s = socket()
    try:
        s.connect()
        with pytest.raises(TimeoutError, match='501'):
            s.wait_for_order(501, timeout=0.05)
    finally:
        s.close()


# ── writes ──────────────────────────────────────────────────────────────────


def test_a_refused_order_raises_over_the_socket_too():
    """The socket answers 200 and puts the refusal in the payload, as REST does."""
    s = socket(
        {
            'order/placeorder': {
                'failureReason': 'MaxPosLimitReached',
                'failureText': 'too big',
            }
        }
    )
    try:
        s.connect()
        with pytest.raises(OrderRejected) as caught:
            s.market('Buy', 'ABCZ6', 1)
        assert caught.value.reason == 'MaxPosLimitReached'
        assert caught.value.text == 'too big'
    finally:
        s.close()


def test_an_accepted_order_returns_its_id():
    s = socket({'order/placeorder': {'failureReason': 'Success', 'orderId': 42}})
    try:
        s.connect()
        assert s.market('Buy', 'ABCZ6', 1)['orderId'] == 42
    finally:
        s.close()


class Recorder:
    def __init__(self):
        self.calls = []

    def request(self, method, path, *, token=None, body=None, params=None):
        self.calls.append((path, body))
        if path == '/auth/accesstokenrequest':
            return {'accessToken': 'tok', 'expirationTime': '2030-01-01T00:00:00Z'}
        if path == '/account/list':
            return [ACCOUNT]
        return {'orderId': 1}


def rest_client(tmp_path):
    from tradovate_client import Credentials

    client = TradovateClient(
        Env.DEMO,
        credentials=Credentials(
            name='u',
            password='p',
            appId='a',
            appVersion='1',
            deviceId='d',
            cid=1,
            sec='s',
        ),
        token_dir=tmp_path,
    )
    client._transport = Recorder()
    return client


def test_brackets_are_the_same_bytes_as_rest(tmp_path):
    rest = rest_client(tmp_path)
    rest.bracketed_market(Action.BUY, 'ABCZ6', 1, take_profit=110.0, stop_loss=95.0)
    rest_body = dict(rest._transport.calls)['/order/placeoso']
    s = socket()
    try:
        s.connect()
        s.bracketed_market('Buy', 'ABCZ6', 1, take_profit=110.0, stop_loss=95.0)
        sent = s.server.body_of('order/placeoso')
        stripped = json.loads(
            json.dumps({k: v for k, v in rest_body.items() if v is not None})
        )
        stripped['bracket1'] = {
            k: v for k, v in stripped['bracket1'].items() if v is not None
        }
        stripped['bracket2'] = {
            k: v for k, v in stripped['bracket2'].items() if v is not None
        }
        assert sent == stripped
        assert sent['bracket1'] == {
            'action': 'Sell', 'orderType': 'Limit', 'price': 110.0, 'timeInForce': 'Day'
        }  # fmt: skip
        assert sent['bracket2'] == {
            'action': 'Sell', 'orderType': 'Stop', 'stopPrice': 95.0, 'timeInForce': 'Day'
        }  # fmt: skip
    finally:
        s.close()


def test_a_bracket_needs_a_price_from_the_caller():
    s = socket()
    try:
        s.connect()
        with pytest.raises(ConfigError, match='take_profit'):
            s.bracketed_market('Buy', 'ABCZ6', 1)
        assert 'order/placeoso' not in s.server.requests
    finally:
        s.close()


def test_oco_over_the_socket():
    s = socket()
    try:
        s.connect()
        stop = s.bracket_leg(Action.SELL, OrderType.STOP, stop_price=95.0)
        s.place_oco(
            action='Sell',
            symbol='ABCZ6',
            qty=1,
            order_type='Limit',
            price=110.0,
            other=stop,
        )
        sent = s.server.body_of('order/placeoco')
        assert sent['price'] == 110.0
        assert sent['other']['stopPrice'] == 95.0
    finally:
        s.close()


def test_modify_resends_the_size_from_the_streamed_version():
    """modifyorder replaces the order, so a price move still carries the size."""
    s = socket()
    try:
        s.connect()
        s.modify(501, price=106.5)
        assert s.server.body_of('order/modifyorder') == {
            'orderId': 501,
            'orderQty': 2,
            'orderType': 'Limit',
            'price': 106.5,
            'timeInForce': 'GTC',
            'isAutomated': True,
        }
        assert 'orderVersion/deps' not in s.server.requests
    finally:
        s.close()


def test_modify_asks_for_the_version_when_it_is_not_held():
    s = socket(
        {
            'orderVersion/deps': [
                {'orderId': 777, 'orderQty': 4, 'orderType': 'Stop', 'stopPrice': 90.0}
            ]
        }
    )
    try:
        s.connect()
        s.modify(777, stop_price=91.0)
        assert s.server.query_of('orderVersion/deps') == 'masterid=777'
        sent = s.server.body_of('order/modifyorder')
        assert (sent['orderQty'], sent['orderType'], sent['stopPrice']) == (
            4,
            'Stop',
            91.0,
        )
    finally:
        s.close()


def test_a_refused_modify_raises():
    s = socket({'order/modifyorder': {'failureReason': 'TooLate'}})
    try:
        s.connect()
        with pytest.raises(OrderRejected):
            s.modify(501, price=106.5)
    finally:
        s.close()


def test_cancel_working_cancels_only_this_accounts_orders():
    s = socket()
    try:
        s.connect()
        assert s.cancel_working() == [501]
        assert [b['orderId'] for b in s.server.bodies_of('order/cancelorder')] == [501]
    finally:
        s.close()


def test_a_bracket_strategy_is_sent_the_way_tradovates_example_sends_it():
    s = socket(
        {
            'orderStrategy/startorderstrategy': {
                'orderStrategy': {'id': 5, 'status': 'ActiveStrategy'}
            }
        }
    )
    try:
        s.connect()
        s.bracket_strategy(
            'Buy',
            'ABCZ6',
            1,
            brackets=[
                {
                    'qty': 1,
                    'profit_target': 75,
                    'stop_loss': -33,
                    'trailing_stop': False,
                }
            ],
        )
        sent = s.server.body_of('orderStrategy/startorderstrategy')
        assert sent['orderStrategyTypeId'] == BRACKET_STRATEGY_TYPE_ID == 2
        assert (sent['accountId'], sent['accountSpec']) == (7, 'DEMO7')
        assert (sent['symbol'], sent['action']) == ('ABCZ6', 'Buy')
        assert json.loads(sent['params']) == {
            'entryVersion': {
                'orderId': 0,
                'orderQty': 1,
                'orderType': 'Market',
                'timeInForce': 'Day',
            },
            'brackets': [
                {'qty': 1, 'profitTarget': 75, 'stopLoss': -33, 'trailingStop': False}
            ],
        }
    finally:
        s.close()


def test_a_refused_strategy_raises():
    s = socket({'orderStrategy/startorderstrategy': {'errorText': 'not permitted'}})
    try:
        s.connect()
        with pytest.raises(OrderRejected, match='not permitted'):
            s.bracket_strategy(
                'Buy', 'ABCZ6', 1, brackets=[{'qty': 1, 'stop_loss': -5}]
            )
    finally:
        s.close()
