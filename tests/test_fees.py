"""Fill fees, fill pairs and products.

What a trade actually cost and what it actually made, from Tradovate's own
records rather than from arithmetic in this library.
"""

from __future__ import annotations

import pytest

from tradovate_client import (
    Credentials,
    Env,
    FillFee,
    FillPair,
    Product,
    TradovateClient,
)
from tradovate_client.errors import NotFoundError

CREDS = Credentials(
    name='u', password='p', appId='a', appVersion='1', deviceId='d', cid=1, sec='s'
)


class FakeTransport:
    def __init__(self, replies):
        self.replies = replies
        self.calls = []

    def request(self, method, path, *, token=None, body=None, params=None):
        self.calls.append({'method': method, 'path': path, 'params': params})
        if path in self.replies:
            value = self.replies[path]
            return value(params) if callable(value) else value
        raise AssertionError('unexpected call to %s' % path)


def build(replies, tmp_path):
    client = TradovateClient(Env.DEMO, credentials=CREDS, token_dir=tmp_path)
    client._transport = FakeTransport(
        dict(
            {
                '/auth/accesstokenrequest': {
                    'accessToken': 'tok',
                    'expirationTime': '2030-01-01T00:00:00Z',
                }
            },
            **replies,
        )
    )
    return client


# ── FillFee ─────────────────────────────────────────────────────────────────

FEE = {
    'id': 11,
    'clearingFee': 0.07,
    'exchangeFee': 0.37,
    'nfaFee': 0.02,
    'brokerageFee': 0.0,
    'ipFee': 0.0,
    'commission': 0.15,
    'orderRoutingFee': 0.0,
}


def test_the_seven_charges_tradovate_reports():
    assert FillFee.COMPONENTS == (
        'clearing_fee',
        'exchange_fee',
        'nfa_fee',
        'brokerage_fee',
        'ip_fee',
        'commission',
        'order_routing_fee',
    )


def test_a_fee_record_is_normalised():
    fee = FillFee.from_api(FEE)
    assert fee.id == 11
    assert fee.clearing_fee == 0.07
    assert fee.exchange_fee == 0.37
    assert fee.commission == 0.15


def test_total_is_every_charge_added_up():
    """Commission alone is not the cost. Exchange and clearing are real."""
    fee = FillFee.from_api(FEE)
    assert round(fee.total, 10) == 0.61
    assert fee.total > fee.commission, 'commission is a fraction of the total'


def test_breakdown_shows_only_what_was_charged():
    assert FillFee.from_api(FEE).breakdown() == {
        'clearing_fee': 0.07,
        'exchange_fee': 0.37,
        'nfa_fee': 0.02,
        'commission': 0.15,
    }


def test_a_missing_charge_is_zero_not_a_guess():
    fee = FillFee.from_api({'id': 1, 'commission': 1.0})
    assert fee.exchange_fee == 0.0 and fee.total == 1.0
    assert fee.breakdown() == {'commission': 1.0}


def test_the_raw_record_is_kept_for_the_currency_fields():
    """Each charge has its own currency id; multi-currency callers need them."""
    fee = FillFee.from_api(dict(FEE, commissionCurrencyId=1))
    assert fee.raw['commissionCurrencyId'] == 1


# ── FillPair ────────────────────────────────────────────────────────────────

PAIR = {
    'id': 7,
    'positionId': 3,
    'buyFillId': 11,
    'sellFillId': 12,
    'qty': 2,
    'buyPrice': 20100.0,
    'sellPrice': 20110.0,
    'active': False,
}


def test_a_pair_is_normalised():
    pair = FillPair.from_api(PAIR)
    assert pair.id == 7 and pair.position_id == 3
    assert pair.buy_fill_id == 11 and pair.sell_fill_id == 12
    assert pair.qty == 2


def test_points_is_the_move_times_the_quantity():
    assert FillPair.from_api(PAIR).points == 20.0


def test_a_losing_pair_is_negative():
    pair = FillPair.from_api(dict(PAIR, buyPrice=20110.0, sellPrice=20100.0))
    assert pair.points == -20.0


def test_money_needs_the_contracts_own_multiplier():
    """The point value is the broker's to report, not ours to assume."""
    pair = FillPair.from_api(PAIR)
    assert pair.gross(2.0) == 40.0
    assert pair.gross(20.0) == 400.0, 'the same points, a different contract'


def test_gross_refuses_without_a_multiplier():
    with pytest.raises(ValueError, match='value_per_point is required'):
        FillPair.from_api(PAIR).gross(None)


def test_net_takes_the_fees_off_both_legs():
    pair = FillPair.from_api(PAIR)
    legs = [FillFee.from_api(FEE), FillFee.from_api(FEE)]
    assert round(pair.net(2.0, legs), 10) == round(40.0 - 1.22, 10)


def test_net_accepts_a_plain_number_too():
    assert FillPair.from_api(PAIR).net(2.0, 1.22) == pytest.approx(38.78)


# ── Product: where the multiplier comes from ────────────────────────────────

