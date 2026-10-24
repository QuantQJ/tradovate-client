# tradovate-client

A Tradovate API client for Python.

```python
from tradovate_client import TradovateClient, Env, Action

client = TradovateClient(Env.DEMO, credential_path="~/my-tradovate-key.json")

print(client.snapshot())
client.limit(Action.BUY, "MNQZ6", 1, price=20150.25)
```

No dependencies. The REST client is standard library only.

## Why this one

There are a few Tradovate wrappers on PyPI and GitHub. Three things here are
not in any of them, and all three are the difference between a script that
works once and one that runs unattended.

**Tokens are renewed, not re-requested.** Tradovate rate-limits
`/auth/accesstokenrequest` hard — the demo environment especially — so a client
that authenticates from scratch on every run eventually gets locked out. This
one calls `/auth/renewaccesstoken` once a cached token is within five minutes
of expiring, and only performs a full authentication when it has nothing
usable. Tokens are cached per environment at mode 0600.

**Demo and live cannot be confused.** They are different hosts with different
credentials, so the environment is a property of the client, not a flag on each
call. `env` is required and has no default. A client built for demo has no code
path that reaches the live host, and the two keep separate token files.

**Failures raise.** No call returns a dictionary with an `error` key in it that
a caller might mistake for a result. That mistake places orders.

**There is no default credential path.** The client opens no file you did not
name, so it cannot quietly pick up a key file that happens to be sitting in a
home directory.

## Install

```sh
pip install tradovate-client              # REST only, no dependencies
pip install "tradovate-client[socket]"    # adds websocket-client
```

## Credentials

There is no default path. You tell the client where your credentials are, or
you hand them over directly. Four ways, tried in this order:

```python
# 1. pass them in
from tradovate_client import Credentials

client = TradovateClient(
    Env.DEMO,
    credentials=Credentials(
        name="...",
        password="...",
        appId="...",
        appVersion="1.0",
        deviceId="...",
        cid=0,
        sec="...",
    ),
)

# 2. name a file
client = TradovateClient(Env.DEMO, credential_path="/secure/place/key.json")
```

```sh
# 3. name that file in the environment
export TRADOVATE_CREDENTIALS=/secure/place/key.json

# 4. or supply the fields themselves
export TRADOVATE_NAME=... TRADOVATE_PASSWORD=... TRADOVATE_APP_ID=... \
       TRADOVATE_APP_VERSION=1.0 TRADOVATE_DEVICE_ID=... \
       TRADOVATE_CID=... TRADOVATE_SEC=...
```

With none of those the client raises `ConfigError` and lists the options. It
never falls back to a path of its own choosing.

The file holds the seven fields Tradovate's auth endpoint wants:

```json
{
  "name": "your-username",
  "password": "your-password",
  "appId": "your-app-name",
  "appVersion": "1.0",
  "deviceId": "a-stable-uuid-for-this-machine",
  "cid": 0,
  "sec": "your-api-secret"
}
```

Give it mode 600 — the client refuses to read a credential file that other
users can read.

`cid` and `sec` come from Tradovate's API access page. `deviceId` should stay
the same for a given machine.

## Reading the account

```python
client = TradovateClient(Env.DEMO, account="DEMO1234")

client.accounts()  # [Account(...), ...]
client.cash_balance()  # CashBalance(total_cash=..., open_pnl=...)
client.positions()  # this account's positions only
client.orders()  # every order
client.working_orders()  # just the live ones
client.fills_for_order(order_id)  # what it actually traded at
client.find_contract("MNQZ6")  # Contract(id=..., name="MNQZ6")

snap = client.snapshot()  # one object for a dashboard poller
snap.cash.open_pnl
snap.positions["MNQZ6"].net_pos
snap.is_flat
```

`snapshot()` resolves contract names only for positions actually held, so it
does not pull the full contract list on every poll.

If the login has more than one account and you did not say which, the client
raises instead of taking the first one.

## Orders

Every type Tradovate publishes:

```python
client.market(Action.BUY, "MNQZ6", 2)
client.limit(Action.BUY, "MNQZ6", 2, price=20150.25)
client.stop(Action.SELL, "MNQZ6", 2, stop_price=20100.00)
client.stop_limit(Action.SELL, "MNQZ6", 2, price=20095.00, stop_price=20100.00)
client.trailing_stop(Action.SELL, "MNQZ6", 2, stop_price=20100.00)
client.trailing_stop_limit(Action.SELL, "MNQZ6", 2, price=20095.0, stop_price=20100.0)
client.market_if_touched(Action.BUY, "MNQZ6", 2, price=20050.00)
```

Any of them takes `time_in_force`, `expire_time`, `max_show`,
`peg_difference`, `text`, `activation_time`, `client_order_id`,
`custom_tag_50`.

```python
from tradovate_client import TimeInForce

client.limit(Action.BUY, "MNQZ6", 1, price=20150.0, time_in_force=TimeInForce.GTC)
```

### Brackets

An entry with a target and a stop attached, in one call:

```python
client.bracketed_market(
    Action.BUY,
    "MNQZ6",
    1,
    take_profit=20200.00,
    stop_loss=20050.00,
)
```

