# Changelog

## 0.2.0

Market data, sockets that stay up, and penalties handled everywhere.

- `MarketDataSocket`: quotes, depth of market, volume histograms, bars and
  ticks from Tradovate's market data socket.
  - `subscribe_quote`, `subscribe_dom`, `subscribe_histogram`, each returning
    a handle with `cancel()`. Messages are routed by contract id; two handlers
    on one contract share a subscription.
  - `Quote` exposes bid, ask, last, sizes, volume, high, low, open, settlement
    and open interest. An entry Tradovate left out is `None`, not zero.
  - `chart()` for a live chart, and `bars()` / `ticks()` as one blocking call
    each. Bars are held by timestamp, so the forming bar is replaced rather
    than repeated. Tick packets are decoded the way Tradovate documents:
    `timestamp = bt + t`, `price = (bp + p) * ts`.
  - `history()` pages through Tradovate's variable cap on one chart request,
    following the procedure in their FAQ.
  - Authorizes with `mdAccessToken`, which the token cache now keeps beside
    the access token; falls back to the access token when there is none.
  - A chart packet that arrives before the caller has been handed its
    subscription ids is held and claimed, not dropped.
- Both sockets reconnect. A fresh token, a new connection, the authorize
  handshake, then a new snapshot (user socket) or the subscriptions sent again
  (market data). `disconnect`, `reconnect` and `error` events;
  `reconnect_delays=` and `max_reconnects=`. A request in flight when the
  socket died is never repeated. Reconnection stops on a captcha or missing
  credentials instead of extending a penalty.
- A new snapshot replaces the streamed state rather than merging into it, and
  a fill that happened while the socket was down is delivered as a `fill`
  event.
- Handlers run on their own thread, so a handler can send a request -- place
  an order from a fill -- and a slow handler cannot hold up the heartbeat.
  Before this, a request made from inside a handler waited on a reply that
  nothing was left to read.
- The user socket checks `failureReason` on order replies and raises
  `OrderRejected`, as REST already did. Before this a refused order came back
  from the socket as an ordinary reply.
- The user socket streams orders: `orders()`, `working_orders()`, `order()`,
  `fills_for_order()` and `wait_for_order()`. It gained `modify`, `place_oso`,
  `place_oco`, `bracketed_market`, `cancel_working` and order strategies, all
  built by the same functions REST uses.
- The user socket keeps only the account it is bound to. The snapshot covers
  the whole login, and another account's positions, orders and cash were
  previously folded into the same state.
- A time penalty is handled on every endpoint, not only on login: a penalised
  `POST` is waited out and sent again with the ticket, and so is any socket
  request. A penalised `GET` raises `TimePenalty`. Before this a penalty reply
  to an ordinary call was returned as if it were the result.
- Order strategies: `bracket_strategy`, `start_order_strategy`,
  `interrupt_order_strategy`, `order_strategies`.
- Contracts: `suggest_contracts`, `roll_contract`, `contract_maturity`.
- `CashBalance` carries net liquidation value, total P&L, initial and
  maintenance margin and week realized P&L. `margin_snapshots()` and
  `auto_liqs()` read margin and the account's risk limits.

## 0.1.0 — unreleased

First cut.

- Authentication with token renewal: `/auth/renewaccesstoken` near expiry,
  full `/auth/accesstokenrequest` only when there is no usable token. Tokens
  cached per environment, mode 0600.
- `Env` is a required argument with no default. Demo and live are separate
  hosts with separate token files.
- No default credential path. The client opens no file the caller did not
  name: `credentials=`, then `credential_path=`, then the file named by
  `$TRADOVATE_CREDENTIALS`, then the seven `TRADOVATE_*` variables, then
  `ConfigError` listing those options.
- Reads: accounts, cash balance, positions, contracts, fills, fills for one
  order, orders, working orders, and a one-call `snapshot()`.
- Every order type Tradovate publishes: Market, Limit, Stop, StopLimit,
  TrailingStop, TrailingStopLimit, MIT, QTS.
- Bracket orders: `place_oso`, `place_oco`, and `bracketed_market`.
- Modify, cancel, cancel all working orders, liquidate, flatten everything.
- Typed exceptions. Nothing returns an error dictionary.
- No dependencies for the REST client.
- Tradovate's authentication time penalty is handled. It arrives as HTTP 200
  with no token and `p-ticket` / `p-time` / `p-captcha` instead, so a client
  that reads the status alone sees a success with a missing token and
  typically retries -- which extends the penalty. This waits the `p-time`
  given, sends the ticket back with the retry, caps a single wait, gives up
  with `TimePenalty` after a bounded number of them, and raises
  `CaptchaRequired` without retrying when a captcha is asked for. Reference:
  `tradovate/example-api-js`, `tutorial/Access/EX-3-Time-Penalty`.
  `errorText` and a blocking `userStatus` are surfaced as well.
- `TradovateSocket`: the persistent websocket. Orders go over a connection
  that is already open, fill prices arrive as events with the price Tradovate
  reported, and one `user/syncrequest` replaces polling. It implements the
  frame protocol properly -- `o`/`h`/`a`/`c`, the authorize handshake, and the
  keep-alive every 2 seconds that the server's 2.5-second idle timeout
  requires. Full auth stays on REST so token renewal is inherited. Order
  bodies come from the same builder as the REST path, with unset fields
  stripped the same way, so the two are byte-for-byte identical. A request
  whose socket dies mid-flight raises with `ambiguous=True` rather than
  hanging or being retried.
- Fill fees, fill pairs and products, so cost and P&L come from the broker's
  books rather than arithmetic here:
  - `FillFee` reads all seven charges Tradovate reports per fill -- clearing,
    exchange, NFA, brokerage, IP, commission and order routing -- with
    `total` and `breakdown()`. Commission alone is a fraction of the cost.
  - `cost_of_order()` walks an order to its fills and each fill to its
    charges. A fill with no reported fees contributes nothing, not a guess.
  - `FillPair` is a buy matched to a sell by the broker. `points` is computed
    here; money needs the contract's multiplier, so `gross()` requires it
    explicitly.
  - `Product` carries `value_per_point` and `tick_size`, which is where that
    multiplier comes from. `round_trips()` fetches it, or takes yours, or
    returns `gross=None` rather than inventing one.
- Retries cannot duplicate an order. A timed-out or reset write is never
  repeated, because the request may already have been accepted; the caller
  gets `TransportError(ambiguous=True)` instead. Reads, and any 429, are
  retried.
- The cached token is created with mode 0600 from its first byte, inside a
  directory created 0700 -- not chmod'ed after the fact.

