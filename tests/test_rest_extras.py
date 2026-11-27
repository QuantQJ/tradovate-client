"""Penalties on any endpoint, the market data token, contracts, risk and
order strategies -- against a fake transport. No network, no credentials."""

from __future__ import annotations

import json

import pytest

from tradovate_client import (
    BRACKET_STRATEGY_TYPE_ID,
    Action,
    CaptchaRequired,
    Credentials,
    Env,
    OrderRejected,
    TimePenalty,
    TokenStore,
    TradovateClient,
    build_bracket_params,
)
from tradovate_client.auth import MAX_PENALTY_RETRIES, MAX_PENALTY_WAIT_SECONDS
from tradovate_client.errors import ConfigError, NotFoundError

CREDS = Credentials(
    name='u', password='p', appId='a', appVersion='1', deviceId='d', cid=1, sec='s'
)
AUTH = {
    'accessToken': 'tok',
    'mdAccessToken': 'md-tok',
    'expirationTime': '2030-01-01T00:00:00Z',
}
ACCOUNTS = [{'id': 7, 'name': 'DEMO7'}]


class FakeTransport:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def request(self, method, path, *, token=None, body=None, params=None):
        self.calls.append(
            {'method': method, 'path': path, 'body': body, 'params': params}
        )
        if path not in self.replies:
            raise AssertionError('unexpected call to %s' % path)
        value = self.replies[path]
        return value(body) if callable(value) else value

    def bodies(self, path):
        return [c['body'] for c in self.calls if c['path'] == path]


def build(replies, tmp_path, **kw):
    sleeps = []
    client = TradovateClient(
        Env.DEMO, credentials=CREDS, token_dir=tmp_path, sleep=sleeps.append, **kw
    )
    merged = {'/auth/accesstokenrequest': AUTH, '/account/list': ACCOUNTS}
    merged.update(replies)
    client._transport = FakeTransport(merged)
    client.sleeps = sleeps
    return client


def penalised(times, then, **penalty):
    seen = []

    def reply(body):
        seen.append(body)
        if len(seen) <= times:
            return dict({'p-ticket': 'T%d' % len(seen), 'p-time': 4}, **penalty)
        return then

    reply.seen = seen
    return reply


# ── a time penalty on any endpoint ──────────────────────────────────────────


def test_a_penalised_order_is_waited_out_and_sent_once_more_with_the_ticket(tmp_path):
    """A penalty reply means declined, not processed -- Tradovate's own FAQ
    resends the same body with the ticket added."""
    reply = penalised(1, {'orderId': 9})
    client = build({'/order/placeorder': reply}, tmp_path)
    assert client.market(Action.BUY, 'ABCZ6', 1) == {'orderId': 9}
    assert client.sleeps == [4.0]
    first, second = reply.seen
    assert 'p-ticket' not in first
    assert second == dict(first, **{'p-ticket': 'T1'})


def test_each_retry_carries_the_latest_ticket(tmp_path):
    reply = penalised(2, {'orderId': 9})
    client = build({'/order/placeorder': reply}, tmp_path)
    client.market(Action.BUY, 'ABCZ6', 1)
    assert [b.get('p-ticket') for b in reply.seen] == [None, 'T1', 'T2']


def test_a_captcha_on_an_order_is_never_retried(tmp_path):
    reply = penalised(9, {'orderId': 9}, **{'p-captcha': True})
    client = build({'/order/placeorder': reply}, tmp_path)
    with pytest.raises(CaptchaRequired):
        client.market(Action.BUY, 'ABCZ6', 1)
    assert len(reply.seen) == 1
    assert client.sleeps == []


def test_a_penalty_that_does_not_lift_raises(tmp_path):
    reply = penalised(99, {'orderId': 9})
    client = build({'/order/placeorder': reply}, tmp_path)
    with pytest.raises(TimePenalty) as caught:
        client.market(Action.BUY, 'ABCZ6', 1)
    assert caught.value.seconds == 4.0
    assert caught.value.attempts == MAX_PENALTY_RETRIES
    assert len(reply.seen) == MAX_PENALTY_RETRIES + 1


def test_one_wait_is_capped(tmp_path):
    reply = penalised(1, {'orderId': 9}, **{'p-time': 10**9})
    client = build({'/order/placeorder': reply}, tmp_path)
    client.market(Action.BUY, 'ABCZ6', 1)
    assert client.sleeps == [MAX_PENALTY_WAIT_SECONDS]


def test_a_penalised_read_reports_the_penalty_rather_than_guessing(tmp_path):
    """Tradovate documents the ticket going back in the body. A GET has none."""
    client = build({'/order/list': {'p-ticket': 'T', 'p-time': 7}}, tmp_path)
    with pytest.raises(TimePenalty) as caught:
        client.orders()
    assert (caught.value.seconds, caught.value.ticket) == (7.0, 'T')
    assert client.sleeps == []


# ── the market data token ───────────────────────────────────────────────────


def test_the_market_data_token_is_kept_beside_the_access_token(tmp_path):
    client = build({}, tmp_path)
    assert client.token() == 'tok'
    assert client.md_token() == 'md-tok'
    assert len(client._transport.bodies('/auth/accesstokenrequest')) == 1


