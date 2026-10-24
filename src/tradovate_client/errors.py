"""Exceptions.

Every failure raises. No call in this library returns an error dictionary that
a caller might mistake for a successful result -- which is the single most
common way an automated trading script places an order it did not mean to.
"""

__all__ = [
    'TradovateError', 'ConfigError', 'TransportError', 'AuthError',
    'ThrottledError', 'RequestError', 'ServerError', 'NotFoundError',
]


class TradovateError(Exception):
    """Base class for everything this library raises."""

    def __init__(self, message, *, endpoint=None, status=None, body=None):
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.status = status
        self.body = body

    def __str__(self):
        bits = [self.message]
        if self.endpoint:
            bits.append('endpoint=%s' % self.endpoint)
        if self.status is not None:
            bits.append('status=%s' % self.status)
        if self.body not in (None, '', {}):
            bits.append('body=%r' % (self.body,))
        return ' | '.join(bits)


class ConfigError(TradovateError):
    """Credentials or settings are missing or malformed. No request was made."""


class TransportError(TradovateError):
    """The request never completed -- DNS, TLS, timeout, connection reset."""


class AuthError(TradovateError):
    """Authentication or authorisation failed (HTTP 401 or 403)."""


class ThrottledError(TradovateError):
    """HTTP 429. Tradovate rate-limits authentication especially hard.

    ``retry_after`` is the server's own advice in seconds when it gives one.
    """

    def __init__(self, message, *, retry_after=None, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


class NotFoundError(TradovateError):
    """HTTP 404."""


class RequestError(TradovateError):
    """Any other 4xx -- the request itself was rejected."""


class ServerError(TradovateError):
    """Any 5xx."""


def raise_for_status(status, endpoint, body, headers=None):
    """Map an HTTP status onto the right exception. Never returns on failure."""
    if 200 <= status < 300:
        return
    headers = headers or {}
    if status in (401, 403):
        raise AuthError('authentication rejected', endpoint=endpoint,
                        status=status, body=body)
    if status == 404:
        raise NotFoundError('no such endpoint or object', endpoint=endpoint,
                            status=status, body=body)
    if status == 429:
        after = headers.get('Retry-After')
        try:
            after = float(after) if after is not None else None
        except (TypeError, ValueError):
            after = None
        raise ThrottledError('rate limited by Tradovate', retry_after=after,
                             endpoint=endpoint, status=status, body=body)
    if 400 <= status < 500:
        raise RequestError('request rejected', endpoint=endpoint,
                           status=status, body=body)
    raise ServerError('Tradovate server error', endpoint=endpoint,
                      status=status, body=body)
