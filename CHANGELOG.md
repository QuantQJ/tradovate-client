# Changelog

## 0.1.0 — unreleased

First cut.

- Authentication with token renewal: `/auth/renewaccesstoken` near expiry,
  full `/auth/accesstokenrequest` only when there is no usable token. Tokens
  cached per environment, mode 0600.
- `Env` is a required argument with no default. Demo and live are separate
  hosts with separate token files.
- Reads: accounts, cash balance, positions, contracts, fills, fills for one
  order, orders, working orders, and a one-call `snapshot()`.
- Every order type Tradovate publishes: Market, Limit, Stop, StopLimit,
  TrailingStop, TrailingStopLimit, MIT, QTS.
- Bracket orders: `place_oso`, `place_oco`, and `bracketed_market`.
- Modify, cancel, cancel all working orders, liquidate, flatten everything.
- Typed exceptions. Nothing returns an error dictionary.
- No dependencies for the REST client.

Not yet included: the websocket client, market data.