def test_without_one_the_access_token_is_used(tmp_path):
    client = build(
        {
            '/auth/accesstokenrequest': {
                'accessToken': 'tok',
                'expirationTime': '2030-01-01T00:00:00Z',
            }
        },
        tmp_path,
    )
    assert client.md_token() == 'tok'


def test_a_renewal_without_a_market_data_token_keeps_the_one_held(tmp_path):
    now = [1000.0]
    store = TokenStore(Env.DEMO, directory=tmp_path, clock=lambda: now[0])
    store.write(
        {
            'accessToken': 'a1',
            'mdAccessToken': 'm1',
            'expirationTime': '2030-01-01T00:00:00Z',
        }
    )
    first = store.read_md()
    store.write({'accessToken': 'a2', 'expirationTime': '2031-01-01T00:00:00Z'})
    assert store.read()[0] == 'a2'
    assert store.read_md() == first  # same token, and its own expiry, not the new one
    assert first[0] == 'm1'


def test_an_expired_market_data_token_is_not_used(tmp_path):
    now = [0.0]
    client = build({}, tmp_path, clock=lambda: now[0])
    store = client._tokens
    store.write(
        {
            'accessToken': 'tok',
            'mdAccessToken': 'old',
            'expirationTime': '1970-01-01T01:00:00Z',
        }
    )
    store.write({'accessToken': 'tok2', 'expirationTime': '1970-01-03T00:00:00Z'})
    now[0] = 7200.0  # past the market data token's expiry, inside the access token's
    assert client.md_token() == 'tok2'


def test_the_token_file_is_still_private(tmp_path):
    client = build({}, tmp_path)
    client.token()
    path = client._tokens.path
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())['mdAccessToken'] == 'md-tok'


# ── balances ────────────────────────────────────────────────────────────────


def test_the_cash_balance_carries_margin_and_net_liq(tmp_path):
    client = build(
        {
            '/cashBalance/getcashbalancesnapshot': {
                'totalCashValue': 50000.0,
                'totalPnL': 120.0,
                'initialMargin': 1500.0,
                'maintenanceMargin': 1400.0,
                'netLiq': 50120.0,
                'openPnL': 20.0,
                'realizedPnL': 100.0,
                'weekRealizedPnL': 310.0,
            }
        },
        tmp_path,
    )
    cash = client.cash_balance()
    assert (cash.total_cash, cash.open_pnl, cash.realized_pnl) == (50000.0, 20.0, 100.0)
    assert (cash.net_liq, cash.total_pnl) == (50120.0, 120.0)
    assert (cash.initial_margin, cash.maintenance_margin) == (1500.0, 1400.0)
    assert cash.week_realized_pnl == 310.0


def test_a_margin_figure_tradovate_did_not_send_is_none(tmp_path):
    client = build(
        {'/cashBalance/getcashbalancesnapshot': {'totalCashValue': 50000.0}}, tmp_path
    )
    cash = client.cash_balance()
    assert cash.net_liq is None and cash.initial_margin is None


def test_margin_snapshots_and_risk_limits_are_read_back(tmp_path):
    client = build(
        {
            '/marginSnapshot/list': [
                {'id': 7, 'initialMargin': 1500.0, 'maintenanceMargin': 1400.0,
                 'autoLiqLevel': 0.0, 'totalUsedMargin': 1500.0, 'fullInitialMargin': 1650.0}
            ],
            '/userAccountAutoLiq/list': [
                {'id': 7, 'changesLocked': True, 'dailyLossAutoLiq': 500.0,
                 'trailingMaxDrawdown': 2000.0, 'marginPercentageAlert': 80.0}
            ],
        },
        tmp_path,
    )  # fmt: skip
    margin = client.margin_snapshots()[0]
    assert (margin.initial_margin, margin.total_used_margin) == (1500.0, 1500.0)
    assert margin.liq_only_level is None
    limits = client.auto_liqs()[0]
    assert limits.changes_locked is True
    assert (limits.daily_loss_auto_liq, limits.trailing_max_drawdown) == (500.0, 2000.0)
    assert limits.weekly_loss_auto_liq is None
    assert limits.raw['marginPercentageAlert'] == 80.0


# ── contracts ───────────────────────────────────────────────────────────────


def test_suggest_sends_tradovates_two_parameters(tmp_path):
    client = build(
        {'/contract/suggest': [{'id': 11, 'name': 'ABCZ6', 'contractMaturityId': 300}]},
        tmp_path,
    )
    found = client.suggest_contracts('ABC', 5)
    assert [(c.id, c.name, c.contract_maturity_id) for c in found] == [
        (11, 'ABCZ6', 300)
    ]
    call = client._transport.calls[-1]
    assert call['params'] == {'t': 'ABC', 'l': 5}


def test_rolling_asks_tradovate_for_the_next_contract(tmp_path):
    client = build(
        {'/contract/rollcontract': {'contract': {'id': 12, 'name': 'ABCH7'}}}, tmp_path
    )
    assert client.roll_contract('ABCZ6').name == 'ABCH7'
    assert client._transport.bodies('/contract/rollcontract') == [
        {'name': 'ABCZ6', 'forward': True, 'ifExpired': None}
    ]


