"""Client behaviour, against a fake transport. No network, no credentials."""

import pytest

from tradovate_client import Action, Env, OrderType, TradovateClient
from tradovate_client.errors import ConfigError, NotFoundError

CREDS = __import__('tradovate_client').Credentials(
    name='u', password='p', appId='a', appVersion='1', deviceId='d', cid=1,
    sec='s')


class FakeTransport:
    """Records calls and replies from a canned table."""

    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def request(self, method, path, *, token=None, body=None, params=None):
        self.calls.append({'method': method, 'path': path, 'body': body,
                           'params': params, 'token': token})
        if path in self.replies:
            value = self.replies[path]
            return value(body) if callable(value) else value
        raise AssertionError('unexpected call to %s' % path)


def build(replies, **kw):
    client = TradovateClient(Env.DEMO, credentials=CREDS,
                             token_dir=kw.pop('token_dir', None), **kw)
    client._transport = FakeTransport(dict(
        {'/auth/accesstokenrequest': {'accessToken': 'tok',
                                      'expirationTime': '2030-01-01T00:00:00Z'}},
        **replies))
    return client


def test_env_is_required():
    with pytest.raises(TypeError):
        TradovateClient()


def test_demo_client_targets_the_demo_host():
    assert TradovateClient(Env.DEMO).env.rest_url.startswith('https://demo.')
    assert TradovateClient(Env.LIVE).env.rest_url.startswith('https://live.')


def test_single_account_is_used_without_being_named(tmp_path):
    client = build({'/account/list': [{'id': 7, 'name': 'DEMO7'}]},
                   token_dir=tmp_path)
    assert client.account().id == 7


def test_several_accounts_must_be_disambiguated(tmp_path):
    client = build({'/account/list': [{'id': 1, 'name': 'A'},
                                      {'id': 2, 'name': 'B'}]},
                   token_dir=tmp_path)
    with pytest.raises(ConfigError, match='say which one'):
        client.account()


def test_account_can_be_chosen_by_name_or_id(tmp_path):
    replies = {'/account/list': [{'id': 1, 'name': 'A'}, {'id': 2, 'name': 'B'}]}
    assert build(replies, token_dir=tmp_path).account('B').id == 2
    assert build(replies, token_dir=tmp_path).account(1).name == 'A'


def test_unknown_account_lists_the_real_ones(tmp_path):
    client = build({'/account/list': [{'id': 1, 'name': 'A'}]},
                   token_dir=tmp_path)
    with pytest.raises(NotFoundError, match='A'):
        client.account('Z')


def test_market_order_payload_reaches_the_endpoint(tmp_path):
    client = build({'/account/list': [{'id': 7, 'name': 'DEMO7'}],
                    '/order/placeorder': {'orderId': 99}},
                   token_dir=tmp_path)
    assert client.market(Action.BUY, 'MNQZ6', 1) == {'orderId': 99}
    sent = [c for c in client._transport.calls
            if c['path'] == '/order/placeorder'][0]['body']
    assert sent['accountId'] == 7 and sent['accountSpec'] == 'DEMO7'
    assert sent['orderType'] == 'Market' and sent['isAutomated'] is True


def test_positions_of_other_accounts_are_filtered_out(tmp_path):
    client = build({'/account/list': [{'id': 7, 'name': 'DEMO7'}],
                    '/position/list': [
                        {'id': 1, 'accountId': 7, 'contractId': 11, 'netPos': 2},
                        {'id': 2, 'accountId': 8, 'contractId': 12, 'netPos': 5}]},
                   token_dir=tmp_path)
    held = client.positions()
    assert [p.contract_id for p in held] == [11]


