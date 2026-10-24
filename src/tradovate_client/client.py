"""The REST client."""

import time

from . import orders as _orders
from ._http import Transport
from .auth import Credentials, TokenStore, RENEW_MARGIN_SECONDS
from .enums import Action, Env, OrderType, TimeInForce
from .errors import AuthError, ConfigError, NotFoundError
from .models import (Account, CashBalance, Contract, Fill, Order, Position,
                     PositionView, Snapshot)

__all__ = ['TradovateClient']


class TradovateClient:
    """A Tradovate REST client bound to one environment.

    ``env`` is required and has no default. Demo and live are different hosts
    with different credentials, so the environment is a property of the client
    rather than a flag on each call -- there is no code path that reaches the
    live host from a client built for demo.

        >>> from tradovate_client import TradovateClient, Env
        >>> client = TradovateClient(Env.DEMO)          # reads ~/.tradovate_key
        >>> client.snapshot().cash.total_cash           # doctest: +SKIP
    """

    DEFAULT_CREDENTIAL_PATH = '~/.tradovate_key'

    def __init__(self, env, *, credentials=None, credential_path=None,
                 token_store=None, token_dir=None, account=None,
                 timeout=20.0, retries=2, clock=time.time, sleep=time.sleep):
        self.env = Env(env)
        self._clock = clock
        self._credentials = credentials
        self._credential_path = credential_path
        self._transport = Transport(self.env.rest_url, timeout=timeout,
                                    retries=retries, sleep=sleep)
        self._tokens = token_store or TokenStore(self.env, directory=token_dir,
                                                 clock=clock)
        self._account_hint = account
        self._account = None

    # ── credentials and tokens ──────────────────────────────────────────────

    @property
    def credentials(self):
        if self._credentials is None:
            self._credentials = Credentials.from_file(
                self._credential_path or self.DEFAULT_CREDENTIAL_PATH)
        return self._credentials

    def token(self, *, force=False):
        """A usable access token, renewed rather than re-requested when possible."""
        if not force:
            cached, expires = self._tokens.read()
            if cached and (expires - self._clock()) > RENEW_MARGIN_SECONDS:
                return cached
            if cached:
                try:
                    reply = self._transport.request(
                        'GET', '/auth/renewaccesstoken', token=cached)
                    return self._tokens.write(reply or {})
                except (AuthError, ConfigError):
                    self._tokens.clear()
        reply = self._transport.request('POST', '/auth/accesstokenrequest',
                                        body=self.credentials.as_payload())
        if not (reply or {}).get('accessToken'):
            raise AuthError('%s authentication failed' % self.env.value,
                            endpoint='/auth/accesstokenrequest', body=reply)
        return self._tokens.write(reply)

    def _get(self, path, **params):
        return self._transport.request('GET', path, token=self.token(),
                                       params=params or None)

    def _post(self, path, body):
        return self._transport.request('POST', path, token=self.token(), body=body)

    # ── reads ───────────────────────────────────────────────────────────────

    def accounts(self):
        return [Account.from_api(a) for a in (self._get('/account/list') or [])
                if isinstance(a, dict)]

    def account(self, name=None):
        """Resolve one account.

        Pass a name or id, or set ``account=`` on the client. With neither, and
        exactly one account on the login, that one is used. With neither and
        several accounts, this raises rather than guessing -- picking the first
        of a list is how an order lands in the wrong account.
        """
        wanted = name if name is not None else self._account_hint
        if self._account is not None and wanted is None:
            return self._account
        found = self.accounts()
        if not found:
            raise NotFoundError('this login has no accounts',
                                endpoint='/account/list')
        if wanted is None:
            if len(found) > 1:
                raise ConfigError(
                    'this login has %d accounts (%s); say which one with '
                    'account=' % (len(found), ', '.join(a.name for a in found)))
            chosen = found[0]
        else:
            matches = [a for a in found
                       if a.name == str(wanted) or a.id == _as_int(wanted)]
            if not matches:
                raise NotFoundError(
                    'no account %r; this login has: %s'
                    % (wanted, ', '.join(a.name for a in found)))
            chosen = matches[0]
        if name is None:
            self._account = chosen
        return chosen

    def cash_balance(self, account_id=None):
        account_id = account_id or self.account().id
        return CashBalance.from_api(
            self._post('/cashBalance/getcashbalancesnapshot',
                       {'accountId': int(account_id)}))

    def positions(self, account_id=None):
        account_id = account_id or self.account().id
        return [p for p in (Position.from_api(d)
                            for d in (self._get('/position/list') or [])
                            if isinstance(d, dict))
                if p.account_id == account_id]

    def contracts(self):
        return [Contract.from_api(d) for d in (self._get('/contract/list') or [])
                if isinstance(d, dict)]

    def contract(self, contract_id):
        d = self._get('/contract/item', id=int(contract_id))
        if not isinstance(d, dict) or 'id' not in d:
            raise NotFoundError('no contract %s' % contract_id,
                                endpoint='/contract/item')
        return Contract.from_api(d)

    def find_contract(self, name):
        d = self._get('/contract/find', name=name)
        if not isinstance(d, dict) or 'id' not in d:
            raise NotFoundError('no contract named %r' % name,
                                endpoint='/contract/find')
        return Contract.from_api(d)

    def fills(self):
        return [Fill.from_api(d) for d in (self._get('/fill/list') or [])
                if isinstance(d, dict)]

    def fills_for_order(self, order_id):
        """The fills belonging to one order -- the real prices it traded at."""
        return [Fill.from_api(d)
                for d in (self._get('/fill/deps', masterid=int(order_id)) or [])
                if isinstance(d, dict)]

    def orders(self):
        return [Order.from_api(d) for d in (self._get('/order/list') or [])
                if isinstance(d, dict)]

    def order(self, order_id):
        d = self._get('/order/item', id=int(order_id))
        if not isinstance(d, dict) or 'id' not in d:
            raise NotFoundError('no order %s' % order_id, endpoint='/order/item')
        return Order.from_api(d)

    def working_orders(self):
        return [o for o in self.orders() if o.is_working]

    def snapshot(self, account=None):
        """Account, cash and named positions in one object.

        Contract names are resolved only for positions actually held, so this
        does not pull the whole contract list on every poll.
        """
        acct = self.account(account)
        held = {}
        for pos in self.positions(acct.id):
            if pos.is_flat:
                continue
            try:
                symbol = self.contract(pos.contract_id).name
            except NotFoundError:
                symbol = str(pos.contract_id)
            held[symbol] = PositionView(symbol=symbol, net_pos=pos.net_pos,
                                        net_price=pos.net_price,
                                        contract_id=pos.contract_id)
        return Snapshot(account=acct, cash=self.cash_balance(acct.id),
                        positions=held)

    # ── placing orders ──────────────────────────────────────────────────────

    def place(self, *, action, symbol, qty, order_type, account=None, **kw):
        """Place any order type. Returns Tradovate's reply as a dict."""
        acct = self.account(account)
        body = _orders.build_order(account_id=acct.id, account_spec=acct.spec,
                                   action=action, symbol=symbol, qty=qty,
                                   order_type=order_type, **kw)
        return self._post('/order/placeorder', body)

    def market(self, action, symbol, qty, **kw):
        return self.place(action=action, symbol=symbol, qty=qty,
                          order_type=OrderType.MARKET, **kw)

    def limit(self, action, symbol, qty, price, **kw):
        return self.place(action=action, symbol=symbol, qty=qty,
                          order_type=OrderType.LIMIT, price=price, **kw)

    def stop(self, action, symbol, qty, stop_price, **kw):
        return self.place(action=action, symbol=symbol, qty=qty,
                          order_type=OrderType.STOP, stop_price=stop_price, **kw)

    def stop_limit(self, action, symbol, qty, price, stop_price, **kw):
        return self.place(action=action, symbol=symbol, qty=qty,
                          order_type=OrderType.STOP_LIMIT, price=price,
                          stop_price=stop_price, **kw)

    def trailing_stop(self, action, symbol, qty, stop_price, **kw):
        return self.place(action=action, symbol=symbol, qty=qty,
                          order_type=OrderType.TRAILING_STOP,
                          stop_price=stop_price, **kw)

    def trailing_stop_limit(self, action, symbol, qty, price, stop_price, **kw):
        return self.place(action=action, symbol=symbol, qty=qty,
                          order_type=OrderType.TRAILING_STOP_LIMIT,
                          price=price, stop_price=stop_price, **kw)

    def market_if_touched(self, action, symbol, qty, price, **kw):
        return self.place(action=action, symbol=symbol, qty=qty,
                          order_type=OrderType.MIT, price=price, **kw)

    def place_oco(self, *, action, symbol, qty, order_type, other,
                  account=None, **kw):
        """One-cancels-other. ``other`` comes from ``bracket_leg``."""
        acct = self.account(account)
        body = _orders.build_order(account_id=acct.id, account_spec=acct.spec,
                                   action=action, symbol=symbol, qty=qty,
                                   order_type=order_type, **kw)
        body['other'] = other
        return self._post('/order/placeoco', body)

    def place_oso(self, *, action, symbol, qty, order_type, bracket1,
                  bracket2=None, account=None, **kw):
        """One-sends-other: an entry with one or two child orders attached."""
        acct = self.account(account)
        body = _orders.build_order(account_id=acct.id, account_spec=acct.spec,
                                   action=action, symbol=symbol, qty=qty,
                                   order_type=order_type, **kw)
        body['bracket1'] = bracket1
        if bracket2 is not None:
            body['bracket2'] = bracket2
        return self._post('/order/placeoso', body)

    @staticmethod
    def bracket_leg(action, order_type, **kw):
        """Build one child leg for ``place_oso`` or ``place_oco``."""
        return _orders.build_bracket_leg(action=action, order_type=order_type, **kw)

    def bracketed_market(self, action, symbol, qty, *, take_profit=None,
                         stop_loss=None, account=None, **kw):
        """A market entry with a profit target and a protective stop attached.

        Both child prices are the caller's own. This library never invents a
        stop distance or a target.
        """
        if take_profit is None and stop_loss is None:
            raise ConfigError('give take_profit=, stop_loss=, or both')
        exit_side = Action(action).opposite
        legs = []
        if take_profit is not None:
            legs.append(self.bracket_leg(exit_side, OrderType.LIMIT,
                                         price=take_profit))
        if stop_loss is not None:
            legs.append(self.bracket_leg(exit_side, OrderType.STOP,
                                         stop_price=stop_loss))
        return self.place_oso(action=action, symbol=symbol, qty=qty,
                              order_type=OrderType.MARKET, bracket1=legs[0],
                              bracket2=legs[1] if len(legs) > 1 else None,
                              account=account, **kw)

    # ── changing and cancelling ─────────────────────────────────────────────

    def modify(self, order_id, *, qty=None, order_type=None, price=None,
               stop_price=None, time_in_force=None, expire_time=None,
               max_show=None, peg_difference=None, text=None,
               activation_time=None, client_order_id=None, custom_tag_50=None,
               is_automated=True):
        body = {
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
        return self._post('/order/modifyorder', body)

    def cancel(self, order_id, *, client_order_id=None, custom_tag_50=None,
               is_automated=True):
        return self._post('/order/cancelorder', {
            'orderId': int(order_id),
            'clOrdId': client_order_id,
            'customTag50': custom_tag_50,
            'isAutomated': bool(is_automated),
        })

    def cancel_working(self):
        """Cancel every working order. Returns the ids it cancelled."""
        cancelled = []
        for order in self.working_orders():
            self.cancel(order.id)
            cancelled.append(order.id)
        return cancelled

    def liquidate(self, contract_id, *, account=None, admin=False,
                  custom_tag_50=None):
        """Flatten one contract with Tradovate's own liquidate endpoint."""
        acct = self.account(account)
        return self._post('/order/liquidateposition', {
            'accountId': acct.id,
            'contractId': int(contract_id),
            'admin': bool(admin),
            'customTag50': custom_tag_50,
        })

    def flatten_all(self, *, account=None, cancel_first=True):
        """Cancel working orders, then liquidate every held contract.

        Returns ``{'cancelled': [...], 'liquidated': [...]}``. This closes real
        positions when the client is on ``Env.LIVE``.
        """
        result = {'cancelled': [], 'liquidated': []}
        if cancel_first:
            result['cancelled'] = self.cancel_working()
        acct = self.account(account)
        for pos in self.positions(acct.id):
            if pos.is_flat:
                continue
            self.liquidate(pos.contract_id, account=acct.name)
            result['liquidated'].append(pos.contract_id)
        return result

    def __repr__(self):
        return 'TradovateClient(env=%r)' % self.env.value


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