def test_rolling_backwards(tmp_path):
    client = build(
        {'/contract/rollcontract': {'contract': {'id': 10, 'name': 'ABCU6'}}}, tmp_path
    )
    client.roll_contract('ABCZ6', forward=False, if_expired=True)
    assert client._transport.bodies('/contract/rollcontract') == [
        {'name': 'ABCZ6', 'forward': False, 'ifExpired': True}
    ]


@pytest.mark.parametrize('reply', [{'errorText': 'unknown contract'}, {}, None])
def test_a_roll_tradovate_cannot_do_raises(tmp_path, reply):
    client = build({'/contract/rollcontract': reply}, tmp_path)
    with pytest.raises(NotFoundError):
        client.roll_contract('NOPE')


def test_contract_maturity(tmp_path):
    client = build(
        {
            '/contractMaturity/item': {
                'id': 300,
                'productId': 40,
                'expirationMonth': 202612,
                'expirationDate': '2026-12-18T14:30Z',
                'isFront': True,
            }
        },
        tmp_path,
    )
    maturity = client.contract_maturity(300)
    assert (maturity.product_id, maturity.expiration_month) == (40, 202612)
    assert maturity.is_front is True
    assert client._transport.calls[-1]['params'] == {'id': 300}


# ── order strategies ────────────────────────────────────────────────────────


def test_the_bracket_params_are_the_shape_in_tradovates_example():
    assert build_bracket_params(
        qty=1,
        brackets=[
            {'qty': 1, 'profit_target': 75, 'stop_loss': -33, 'trailing_stop': False}
        ],
    ) == {
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


def test_a_limit_entry_carries_its_price():
    params = build_bracket_params(
        qty=2, order_type='Limit', price=100.5, brackets=[{'qty': 2, 'stop_loss': -10}]
    )
    assert params['entryVersion'] == {
        'orderId': 0, 'orderQty': 2, 'orderType': 'Limit', 'price': 100.5, 'timeInForce': 'Day'
    }  # fmt: skip


def test_a_field_this_library_has_not_named_is_passed_through():
    params = build_bracket_params(qty=1, brackets=[{'qty': 1, 'someNewField': 3}])
    assert params['brackets'] == [{'qty': 1, 'someNewField': 3}]


@pytest.mark.parametrize(
    'kw',
    [
        {'qty': 1, 'brackets': []},
        {'qty': 0, 'brackets': [{'qty': 1}]},
        {'qty': 1, 'brackets': [{'profit_target': 5}]},
    ],
)
def test_bracket_params_supply_nothing_themselves(kw):
    """No default bracket, no default size: the caller's or none."""
    with pytest.raises(ConfigError):
        build_bracket_params(**kw)


def test_starting_a_bracket_strategy(tmp_path):
    client = build(
        {
            '/orderStrategy/startorderstrategy': {
                'orderStrategy': {'id': 5, 'accountId': 7, 'contractId': 11, 'action': 'Buy',
                                  'orderStrategyTypeId': 2, 'status': 'ActiveStrategy'}
            }
        },
        tmp_path,
    )  # fmt: skip
    strategy = client.bracket_strategy(
        Action.BUY,
        'ABCZ6',
        1,
        brackets=[{'qty': 1, 'profit_target': 75, 'stop_loss': -33}],
    )
    assert (strategy.id, strategy.status, strategy.is_active) == (
        5,
        'ActiveStrategy',
        True,
    )
    sent = client._transport.bodies('/orderStrategy/startorderstrategy')[0]
    assert sent['orderStrategyTypeId'] == BRACKET_STRATEGY_TYPE_ID
    assert (sent['accountId'], sent['accountSpec'], sent['symbol'], sent['action']) == (
        7, 'DEMO7', 'ABCZ6', 'Buy'
    )  # fmt: skip
    assert isinstance(sent['params'], str)
    assert json.loads(sent['params'])['brackets'] == [
        {'qty': 1, 'profitTarget': 75, 'stopLoss': -33}
    ]


def test_a_refused_strategy_raises(tmp_path):
    client = build(
        {'/orderStrategy/startorderstrategy': {'errorText': 'not permitted'}}, tmp_path
    )
    with pytest.raises(OrderRejected, match='not permitted'):
        client.bracket_strategy(Action.BUY, 'ABCZ6', 1, brackets=[{'qty': 1}])


def test_interrupting_and_listing_strategies(tmp_path):
    client = build(
        {
            '/orderStrategy/interruptorderstrategy': {
                'orderStrategy': {'id': 5, 'status': 'ExecutionInterrupted'}
            },
            '/orderStrategy/list': [{'id': 5, 'status': 'ExecutionInterrupted'}],
        },
        tmp_path,
    )
    stopped = client.interrupt_order_strategy(5)
    assert stopped.status == 'ExecutionInterrupted' and not stopped.is_active
    assert client._transport.bodies('/orderStrategy/interruptorderstrategy') == [
        {'orderStrategyId': 5}
    ]
    assert [s.id for s in client.order_strategies()] == [5]
