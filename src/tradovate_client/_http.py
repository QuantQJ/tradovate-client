"""The transport layer: JSON over HTTPS, standard library only.

No third-party dependency. One place where every request is built, every
response is decoded, and every failure becomes a typed exception.

On retries, and why they are not automatic
------------------------------------------
A timeout is not a failure -- it is an absence of news. If
``POST /order/placeorder`` times out, the order may already be working at
Tradovate, and resending it places a second one. So a request is only
retried when it is safe to resend:

* ``GET`` and ``HEAD`` carry no side effect and are always retried.
* Anything else is retried only when the failure proves the request never
  left this machine -- DNS failure, or the connection being refused.
* HTTP 429 is retried for any method, because a rate-limited request was
  rejected rather than processed.

When a write fails ambiguously the caller gets a ``TransportError`` with
``ambiguous=True``, meaning: the order state is unknown, go and look before
you send anything else. Passing ``client_order_id=`` on an order is the way
to make a deliberate resend safe.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import ThrottledError, TransportError, raise_for_status

DEFAULT_TIMEOUT = 20.0
DEFAULT_RETRIES = 2
USER_AGENT = 'tradovate-client/0.1.0 (+https://github.com/QuantQJ/tradovate-client)'

#: Methods with no side effect, safe to repeat.
IDEMPOTENT_METHODS = frozenset({'GET', 'HEAD'})

#: Failures that prove the request never reached the server.
NEVER_SENT = (socket.gaierror, ConnectionRefusedError)


def _encode(body) -> bytes | None:
    if body is None:
        return None
    return json.dumps(_drop_none(body)).encode('utf-8')


def _drop_none(value) -> Any:
    """Strip keys whose value is None, recursively.

    Tradovate's order models mark most fields optional. Sending an explicit
    null where the caller simply did not set a field is a good way to get a
    confusing rejection, so unset fields are omitted entirely.
    """
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


def never_sent(exc) -> bool:
    """True when ``exc`` proves the request did not reach the server."""
    reason = getattr(exc, 'reason', None)
    return isinstance(exc, NEVER_SENT) or isinstance(reason, NEVER_SENT)


def may_resend(method, exc) -> bool:
    """Whether a failed request of this method can be safely repeated."""
    if method.upper() in IDEMPOTENT_METHODS:
        return True
    return never_sent(exc)


class Transport:
    """Minimal JSON client for one environment's REST base URL."""

    def __init__(
        self,
        base_url,
        *,
        timeout=DEFAULT_TIMEOUT,
        retries=DEFAULT_RETRIES,
        sleep=time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.retries = max(0, int(retries))
        self._sleep = sleep

    def request(self, method, path, *, token=None, body=None, params=None) -> Any:
        if params:
            path = '%s?%s' % (
                path,
                urllib.parse.urlencode(
                    {k: v for k, v in params.items() if v is not None}
                ),
            )
        url = self.base_url + path
        headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'User-Agent': USER_AGENT,
        }
        if token:
            headers['Authorization'] = 'Bearer ' + token

        attempt = 0
        while True:
            attempt += 1
            try:
                req = urllib.request.Request(
                    url, data=_encode(body), headers=headers, method=method
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                    raise_for_status(resp.status, path, None, dict(resp.headers))
                    return self._decode(raw, path)
            except urllib.error.HTTPError as exc:
                # The server answered, so nothing is ambiguous: a 429 was
                # declined rather than processed and may be sent again.
                raw = b''
                try:
                    raw = exc.read()
                except Exception:
                    pass
                parsed = self._safe_json(raw)
                try:
                    raise_for_status(exc.code, path, parsed, dict(exc.headers or {}))
                except ThrottledError as throttled:
                    if attempt <= self.retries:
                        self._sleep(throttled.retry_after or 2.0 * attempt)
                        continue
                    raise
                raise
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                if attempt <= self.retries and may_resend(method, exc):
                    self._sleep(0.5 * attempt)
                    continue
                raise self._transport_error(method, path, exc) from exc

    @staticmethod
    def _transport_error(method, path, exc) -> TransportError:
        reason = getattr(exc, 'reason', exc)
        if may_resend(method, exc):
            return TransportError('request failed: %s' % reason, endpoint=path)
        return TransportError(
            'request failed after it was sent: %s -- this %s may or may not '
            'have been accepted by Tradovate. Check order and position state '
            'before sending anything else.' % (reason, method.upper()),
            endpoint=path,
            ambiguous=True,
        )

    @staticmethod
    def _safe_json(raw) -> Any:
        try:
            return json.loads(raw.decode('utf-8'))
        except Exception:
            return raw.decode('utf-8', 'replace')[:500] or None

    @staticmethod
    def _decode(raw, path) -> Any:
        if not raw:
            return None
        try:
            return json.loads(raw.decode('utf-8'))
        except ValueError as exc:
            raise TransportError(
                'response was not JSON: %s' % exc,
                endpoint=path,
                body=raw.decode('utf-8', 'replace')[:200],
            ) from exc