Both prices are yours. This library does not calculate a stop distance or a
target and never will — that is a trading decision, not a client's job.

For full control, build the legs:

```python
from tradovate_client import OrderType

target = client.bracket_leg(Action.SELL, OrderType.LIMIT, price=20200.0)
stop = client.bracket_leg(Action.SELL, OrderType.STOP, stop_price=20050.0)

client.place_oso(
    action=Action.BUY,
    symbol="MNQZ6",
    qty=1,
    order_type=OrderType.MARKET,
    bracket1=target,
    bracket2=stop,
)

client.place_oco(
    action=Action.SELL,
    symbol="MNQZ6",
    qty=1,
    order_type=OrderType.LIMIT,
    price=20200.0,
    other=stop,
)
```

### Changing and cancelling

## What a trade cost, and what it made

Commission is not the cost. Tradovate reports seven separate charges per fill
and this reads all of them:

```python
client.cost_of_order(4242)
# {'total': 1.22, 'fills': 2, 'fees': 2,
#  'breakdown': {'clearing_fee': 0.14, 'exchange_fee': 0.74,
#                'nfa_fee': 0.04, 'commission': 0.30},
#  'fill_ids': [11, 12]}
```

Round trips come matched by the broker, not reassembled by you:

```python
client.round_trips("MNQ")
# [{'id': 7, 'qty': 2, 'buy_price': 20100.0, 'sell_price': 20110.0,
#   'points': 20.0, 'gross': 40.0, 'active': False}]
```

The dollar figure uses the contract's own `valuePerPoint`, fetched from
Tradovate. Supply your own with `value_per_point=`, or pass neither and
`gross` comes back `None` — this library will not invent a multiplier.

```python
pair = client.fill_pairs()[0]
pair.points  # 20.0
pair.gross(2.0)  # 40.0
pair.net(2.0, client.fill_fees())  # gross less both legs' charges

product = client.find_product("MNQ")
product.value_per_point, product.tick_size, product.tick_value
```

## Changing and cancelling

```python
client.modify(order_id, price=20160.00)
client.cancel(order_id)
client.cancel_working()  # -> [ids it cancelled]
client.liquidate(contract_id)
client.flatten_all()  # -> {"cancelled": [...], "liquidated": [...]}
```

`flatten_all()` on a live client closes real positions.

## Errors

```python
from tradovate_client import (
    TradovateError,
    AuthError,
    ThrottledError,
    RequestError,
    ServerError,
    TransportError,
    ConfigError,
    NotFoundError,
)

try:
    client.market(Action.BUY, "MNQZ6", 1)
except ThrottledError as exc:
    wait = exc.retry_after  # the server's own advice, when it gives one
except RequestError as exc:
    print(exc.status, exc.endpoint, exc.body)
```

Every exception carries the endpoint, the status and the response body.

### Retries never duplicate an order

A timeout is not a rejection. If `POST /order/placeorder` times out, the order
may already be working at Tradovate, so repeating it would place a second one.
This client therefore retries only when a retry is safe:

- `GET` carries no side effect and is always retried.
- A write is retried only when the failure proves nothing was sent — DNS
  failure, or the connection refused.
- HTTP 429 is retried for any method, because a rate-limited request was
  declined rather than processed. `Retry-After` is honoured.

When a write fails ambiguously you get a `TransportError` with
`ambiguous=True`, which means the order state is unknown:

```python
except TransportError as exc:
    if exc.ambiguous:
        # The order may be working. Read before you write.
        for order in client.working_orders():
            ...
```

Pass `client_order_id=` on an order if you want a deliberate resend to be safe
at Tradovate's end.

## Order types and their prices

| type | needs |
| --- | --- |
| `Market` | nothing |
| `Limit` | `price` |
| `Stop` | `stop_price` |
| `StopLimit` | `price` and `stop_price` |
| `TrailingStop` | `stop_price` |
| `TrailingStopLimit` | `price` and `stop_price` |
| `MIT` | `price` |
| `QTS` | — |

The client checks these before sending and raises `ConfigError` on a mismatch.
Pass `strict=False` to skip the check and let the API decide.

## Provenance

The enumerations and payload field names in this library are copied from
Tradovate's own published material, not inferred:

- `OrderType` — `tradovate/example-api-trading-strategy`,
  `utils/orderType.js`
- `Action`, `TimeInForce`, and every order field name —
  `tradovate/example-api-csharp-trading`, `services-api/docs/`
  (`PlaceOSO.md`, `PlaceOCO.md`, `ModifyOrder.md`, `CancelOrder.md`,
  `LiquidatePosition.md`, `RestrainedOrderVersion.md`)

If Tradovate changes a value, `tests/test_enums.py` is where it shows up.

## Tests

```sh
pip install -e ".[dev]"
pytest
```

They use a fake transport and a fake clock. No network, no credentials, no
broker account.

## Not here yet

- The websocket client — persistent socket, heartbeat, real-time fill events.
- Market data.

## Disclaimer

Not affiliated with, endorsed by, or supported by Tradovate. Trading futures
involves risk of loss. This is software, not advice.

## Licence

MIT.
