"""Typed views over Tradovate's JSON.

Each model keeps the raw payload on ``.raw`` so nothing is ever lost, and
tolerates fields Tradovate adds later.
"""

from dataclasses import dataclass, field

__all__ = [
    'Account',
    'CashBalance',
    'Contract',
    'Position',
    'Fill',
    'FillFee',
    'FillPair',
    'Order',
    'Product',
    'Snapshot',
    'PositionView',
    'ContractMaturity',
    'MarginSnapshot',
    'AutoLiq',
    'OrderStrategy',
]


@dataclass
class Account:
    id: int
    name: str
    account_type: str = ''
    active: bool = True
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "Account":
        return cls(
            id=d['id'],
            name=d.get('name') or str(d['id']),
            account_type=d.get('accountType') or '',
            active=bool(d.get('active', True)),
            raw=d,
        )

    @property
    def spec(self) -> str:
        """What Tradovate calls ``accountSpec`` on order payloads."""
        return self.name


@dataclass
class CashBalance:
    """Tradovate's cash balance snapshot.

    The first three fields are always numbers. The rest are None when
    Tradovate did not send them, so an absent margin figure is not mistaken
    for a margin of zero.
    """

    total_cash: float = 0.0
    open_pnl: float = 0.0
    realized_pnl: float = 0.0
    net_liq: "float | None" = None
    total_pnl: "float | None" = None
    initial_margin: "float | None" = None
    maintenance_margin: "float | None" = None
    week_realized_pnl: "float | None" = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "CashBalance":
        d = d or {}
        return cls(
            total_cash=_f(d.get('totalCashValue')),
            open_pnl=_f(d.get('openPnL')),
            realized_pnl=_f(d.get('realizedPnL')),
            net_liq=_opt(d.get('netLiq')),
            total_pnl=_opt(d.get('totalPnL')),
            initial_margin=_opt(d.get('initialMargin')),
            maintenance_margin=_opt(d.get('maintenanceMargin')),
            week_realized_pnl=_opt(d.get('weekRealizedPnL')),
            raw=d,
        )


@dataclass
class Contract:
    id: int
    name: str
    contract_maturity_id: "int | None" = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "Contract":
        return cls(
            id=d['id'],
            name=d.get('name') or str(d['id']),
            contract_maturity_id=d.get('contractMaturityId'),
            raw=d,
        )


@dataclass
class ContractMaturity:
    """When a contract expires, and whether it is the front month."""

    id: int
    product_id: "int | None" = None
    expiration_month: "int | None" = None
    expiration_date: str = ''
    first_intent_date: str = ''
    is_front: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "ContractMaturity":
        return cls(
            id=d.get('id'),
            product_id=d.get('productId'),
            expiration_month=d.get('expirationMonth'),
            expiration_date=d.get('expirationDate') or '',
            first_intent_date=d.get('firstIntentDate') or '',
            is_front=bool(d.get('isFront')),
            raw=d,
        )


@dataclass
class MarginSnapshot:
    """Tradovate's margin figures, as it reports them."""

    id: int
    timestamp: str = ''
    initial_margin: "float | None" = None
    maintenance_margin: "float | None" = None
    auto_liq_level: "float | None" = None
    liq_only_level: "float | None" = None
    total_used_margin: "float | None" = None
    full_initial_margin: "float | None" = None
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "MarginSnapshot":
        return cls(
            id=d.get('id'),
            timestamp=d.get('timestamp') or '',
            initial_margin=_opt(d.get('initialMargin')),
            maintenance_margin=_opt(d.get('maintenanceMargin')),
            auto_liq_level=_opt(d.get('autoLiqLevel')),
            liq_only_level=_opt(d.get('liqOnlyLevel')),
            total_used_margin=_opt(d.get('totalUsedMargin')),
            full_initial_margin=_opt(d.get('fullInitialMargin')),
            raw=d,
        )


