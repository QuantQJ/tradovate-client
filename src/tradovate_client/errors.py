"""Exceptions.

Every failure raises. No call in this library returns an error dictionary that
a caller might mistake for a successful result -- which is the single most
common way an automated trading script places an order it did not mean to.
"""

#: Tradovate reports an order's fate in ``failureReason`` on an HTTP 200.
#: ``Success`` is one of the documented values, so the field being present
#: does not mean the order failed -- only a value other than ``Success`` does.
#: The full set is in Tradovate's own PlaceOrderResult model.
SUCCESS_REASON = 'Success'

#: Field names Tradovate uses to signal an authentication time penalty.
#: A response carrying ``p-ticket`` is a throttle, not a success, even though
#: it arrives as HTTP 200.
PENALTY_TICKET = 'p-ticket'
PENALTY_TIME = 'p-time'
PENALTY_CAPTCHA = 'p-captcha'

__all__ = [
    'SUCCESS_REASON',
    'SENSITIVE_KEYS',
    'redact',
    'PENALTY_TICKET',
    'PENALTY_TIME',
    'PENALTY_CAPTCHA',
    'TradovateError',
    'ConfigError',
    'TransportError',
    'AuthError',
    'ThrottledError',
    'RequestError',
    'ServerError',
    'ServerError',
    'OrderRejected',
    'raise_for_result',
    'TimePenalty',
    'CaptchaRequired',
    'penalty_of',
]


#: Keys whose values never belong in an exception, a log line or a traceback.
#: ``accessToken`` and ``mdAccessToken`` are bearer tokens; ``p-ticket`` is a
#: single-use authentication ticket; the rest are credentials.
SENSITIVE_KEYS = frozenset(
    {
        'accessToken',
        'mdAccessToken',
        'password',
        'sec',
        'secret',
        'apiKey',
        'api_key',
        'p-ticket',
        'p-captcha',
    }
)

REDACTED = '[redacted]'


def redact(value):
    """Replace the value of any sensitive key, at any depth.

    Applied to every exception body, so no error path can print a token --
    including the ones that carry an authentication reply or a penalty ticket.
    """
    if isinstance(value, dict):
        return {
            k: (REDACTED if k in SENSITIVE_KEYS else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v) for v in value)
    return value


class TradovateError(Exception):
    """Base class for everything this library raises."""

    def __init__(self, message, *, endpoint=None, status=None, body=None) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.status = status
        # Redacted on the way in, so an exception cannot carry a token even if
        # it is caught, re-raised, pickled or logged by someone else.
        self.body = redact(body)

    def __str__(self) -> str:
        bits = [self.message]
        if self.endpoint:
            bits.append('endpoint=%s' % self.endpoint)
        if self.status is not None:
            bits.append('status=%s' % self.status)
        if self.body not in (None, '', {}):
            bits.append('body=%r' % (self.body,))
        return ' | '.join(bits)


class OrderRejected(TradovateError):
    """Tradovate answered 200 and refused the order inside the payload.

    ``reason`` is its own ``failureReason`` -- one of the documented values
    such as ``MaxPosLimitReached`` or ``InvalidPrice`` -- and ``text`` is the
    free-form ``failureText`` when it sends one.
    """

    def __init__(self, message, *, reason=None, text=None, **kw) -> None:
        super().__init__(message, **kw)
        self.reason = reason
        self.text = text


def raise_for_result(payload, endpoint=None):
    """Raise if an order result carries a real rejection.

    Tradovate answers 200 and puts the outcome in ``failureReason``. A missing
    field, or the documented value ``Success``, both mean accepted; anything
    else is a refusal. Checking only for the field's presence would reject
    successful orders.
    """
    if not isinstance(payload, dict):
        return payload
    reason = payload.get('failureReason')
    if reason in (None, '', SUCCESS_REASON):
        return payload
    text = payload.get('failureText')
    raise OrderRejected(
        'Tradovate refused the order: %s%s' % (reason, ' (%s)' % text if text else ''),
        reason=reason,
        text=text,
        endpoint=endpoint,
        body=payload,
    )


