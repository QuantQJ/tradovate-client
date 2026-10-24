"""Order payload construction, kept separate from any network access.

Splitting this out means the exact bytes that would be sent to Tradovate can
be asserted in a unit test without a broker account, a token, or a socket.
"""

from .enums import Action, OrderType, TimeInForce
from .errors import ConfigError

__all__ = ['build_order', 'build_bracket_leg', 'LIMIT_PRICE_REQUIRED',
           'STOP_PRICE_REQUIRED']

#: Order types that cannot be sent without ``price``. Taken from the plain
#: meaning of the type, not from a schema -- Tradovate marks every price field
#: optional, because which one applies depends on the order type.
LIMIT_PRICE_REQUIRED = frozenset({OrderType.LIMIT, OrderType.STOP_LIMIT,
                                  OrderType.TRAILING_STOP_LIMIT})

#: Order types that cannot be sent without ``stopPrice``.
STOP_PRICE_REQUIRED = frozenset({OrderType.STOP, OrderType.STOP_LIMIT,
                                 OrderType.TRAILING_STOP,
                                 OrderType.TRAILING_STOP_LIMIT})


def _check(order_type, price, stop_price, strict):
    if not strict:
        return
    if order_type in LIMIT_PRICE_REQUIRED and price is None:
        raise ConfigError('%s needs price=' % order_type.value)
    if order_type in STOP_PRICE_REQUIRED and stop_price is None:
        raise ConfigError('%s needs stop_price=' % order_type.value)
    if order_type is OrderType.MARKET and (price is not None or stop_price is not None):
        raise ConfigError('a Market order carries no price; drop price=/stop_price=')


def build_order(*, account_id, account_spec, action, symbol, qty, order_type,
                price=None, stop_price=None, time_in_force=TimeInForce.DAY,
                expire_time=None, max_show=None, peg_difference=None,
                text=None, activation_time=None, client_order_id=None,
                custom_tag_50=None, is_automated=True, strict=True):
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


def build_bracket_leg(*, action, order_type, price=None, stop_price=None,
                      time_in_force=TimeInForce.DAY, expire_time=None,
                      max_show=None, peg_difference=None, text=None,
                      client_order_id=None, strict=True):
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
