"""Order bodies are asserted without any network access."""

import pytest

from tradovate_client import Action, OrderType, TimeInForce, build_order
from tradovate_client._http import _drop_none
from tradovate_client.errors import ConfigError
from tradovate_client.orders import build_bracket_leg

BASE = {
    'account_id': 12345,
    'account_spec': 'DEMO1234',
    'action': Action.BUY,
    'symbol': 'MNQZ6',
    'qty': 2,
}


def test_market_order_body():
    body = _drop_none(build_order(order_type=OrderType.MARKET, **BASE))
    assert body == {
        'accountId': 12345,
        'accountSpec': 'DEMO1234',
        'action': 'Buy',
        'symbol': 'MNQZ6',
        'orderQty': 2,
        'orderType': 'Market',
        'timeInForce': 'Day',
        'isAutomated': True,
    }


def test_unset_optionals_are_omitted_not_null():
    body = _drop_none(build_order(order_type=OrderType.MARKET, **BASE))
    for absent in (
        'price',
        'stopPrice',
        'maxShow',
        'pegDifference',
        'expireTime',
        'text',
        'activationTime',
        'customTag50',
        'clOrdId',
    ):
        assert absent not in body


def test_limit_order_carries_price():
    body = _drop_none(build_order(order_type=OrderType.LIMIT, price=20150.25, **BASE))
    assert body['orderType'] == 'Limit' and body['price'] == 20150.25


def test_stop_limit_carries_both_prices():
    body = _drop_none(
        build_order(
            order_type=OrderType.STOP_LIMIT, price=20100.0, stop_price=20090.0, **BASE
        )
    )
    assert body['price'] == 20100.0 and body['stopPrice'] == 20090.0


def test_time_in_force_can_be_changed():
    body = _drop_none(
        build_order(
            order_type=OrderType.LIMIT, price=1.0, time_in_force=TimeInForce.GTC, **BASE
        )
    )
    assert body['timeInForce'] == 'GTC'


def test_is_automated_defaults_true():
    assert build_order(order_type=OrderType.MARKET, **BASE)['isAutomated'] is True


@pytest.mark.parametrize(
    'order_type', [OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP_LIMIT]
)
def test_missing_limit_price_is_refused(order_type):
    with pytest.raises(ConfigError, match='needs price'):
        build_order(order_type=order_type, stop_price=1.0, **BASE)


@pytest.mark.parametrize('order_type', [OrderType.STOP, OrderType.TRAILING_STOP])
def test_missing_stop_price_is_refused(order_type):
    with pytest.raises(ConfigError, match='needs stop_price'):
        build_order(order_type=order_type, **BASE)


def test_market_order_with_a_price_is_refused():
    with pytest.raises(ConfigError, match='carries no price'):
        build_order(order_type=OrderType.MARKET, price=20000.0, **BASE)


def test_strict_can_be_turned_off():
    body = build_order(order_type=OrderType.LIMIT, strict=False, **BASE)
    assert body['price'] is None


@pytest.mark.parametrize('qty', [0, -1])
def test_non_positive_quantity_is_refused(qty):
    args = dict(BASE, qty=qty)
    with pytest.raises(ConfigError, match='positive'):
        build_order(order_type=OrderType.MARKET, **args)


def test_bad_action_is_refused():
    args = dict(BASE, action='buy')
    with pytest.raises(ValueError):
        build_order(order_type=OrderType.MARKET, **args)


def test_bracket_leg_has_no_account_symbol_or_quantity():
    leg = _drop_none(
        build_bracket_leg(action=Action.SELL, order_type=OrderType.LIMIT, price=20200.0)
    )
    assert leg == {
        'action': 'Sell',
        'orderType': 'Limit',
        'price': 20200.0,
        'timeInForce': 'Day',
    }
    for inherited in ('accountId', 'accountSpec', 'symbol', 'orderQty'):
        assert inherited not in leg