@dataclass
class AutoLiq:
    """The risk limits Tradovate holds for an account (``userAccountAutoLiq``).

    These are the account's own settings, read back. Every one is None when
    it is not set. The full record, including the percentage and alert
    thresholds not named here, is on ``raw``.
    """

    id: int
    changes_locked: bool = False
    daily_loss_auto_liq: "float | None" = None
    weekly_loss_auto_liq: "float | None" = None
    daily_profit_auto_liq: "float | None" = None
    weekly_profit_auto_liq: "float | None" = None
    trailing_max_drawdown: "float | None" = None
    trailing_max_drawdown_limit: "float | None" = None
    flatten_timestamp: str = ''
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "AutoLiq":
        return cls(
            id=d.get('id'),
            changes_locked=bool(d.get('changesLocked')),
            daily_loss_auto_liq=_opt(d.get('dailyLossAutoLiq')),
            weekly_loss_auto_liq=_opt(d.get('weeklyLossAutoLiq')),
            daily_profit_auto_liq=_opt(d.get('dailyProfitAutoLiq')),
            weekly_profit_auto_liq=_opt(d.get('weeklyProfitAutoLiq')),
            trailing_max_drawdown=_opt(d.get('trailingMaxDrawdown')),
            trailing_max_drawdown_limit=_opt(d.get('trailingMaxDrawdownLimit')),
            flatten_timestamp=d.get('flattenTimestamp') or '',
            raw=d,
        )


@dataclass
class OrderStrategy:
    """A running or finished order strategy."""

    id: int
    account_id: "int | None" = None
    contract_id: "int | None" = None
    order_strategy_type_id: "int | None" = None
    action: str = ''
    status: str = ''
    failure_message: str = ''
    params: str = ''
    uuid: str = ''
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "OrderStrategy":
        return cls(
            id=d.get('id'),
            account_id=d.get('accountId'),
            contract_id=d.get('contractId'),
            order_strategy_type_id=d.get('orderStrategyTypeId'),
            action=d.get('action') or '',
            status=d.get('status') or '',
            failure_message=d.get('failureMessage') or '',
            params=d.get('params') or '',
            uuid=d.get('uuid') or '',
            raw=d,
        )

    @property
    def is_active(self) -> bool:
        return self.status == 'ActiveStrategy'


@dataclass
class Position:
    id: int
    account_id: int
    contract_id: int
    net_pos: int = 0
    net_price: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "Position":
        return cls(
            id=d.get('id'),
            account_id=d.get('accountId'),
            contract_id=d.get('contractId'),
            net_pos=int(d.get('netPos') or 0),
            net_price=_f(d.get('netPrice')),
            raw=d,
        )

    @property
    def is_flat(self) -> bool:
        return self.net_pos == 0


@dataclass
class PositionView:
    """A position with its contract name resolved."""

    symbol: str
    net_pos: int
    net_price: float
    contract_id: int


@dataclass
class Fill:
    id: int
    order_id: int
    contract_id: int
    action: str = ''
    qty: int = 0
    price: float = 0.0
    timestamp: str = ''
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "Fill":
        return cls(
            id=d.get('id'),
            order_id=d.get('orderId'),
            contract_id=d.get('contractId'),
            action=d.get('action') or '',
            qty=int(d.get('qty') or 0),
            price=_f(d.get('price')),
            timestamp=d.get('timestamp') or '',
            raw=d,
        )


@dataclass
class FillFee:
    """What a fill actually cost, broken out the way Tradovate reports it.

    Seven separate charges, each in its own currency field. ``total`` adds the
    ones that are present; it assumes they share a currency, which they do on
    a US futures account. If you trade a venue where they do not, read the
    components and their ``*_currency_id`` from ``raw``.

    This is the number worth having. A rate card is an estimate; this is the
    charge.
    """

    id: int
    clearing_fee: float = 0.0
    exchange_fee: float = 0.0
    nfa_fee: float = 0.0
    brokerage_fee: float = 0.0
    ip_fee: float = 0.0
    commission: float = 0.0
    order_routing_fee: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    #: The charge fields, in Tradovate's own order.
    COMPONENTS = (
        'clearing_fee',
        'exchange_fee',
        'nfa_fee',
        'brokerage_fee',
        'ip_fee',
        'commission',
        'order_routing_fee',
    )

    @classmethod
    def from_api(cls, d) -> 'FillFee':
        return cls(
            id=d.get('id'),
            clearing_fee=_f(d.get('clearingFee')),
            exchange_fee=_f(d.get('exchangeFee')),
            nfa_fee=_f(d.get('nfaFee')),
            brokerage_fee=_f(d.get('brokerageFee')),
            ip_fee=_f(d.get('ipFee')),
            commission=_f(d.get('commission')),
            order_routing_fee=_f(d.get('orderRoutingFee')),
            raw=d,
        )

    @property
    def total(self) -> float:
        """Every charge on this fill, added up."""
        return sum(getattr(self, name) for name in self.COMPONENTS)

    def breakdown(self) -> dict:
        """The charges that are actually non-zero."""
        return {
            name: getattr(self, name) for name in self.COMPONENTS if getattr(self, name)
        }


