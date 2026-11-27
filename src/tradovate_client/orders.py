"""Order payload construction, kept separate from any network access.

Splitting this out means the exact bytes that would be sent to Tradovate can
be asserted in a unit test without a broker account, a token, or a socket.
"""

import json

from .enums import Action, OrderType, TimeInForce
from .errors import ConfigError

__all__ = [
    'build_order',
    'build_bracket_leg',
    'build_modify',
    'build_order_strategy',
    'build_bracket_params',
    'BRACKET_STRATEGY_TYPE_ID',
    'LIMIT_PRICE_REQUIRED',
    'STOP_PRICE_REQUIRED',
]

#: Order types that cannot be sent without ``price``. Taken from the plain
#: meaning of the type, not from a schema -- Tradovate marks every price field
#: optional, because which one applies depends on the order type.
LIMIT_PRICE_REQUIRED = frozenset(
    {OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.TRAILING_STOP_LIMIT}
)

#: Order types that cannot be sent without ``stopPrice``.
STOP_PRICE_REQUIRED = frozenset(
    {
        OrderType.STOP,
        OrderType.STOP_LIMIT,
        OrderType.TRAILING_STOP,
        OrderType.TRAILING_STOP_LIMIT,
    }
)


def _check(order_type, price, stop_price, strict) -> None:
    if not strict:
        return
    if order_type in LIMIT_PRICE_REQUIRED and price is None:
        raise ConfigError('%s needs price=' % order_type.value)
    if order_type in STOP_PRICE_REQUIRED and stop_price is None:
        raise ConfigError('%s needs stop_price=' % order_type.value)
    if order_type is OrderType.MARKET and (price is not None or stop_price is not None):
        raise ConfigError('a Market order carries no price; drop price=/stop_price=')


def build_order(
    *,
    account_id,
    account_spec,
    action,
    symbol,
    qty,
    order_type,
    price=None,
    stop_price=None,
    time_in_force=TimeInForce.DAY,
    expire_time=None,
    max_show=None,
    peg_difference=None,
    text=None,
    activation_time=None,
    client_order_id=None,
    custom_tag_50=None,
    is_automated=True,
    strict=True,
) -> dict:
    """Return the body for ``/order/placeorder``.

    Field names and the set of legal values come from Tradovate's generated
    model documentation. ``None`` values are dropped by the transport, so
    optional fields the caller did not set are simply absent.
    """
    action = Action(action)
    order_type = OrderType(order_type)
    if int(qty) <= 0:
        raise ConfigError('qty must be a positive whole number of contracts')
    _check(order_type, price, stop_price, strict)
    return {
        'accountId': int(account_id),
        'accountSpec': account_spec,
        'clOrdId': client_order_id,
        'action': action.value,
        'symbol': symbol,
        'orderQty': int(qty),
        'orderType': order_type.value,
        'price': price,
        'stopPrice': stop_price,
        'maxShow': max_show,
        'pegDifference': peg_difference,
        'timeInForce': TimeInForce(time_in_force).value if time_in_force else None,
        'expireTime': expire_time,
        'text': text,
        'activationTime': activation_time,
        'customTag50': custom_tag_50,
        'isAutomated': bool(is_automated),
    }


def build_bracket_leg(
    *,
    action,
    order_type,
    price=None,
    stop_price=None,
    time_in_force=TimeInForce.DAY,
    expire_time=None,
    max_show=None,
    peg_difference=None,
    text=None,
    client_order_id=None,
    strict=True,
) -> dict:
    """Return one ``RestrainedOrderVersion`` -- the shape OSO and OCO want.

    A bracket leg has no account, symbol or quantity of its own: it inherits
    them from the parent order.
    """
    action = Action(action)
    order_type = OrderType(order_type)
    _check(order_type, price, stop_price, strict)
    return {
        'action': action.value,
        'clOrdId': client_order_id,
        'orderType': order_type.value,
        'price': price,
        'stopPrice': stop_price,
        'maxShow': max_show,
        'pegDifference': peg_difference,
        'timeInForce': TimeInForce(time_in_force).value if time_in_force else None,
        'expireTime': expire_time,
        'text': text,
    }


