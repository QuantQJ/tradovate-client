# Changelog

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

Not yet included: the websocket client, market data.