class TimePenalty(TradovateError):
    """Tradovate is throttling authentication and said how long to wait.

    Arrives as HTTP 200 with no access token. ``seconds`` is its own
    ``p-time`` and ``ticket`` its ``p-ticket``, which must be sent back with
    the retry. The client waits and retries on its own; this surfaces only
    when the retry budget is exhausted, and then the right response is to wait
    rather than to try again.
    """

    def __init__(self, message, *, seconds=None, ticket=None, attempts=None, **kw):
        super().__init__(message, **kw)
        self.seconds = seconds
        self.ticket = ticket
        self.attempts = attempts


class CaptchaRequired(TradovateError):
    """Authentication needs a human.

    Tradovate sends ``p-captcha`` when it wants a captcha solved, and their
    own guidance is that a third-party application cannot proceed. This is
    never retried: retrying extends the penalty. Sign in through Tradovate's
    own interface, then let the client authenticate again.
    """


def penalty_of(reply):
    """Return ``(ticket, seconds, captcha)`` if this reply is a penalty.

    Returns ``None`` when it is not. A reply is a penalty when it carries
    ``p-ticket`` -- the HTTP status is 200 either way, which is exactly why
    this has to be checked rather than inferred from the status.
    """
    if not isinstance(reply, dict):
        return None
    ticket = reply.get(PENALTY_TICKET)
    if not ticket:
        return None
    raw = reply.get(PENALTY_TIME)
    try:
        seconds = float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        seconds = 0.0
    return ticket, seconds, reply.get(PENALTY_CAPTCHA)


class ConfigError(TradovateError):
    """Credentials or settings are missing or malformed. No request was made."""


class TransportError(TradovateError):
    """The request did not complete -- DNS, TLS, timeout, connection reset.

    ``ambiguous`` is True when the request may already have been accepted by
    Tradovate, so repeating it could duplicate the effect. For an order that
    means: go and read the order and position state before sending anything
    else. It is False when the failure proves nothing was sent.
    """

    def __init__(self, message, *, ambiguous=False, **kw) -> None:
        super().__init__(message, **kw)
        self.ambiguous = ambiguous


class AuthError(TradovateError):
    """Authentication or authorisation failed (HTTP 401 or 403)."""


class ThrottledError(TradovateError):
    """HTTP 429. Tradovate rate-limits authentication especially hard.

    ``retry_after`` is the server's own advice in seconds when it gives one.
    """

    def __init__(self, message, *, retry_after=None, **kw) -> None:
        super().__init__(message, **kw)
        self.retry_after = retry_after


class NotFoundError(TradovateError):
    """HTTP 404."""


class RequestError(TradovateError):
    """Any other 4xx -- the request itself was rejected."""


class ServerError(TradovateError):
    """Any 5xx."""


def raise_for_status(status, endpoint, body, headers=None) -> None:
    """Map an HTTP status onto the right exception. Never returns on failure."""
    if 200 <= status < 300:
        return
    headers = headers or {}
    if status in (401, 403):
        raise AuthError(
            'authentication rejected', endpoint=endpoint, status=status, body=body
        )
    if status == 404:
        raise NotFoundError(
            'no such endpoint or object', endpoint=endpoint, status=status, body=body
        )
    if status == 429:
        after = headers.get('Retry-After')
        try:
            after = float(after) if after is not None else None
        except (TypeError, ValueError):
            after = None
        raise ThrottledError(
            'rate limited by Tradovate',
            retry_after=after,
            endpoint=endpoint,
            status=status,
            body=body,
        )
    if 400 <= status < 500:
        raise RequestError(
            'request rejected', endpoint=endpoint, status=status, body=body
        )
    raise ServerError(
        'Tradovate server error', endpoint=endpoint, status=status, body=body
    )
