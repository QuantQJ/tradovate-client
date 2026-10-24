"""Retries, and the rule that an order is never resent blindly.

The HIGH finding these cover: a timeout on an order POST does not mean the
order was rejected, so repeating it can place a second one.
"""

import socket
import urllib.error

import pytest

from tradovate_client._http import Transport, may_resend, never_sent
from tradovate_client.errors import ThrottledError, TransportError


class FakeResponse:
    status = 200
    headers = {}

    def __init__(self, payload=b'{"ok": true}'):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def transport(monkeypatch, failures, *, retries=2):
    """A Transport whose urlopen raises each item in `failures`, then succeeds."""
    queue = list(failures)
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        if queue:
            raise queue.pop(0)
        return FakeResponse()

    monkeypatch.setattr('urllib.request.urlopen', fake_urlopen)
    slept = []
    t = Transport(
        'https://demo.tradovateapi.com/v1', retries=retries, sleep=slept.append
    )
    t.calls = calls
    t.slept = slept
    return t


TIMEOUT = TimeoutError('timed out')
REFUSED = urllib.error.URLError(ConnectionRefusedError(61, 'Connection refused'))
DNS = urllib.error.URLError(socket.gaierror(8, 'nodename nor servname provided'))
RESET = urllib.error.URLError(ConnectionResetError(54, 'Connection reset by peer'))


# ── the rule itself ────────────────────────────────────────────────────────


def test_get_is_always_resendable():
    for exc in (TIMEOUT, REFUSED, DNS, RESET):
        assert may_resend('GET', exc) is True


def test_post_is_resendable_only_when_nothing_was_sent():
    assert may_resend('POST', REFUSED) is True
    assert may_resend('POST', DNS) is True
    assert may_resend('POST', TIMEOUT) is False
    assert may_resend('POST', RESET) is False


def test_never_sent_recognises_the_pre_flight_failures():
    assert never_sent(REFUSED) and never_sent(DNS)
    assert not never_sent(TIMEOUT) and not never_sent(RESET)


# ── what the transport actually does ───────────────────────────────────────


def test_a_timed_out_read_is_retried(monkeypatch):
    t = transport(monkeypatch, [TIMEOUT, TIMEOUT])
    assert t.request('GET', '/account/list') == {'ok': True}
    assert len(t.calls) == 3


def test_a_timed_out_order_is_not_retried(monkeypatch):
    t = transport(monkeypatch, [TIMEOUT])
    with pytest.raises(TransportError) as caught:
        t.request('POST', '/order/placeorder', body={'orderQty': 1})
    assert len(t.calls) == 1, 'the order must not be sent twice'
    assert caught.value.ambiguous is True
    assert 'may or may not' in str(caught.value)
    assert 'Check order and position state' in str(caught.value)


def test_a_reset_order_is_not_retried(monkeypatch):
    t = transport(monkeypatch, [RESET])
    with pytest.raises(TransportError) as caught:
        t.request('POST', '/order/placeorder', body={'orderQty': 1})
    assert len(t.calls) == 1
    assert caught.value.ambiguous is True


@pytest.mark.parametrize('exc', [REFUSED, DNS])
def test_an_order_that_never_left_is_retried(monkeypatch, exc):
    t = transport(monkeypatch, [exc])
    assert t.request('POST', '/order/placeorder', body={'orderQty': 1}) == {'ok': True}
    assert len(t.calls) == 2


def test_an_unsent_order_failure_is_not_ambiguous(monkeypatch):
    t = transport(monkeypatch, [REFUSED, REFUSED, REFUSED], retries=1)
    with pytest.raises(TransportError) as caught:
        t.request('POST', '/order/placeorder', body={'orderQty': 1})
    assert caught.value.ambiguous is False


def test_cancel_and_liquidate_are_held_to_the_same_rule():
    for path_method in ('POST',):
        assert may_resend(path_method, TIMEOUT) is False


# ── throttling is a real answer, so it may be repeated ─────────────────────


def throttled(retry_after='1'):
    return urllib.error.HTTPError(
        'https://demo.tradovateapi.com/v1/auth/accesstokenrequest',
        429,
        'Too Many Requests',
        {'Retry-After': retry_after},
        None,
    )


def test_a_throttled_order_is_retried(monkeypatch):
    t = transport(monkeypatch, [throttled()])
    assert t.request('POST', '/order/placeorder', body={'orderQty': 1}) == {'ok': True}
    assert len(t.calls) == 2
    assert t.slept == [1.0]


def test_throttling_gives_up_after_the_retry_budget(monkeypatch):
    t = transport(monkeypatch, [throttled(), throttled(), throttled()], retries=2)
    with pytest.raises(ThrottledError):
        t.request('GET', '/account/list')
    assert len(t.calls) == 3


def test_retries_can_be_turned_off(monkeypatch):
    t = transport(monkeypatch, [DNS], retries=0)
    with pytest.raises(TransportError):
        t.request('GET', '/account/list')
    assert len(t.calls) == 1