def build_modify(
    order_id,
    *,
    current=None,
    qty=None,
    order_type=None,
    price=None,
    stop_price=None,
    time_in_force=None,
    expire_time=None,
    max_show=None,
    peg_difference=None,
    text=None,
    activation_time=None,
    client_order_id=None,
    custom_tag_50=None,
    is_automated=True,
) -> dict:
    """Return the body for ``/order/modifyorder``.

    Tradovate's modifyorder replaces the order rather than patching it:
    ``orderQty`` is required on every call, and ``orderType`` with it. A
    caller who only means to move a price still has to resend the size, so
    anything not passed is taken from ``current`` -- the order's latest
    ``OrderVersion``. Without it the endpoint answers 400 'missing required
    field "orderQty"' and the order is left exactly as it was.
    """
    if (qty is None or order_type is None) and current:
        if qty is None:
            qty = current.get('orderQty')
        if order_type is None:
            order_type = current.get('orderType')
        if price is None:
            price = current.get('price')
        if stop_price is None:
            stop_price = current.get('stopPrice')
        if time_in_force is None:
            time_in_force = current.get('timeInForce')
    return {
        'orderId': int(order_id),
        'clOrdId': client_order_id,
        'orderQty': int(qty) if qty is not None else None,
        'orderType': OrderType(order_type).value if order_type else None,
        'price': price,
        'stopPrice': stop_price,
        'maxShow': max_show,
        'pegDifference': peg_difference,
        'timeInForce': TimeInForce(time_in_force).value if time_in_force else None,
        'expireTime': expire_time,
        'text': text,
        'activationTime': activation_time,
        'customTag50': custom_tag_50,
        'isAutomated': bool(is_automated),
    }


#: The ``orderStrategyTypeId`` Tradovate's own examples use for a bracket
#: strategy -- ``tradovate/example-api-faq``,
#: ``example-code/start-an-order-strategy``, and
#: ``tradovate/example-api-trading-strategy``,
#: ``standardMiddleware/startOrderStrategy.js``.
BRACKET_STRATEGY_TYPE_ID = 2

#: Python-side names for the bracket fields in Tradovate's example ``params``.
_BRACKET_KEYS = {
    'profit_target': 'profitTarget',
    'stop_loss': 'stopLoss',
    'trailing_stop': 'trailingStop',
}


def build_bracket_params(
    *,
    qty,
    brackets,
    order_type=OrderType.MARKET,
    price=None,
    stop_price=None,
    time_in_force=TimeInForce.DAY,
) -> dict:
    """Return the ``params`` object for a bracket order strategy.

    The shape is the one in Tradovate's own example
    (``example-api-faq/example-code/start-an-order-strategy/src/data.js``)::

        {"entryVersion": {"orderId": 0, "orderQty": 1,
                          "orderType": "Market", "timeInForce": "Day"},
         "brackets": [{"qty": 1, "profitTarget": 75, "stopLoss": -33,
                       "trailingStop": false}]}

    Every bracket is the caller's own: this function supplies no target, no
    stop and no distance. Each one is a mapping with ``qty`` and any of
    ``profit_target``, ``stop_loss`` and ``trailing_stop``; a key it does not
    know is passed through untouched, so a field Tradovate documents and this
    library has not named can still be sent.
    """
    order_type = OrderType(order_type)
    if int(qty) <= 0:
        raise ConfigError('qty must be a positive whole number of contracts')
    if not brackets:
        raise ConfigError('give at least one bracket')
    legs = []
    for bracket in brackets:
        if not isinstance(bracket, dict) or bracket.get('qty') is None:
            raise ConfigError('each bracket is a mapping with qty=')
        legs.append({_BRACKET_KEYS.get(k, k): v for k, v in bracket.items()})
    entry = {
        'orderId': 0,
        'orderQty': int(qty),
        'orderType': order_type.value,
        'price': price,
        'stopPrice': stop_price,
        'timeInForce': TimeInForce(time_in_force).value if time_in_force else None,
    }
    return {
        'entryVersion': {k: v for k, v in entry.items() if v is not None},
        'brackets': legs,
    }


def build_order_strategy(
    *,
    account_id,
    account_spec,
    action,
    symbol,
    order_strategy_type_id,
    params=None,
    uuid=None,
    custom_tag_50=None,
) -> dict:
    """Return the body for ``/orderStrategy/startorderstrategy``.

    Field names are from Tradovate's ``StartOrderStrategy`` model. ``params``
    travels as a JSON string, which is how their own examples send it; a
    mapping given here is serialised for you.
    """
    if params is not None and not isinstance(params, str):
        params = json.dumps(params)
    return {
        'accountId': int(account_id),
        'accountSpec': account_spec,
        'symbol': symbol,
        'orderStrategyTypeId': int(order_strategy_type_id),
        'action': Action(action).value,
        'params': params,
        'uuid': uuid,
        'customTag50': custom_tag_50,
    }
