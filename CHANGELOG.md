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
- Retries cannot duplicate an order. A timed-out or reset write is never
  repeated, because the request may already have been accepted; the caller
  gets `TransportError(ambiguous=True)` instead. Reads, and any 429, are
  retried.
- The cached token is created with mode 0600 from its first byte, inside a
  directory created 0700 -- not chmod'ed after the fact.

Not yet included: the websocket client, market data.