@dataclass
class FillPair:
    """A buy matched against a sell: one round trip, as Tradovate paired it.

    ``points`` is the move, which this library can compute from the two
    prices. Money needs the contract's value per point, which lives on
    ``Product`` -- pass it to ``gross()`` rather than having this library
    guess a multiplier.
    """

    id: int
    position_id: int = 0
    buy_fill_id: int = 0
    sell_fill_id: int = 0
    qty: int = 0
    buy_price: float = 0.0
    sell_price: float = 0.0
    active: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> 'FillPair':
        return cls(
            id=d.get('id'),
            position_id=d.get('positionId') or 0,
            buy_fill_id=d.get('buyFillId') or 0,
            sell_fill_id=d.get('sellFillId') or 0,
            qty=int(d.get('qty') or 0),
            buy_price=_f(d.get('buyPrice')),
            sell_price=_f(d.get('sellPrice')),
            active=bool(d.get('active', False)),
            raw=d,
        )

    @property
    def points(self) -> float:
        """The move on this round trip, in points, times the quantity."""
        return (self.sell_price - self.buy_price) * self.qty

    def gross(self, value_per_point) -> float:
        """Points turned into money, using the contract's own multiplier.

        Get ``value_per_point`` from ``Product.value_per_point`` -- the
        client's ``find_product()`` or ``product()`` will fetch it. This
        library will not supply a multiplier of its own.
        """
        if value_per_point is None:
            raise ValueError(
                'value_per_point is required; read it from the product '
                '(client.find_product(name).value_per_point)'
            )
        return self.points * float(value_per_point)

    def net(self, value_per_point, fees) -> float:
        """Gross less the fees on both legs.

        ``fees`` is whatever you have: a number, or the ``FillFee`` objects
        for the two legs.
        """
        if isinstance(fees, (int, float)):
            cost = float(fees)
        else:
            cost = sum(getattr(f, 'total', 0.0) for f in fees)
        return self.gross(value_per_point) - cost


@dataclass
class Product:
    """A tradable product, and the two numbers that make its prices mean money."""

    id: int
    name: str
    product_type: str = ''
    value_per_point: float = 0.0
    tick_size: float = 0.0
    currency_id: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> 'Product':
        return cls(
            id=d.get('id'),
            name=d.get('name') or '',
            product_type=d.get('productType') or '',
            value_per_point=_f(d.get('valuePerPoint')),
            tick_size=_f(d.get('tickSize')),
            currency_id=d.get('currencyId') or 0,
            raw=d,
        )

    @property
    def tick_value(self) -> float:
        """What one tick is worth."""
        return self.tick_size * self.value_per_point


@dataclass
class Order:
    id: int
    account_id: int = 0
    contract_id: int = 0
    action: str = ''
    order_type: str = ''
    status: str = ''
    qty: float = 0.0
    price: "float | None" = None
    stop_price: "float | None" = None
    time_in_force: str = ''
    client_order_id: "str | None" = None
    text: str = ''
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d) -> "Order":
        return cls(
            id=d.get('id'),
            account_id=d.get('accountId'),
            contract_id=d.get('contractId'),
            action=d.get('action') or '',
            order_type=d.get('orderType') or '',
            status=d.get('ordStatus') or '',
            qty=_f(d.get('orderQty')),
            price=_opt(d.get('price')),
            stop_price=_opt(d.get('stopPrice')),
            time_in_force=d.get('timeInForce') or '',
            client_order_id=d.get('clOrdId') or None,
            text=d.get('text') or '',
            raw=d,
        )

    @property
    def side(self) -> str:
        """An alias for ``action``.

        Tradovate calls it ``action``; most other venues call it side. Both
        names work here so a caller moving between them is not caught out.
        """
        return self.action

    @property
    def is_working(self) -> bool:
        return self.status in ('Working', 'PendingNew', 'Suspended')


@dataclass
class Snapshot:
    """One flat view of an account, for a dashboard poller."""

    account: Account
    cash: CashBalance
    positions: dict = field(default_factory=dict)

    @property
    def is_flat(self) -> bool:
        return not any(p.net_pos for p in self.positions.values())


def _opt(value) -> "float | None":
    """A float, or None when the field was absent rather than zero."""
    if value is None or value == '':
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _f(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