PRODUCT = {
    'id': 42,
    'name': 'ABC',
    'productType': 'Futures',
    'valuePerPoint': 2.0,
    'tickSize': 0.25,
    'currencyId': 1,
}


def test_a_product_carries_what_makes_prices_mean_money():
    product = Product.from_api(PRODUCT)
    assert product.name == 'ABC'
    assert product.value_per_point == 2.0
    assert product.tick_size == 0.25


def test_tick_value_is_derived_not_assumed():
    assert Product.from_api(PRODUCT).tick_value == 0.5


# ── through the client ──────────────────────────────────────────────────────


def test_fill_fees_are_listed(tmp_path):
    client = build({'/fillFee/list': [FEE]}, tmp_path)
    fees = client.fill_fees()
    assert len(fees) == 1 and round(fees[0].total, 10) == 0.61


def test_one_fee_is_fetched(tmp_path):
    client = build({'/fillFee/item': FEE}, tmp_path)
    assert client.fill_fee(11).id == 11


def test_a_missing_fee_raises(tmp_path):
    client = build({'/fillFee/item': {}}, tmp_path)
    with pytest.raises(NotFoundError, match='no fill fee'):
        client.fill_fee(99)


def test_the_charges_on_one_fill_use_the_deps_pattern(tmp_path):
    client = build({'/fillFee/deps': [FEE]}, tmp_path)
    client.fill_fees_for_fill(11)
    call = [c for c in client._transport.calls if c['path'] == '/fillFee/deps'][0]
    assert call['params'] == {'masterid': 11}


def test_cost_of_order_walks_fills_then_fees(tmp_path):
    """Order -> its fills -> each fill's charges, added up."""
    client = build(
        {
            '/fill/deps': [
                {
                    'id': 11,
                    'orderId': 4242,
                    'contractId': 1,
                    'qty': 1,
                    'price': 20100.0,
                },
                {
                    'id': 12,
                    'orderId': 4242,
                    'contractId': 1,
                    'qty': 1,
                    'price': 20110.0,
                },
            ],
            '/fillFee/deps': lambda params: [dict(FEE, id=params['masterid'])],
        },
        tmp_path,
    )
    cost = client.cost_of_order(4242)
    assert cost['fills'] == 2 and cost['fees'] == 2
    assert cost['total'] == pytest.approx(1.22), 'both sides of a round trip'
    assert cost['breakdown']['exchange_fee'] == pytest.approx(0.74)
    assert cost['fill_ids'] == [11, 12]


def test_an_order_with_no_reported_fees_costs_nothing_not_a_guess(tmp_path):
    client = build(
        {
            '/fill/deps': [{'id': 11, 'orderId': 1, 'contractId': 1, 'qty': 1}],
            '/fillFee/deps': [],
        },
        tmp_path,
    )
    cost = client.cost_of_order(1)
    assert cost['total'] == 0 and cost['fees'] == 0 and cost['breakdown'] == {}


def test_fill_pairs_are_listed(tmp_path):
    client = build({'/fillPair/list': [PAIR]}, tmp_path)
    assert client.fill_pairs()[0].points == 20.0


def test_pairs_for_a_position_use_ldeps(tmp_path):
    client = build({'/fillPair/ldeps': [PAIR]}, tmp_path)
    client.fill_pairs_for_position(3)
    call = [c for c in client._transport.calls if c['path'] == '/fillPair/ldeps'][0]
    assert call['params'] == {'masterids': 3}


def test_a_product_is_found_by_name(tmp_path):
    client = build({'/product/find': PRODUCT}, tmp_path)
    product = client.find_product('ABC')
    assert product.value_per_point == 2.0
    call = [c for c in client._transport.calls if c['path'] == '/product/find'][0]
    assert call['params'] == {'name': 'ABC'}


def test_an_unknown_product_raises(tmp_path):
    client = build({'/product/find': {}}, tmp_path)
    with pytest.raises(NotFoundError, match='no product named'):
        client.find_product('NOPE')


# ── round_trips: the multiplier is fetched, supplied, or absent ─────────────


def test_round_trips_fetch_the_multiplier_from_the_product(tmp_path):
    client = build({'/fillPair/list': [PAIR], '/product/find': PRODUCT}, tmp_path)
    rows = client.round_trips('ABC')
    assert rows[0]['points'] == 20.0
    assert rows[0]['gross'] == 40.0, 'the value the fixture\'s own response gives'


def test_round_trips_accept_your_own_multiplier(tmp_path):
    client = build({'/fillPair/list': [PAIR]}, tmp_path)
    rows = client.round_trips(value_per_point=20.0)
    assert rows[0]['gross'] == 400.0
    assert not any(c['path'] == '/product/find' for c in client._transport.calls), (
        'no lookup needed when you supply it'
    )


def test_round_trips_leave_money_out_rather_than_invent_it(tmp_path):
    client = build({'/fillPair/list': [PAIR]}, tmp_path)
    rows = client.round_trips()
    assert rows[0]['points'] == 20.0
    assert rows[0]['gross'] is None, 'no multiplier, no number'
