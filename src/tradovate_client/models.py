"""Typed views over Tradovate's JSON.

Each model keeps the raw payload on ``.raw`` so nothing is ever lost, and
tolerates fields Tradovate adds later.
"""

from dataclasses import dataclass, field

__all__ = ['Account', 'CashBalance', 'Contract', 'Position', 'Fill', 'Order',
           'Snapshot', 'PositionView']


@dataclass
class Account:
    id: int
    name: str
    account_type: str = ''
    active: bool = True
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d):
        return cls(id=d['id'], name=d.get('name') or str(d['id']),
                   account_type=d.get('accountType') or '',
                   active=bool(d.get('active', True)), raw=d)

    @property
    def spec(self) -> str:
        """What Tradovate calls ``accountSpec`` on order payloads."""
        return self.name


@dataclass
class CashBalance:
    total_cash: float = 0.0
    open_pnl: float = 0.0
    realized_pnl: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d):
        d = d or {}
        return cls(total_cash=_f(d.get('totalCashValue')),
                   open_pnl=_f(d.get('openPnL')),
                   realized_pnl=_f(d.get('realizedPnL')), raw=d)


@dataclass
class Contract:
    id: int
    name: str
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d):
        return cls(id=d['id'], name=d.get('name') or str(d['id']), raw=d)


@dataclass
class Position:
    id: int
    account_id: int
    contract_id: int
    net_pos: int = 0
    net_price: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d):
        return cls(id=d.get('id'), account_id=d.get('accountId'),
                   contract_id=d.get('contractId'),
                   net_pos=int(d.get('netPos') or 0),
                   net_price=_f(d.get('netPrice')), raw=d)

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
    def from_api(cls, d):
        return cls(id=d.get('id'), order_id=d.get('orderId'),
                   contract_id=d.get('contractId'),
                   action=d.get('action') or '', qty=int(d.get('qty') or 0),
                   price=_f(d.get('price')), timestamp=d.get('timestamp') or '',
                   raw=d)


@dataclass
class Order:
    id: int
    account_id: int = 0
    contract_id: int = 0
    action: str = ''
    order_type: str = ''
    status: str = ''
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, d):
        return cls(id=d.get('id'), account_id=d.get('accountId'),
                   contract_id=d.get('contractId'),
                   action=d.get('action') or '',
                   order_type=d.get('orderType') or '',
                   status=d.get('ordStatus') or '', raw=d)

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


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