def test_snapshot_names_only_held_contracts(tmp_path):
    client = build({
        '/account/list': [{'id': 7, 'name': 'DEMO7'}],
        '/position/list': [
            {'id': 1, 'accountId': 7, 'contractId': 11, 'netPos': 2,
             'netPrice': 20100.5},
            {'id': 2, 'accountId': 7, 'contractId': 12, 'netPos': 0}],
        '/contract/item': {'id': 11, 'name': 'MNQZ6'},
        '/cashBalance/getcashbalancesnapshot': {'totalCashValue': 50000.0,
                                                'openPnL': 25.0,
                                                'realizedPnL': -10.0},
    }, token_dir=tmp_path)
    snap = client.snapshot()
    assert set(snap.positions) == {'MNQZ6'}
    assert snap.positions['MNQZ6'].net_pos == 2
    assert snap.cash.total_cash == 50000.0 and snap.cash.open_pnl == 25.0
    assert not snap.is_flat
    assert not any(c['path'] == '/contract/list'
                   for c in client._transport.calls)


def test_bracketed_market_uses_the_opposite_side_for_its_children(tmp_path):
    client = build({'/account/list': [{'id': 7, 'name': 'DEMO7'}],
                    '/order/placeoso': {'orderId': 1}},
                   token_dir=tmp_path)
    client.bracketed_market(Action.BUY, 'MNQZ6', 1, take_profit=20200.0,
                            stop_loss=20050.0)
    body = [c for c in client._transport.calls
            if c['path'] == '/order/placeoso'][0]['body']
    assert body['action'] == 'Buy' and body['orderType'] == 'Market'
    assert body['bracket1'] == {'action': 'Sell', 'clOrdId': None,
                                'orderType': 'Limit', 'price': 20200.0,
                                'stopPrice': None, 'maxShow': None,
                                'pegDifference': None, 'timeInForce': 'Day',
                                'expireTime': None, 'text': None}
    assert body['bracket2']['orderType'] == 'Stop'
    assert body['bracket2']['stopPrice'] == 20050.0


def test_bracket_needs_at_least_one_child(tmp_path):
    client = build({'/account/list': [{'id': 7, 'name': 'DEMO7'}]},
                   token_dir=tmp_path)
    with pytest.raises(ConfigError, match='take_profit'):
        client.bracketed_market(Action.BUY, 'MNQZ6', 1)


def test_cancel_working_only_cancels_working_orders(tmp_path):
    client = build({'/account/list': [{'id': 7, 'name': 'DEMO7'}],
                    '/order/list': [{'id': 1, 'ordStatus': 'Working'},
                                    {'id': 2, 'ordStatus': 'Filled'},
                                    {'id': 3, 'ordStatus': 'PendingNew'}],
                    '/order/cancelorder': {'ok': True}},
                   token_dir=tmp_path)
    assert client.cancel_working() == [1, 3]


def test_flatten_all_cancels_then_liquidates(tmp_path):
    client = build({'/account/list': [{'id': 7, 'name': 'DEMO7'}],
                    '/order/list': [{'id': 1, 'ordStatus': 'Working'}],
                    '/order/cancelorder': {'ok': True},
                    '/position/list': [{'id': 1, 'accountId': 7,
                                        'contractId': 11, 'netPos': -3}],
                    '/order/liquidateposition': {'ok': True}},
                   token_dir=tmp_path)
    result = client.flatten_all()
    assert result == {'cancelled': [1], 'liquidated': [11]}
    order = [c['path'] for c in client._transport.calls]
    assert order.index('/order/cancelorder') < order.index('/order/liquidateposition')


def test_a_cached_token_is_renewed_not_re_requested(tmp_path):
    client = build({'/auth/renewaccesstoken': {
        'accessToken': 'renewed', 'expirationTime': '2030-01-01T00:00:00Z'}},
        token_dir=tmp_path)
    client._tokens.write({'accessToken': 'old'})     # assumed 3000s of life
    client._clock = lambda: client._tokens.read()[1] - 60.0   # inside the margin
    client._tokens._clock = client._clock
    assert client.token() == 'renewed'
    assert not any(c['path'] == '/auth/accesstokenrequest'
                   for c in client._transport.calls)


def test_a_fresh_token_makes_no_request_at_all(tmp_path):
    client = build({}, token_dir=tmp_path)
    client._tokens.write({'accessToken': 'good',
                          'expirationTime': '2030-01-01T00:00:00Z'})
    assert client.token() == 'good'
    assert client._transport.calls == []
