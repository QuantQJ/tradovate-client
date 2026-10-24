"""Tradovate's authentication time penalty.

It arrives as **HTTP 200** with no access token and these fields instead:

    p-ticket   send this back with the retry
    p-time     wait this many seconds first
    p-captcha  a human has to intervene

Reference: ``tradovate/example-api-js``,
``tutorial/Access/EX-3-Time-Penalty/src/connect.js``. Retrying through a
penalty extends it, which is how an unattended client gets locked out, so the
behaviour here is: wait as instructed, send the ticket back, and never retry
past a captcha.
"""

from __future__ import annotations

import pytest

from tradovate_client import (
    AuthError,
    CaptchaRequired,
    Credentials,
    Env,
    TimePenalty,
    TradovateClient,
)
from tradovate_client.auth import MAX_PENALTY_RETRIES, MAX_PENALTY_WAIT_SECONDS
from tradovate_client.errors import (
    PENALTY_CAPTCHA,
    PENALTY_TICKET,
    PENALTY_TIME,
    penalty_of,
)

CREDS = Credentials(
    name='u', password='p', appId='a', appVersion='1', deviceId='d', cid=1, sec='s'
)
GOOD = {'accessToken': 'tok', 'expirationTime': '2030-01-01T00:00:00Z'}


class Replies:
    """Answers a queue of auth replies, recording every body it was sent."""

    def __init__(self, queue):
        self.queue = list(queue)
        self.bodies = []
        self.paths = []

    def request(self, method, path, *, token=None, body=None, params=None):
        self.paths.append(path)
        if path == '/auth/accesstokenrequest':
            self.bodies.append(dict(body or {}))
            return self.queue.pop(0) if self.queue else dict(GOOD)
        if path == '/auth/renewaccesstoken':
            return dict(GOOD)
        return None


def client(queue, tmp_path):
    slept = []
    c = TradovateClient(
        Env.DEMO, credentials=CREDS, token_dir=tmp_path, sleep=slept.append
    )
    c._transport = Replies(queue)
    c.slept = slept
    return c


def penalty(ticket='T1', seconds=15, captcha=None):
    reply = {PENALTY_TICKET: ticket, PENALTY_TIME: seconds}
    if captcha is not None:
        reply[PENALTY_CAPTCHA] = captcha
    return reply


# ── the parser: a 200 with a ticket is a throttle, not a success ────────────


def test_a_successful_reply_is_not_a_penalty():
    assert penalty_of(GOOD) is None


def test_a_ticket_makes_it_a_penalty_even_though_the_status_was_200():
    assert penalty_of(penalty()) == ('T1', 15.0, None)


def test_the_captcha_flag_is_carried_through():
    assert penalty_of(penalty(captcha=True)) == ('T1', 15.0, True)


def test_a_missing_time_reads_as_zero_rather_than_raising():
    assert penalty_of({PENALTY_TICKET: 'T'}) == ('T', 0.0, None)


def test_a_nonsense_time_reads_as_zero():
    assert penalty_of({PENALTY_TICKET: 'T', PENALTY_TIME: 'soon'}) == ('T', 0.0, None)


def test_a_time_without_a_ticket_is_not_a_penalty():
    """The ticket is what identifies a penalty; p-time alone is not one."""
    assert penalty_of({PENALTY_TIME: 30}) is None


def test_a_non_dict_is_not_a_penalty():
    assert penalty_of(None) is None and penalty_of('nope') is None


# ── the wait is respected, and additive ────────────────────────────────────


def test_one_penalty_is_waited_out_then_the_retry_succeeds(tmp_path):
    c = client([penalty('T1', 15), dict(GOOD)], tmp_path)
    assert c.token() == 'tok'
    assert c.slept == [15.0], "it must wait Tradovate's own p-time"
    assert len(c._transport.bodies) == 2


def test_the_ticket_is_sent_back_with_the_retry(tmp_path):
    c = client([penalty('TICKET-A', 5), dict(GOOD)], tmp_path)
    c.token()
    first, second = c._transport.bodies
    assert PENALTY_TICKET not in first, 'the first attempt carries no ticket'
    assert second[PENALTY_TICKET] == 'TICKET-A'
    assert second['name'] == 'u', 'and the rest of the credentials come too'


def test_successive_penalties_are_waited_additively(tmp_path):
    c = client(
        [penalty('T1', 5), penalty('T2', 10), penalty('T3', 20), dict(GOOD)], tmp_path
    )
    assert c.token() == 'tok'
    assert c.slept == [5.0, 10.0, 20.0]
    assert sum(c.slept) == 35.0


def test_each_retry_carries_the_latest_ticket(tmp_path):
    c = client([penalty('T1', 1), penalty('T2', 1), dict(GOOD)], tmp_path)
    c.token()
    tickets = [b.get(PENALTY_TICKET) for b in c._transport.bodies]
    assert tickets == [None, 'T1', 'T2']


def test_a_single_wait_is_capped(tmp_path):
    """A bad p-time must not hang a process indefinitely."""
    c = client([penalty('T1', 99999), dict(GOOD)], tmp_path)
    c.token()
    assert c.slept == [MAX_PENALTY_WAIT_SECONDS]


def test_a_negative_time_does_not_sleep_backwards(tmp_path):
    c = client([penalty('T1', -5), dict(GOOD)], tmp_path)
    c.token()
    assert c.slept == [0.0]


# ── a captcha is terminal, never retried ───────────────────────────────────


def test_a_captcha_stops_immediately(tmp_path):
    c = client([penalty('T1', 60, captcha=True)], tmp_path)
    with pytest.raises(CaptchaRequired) as caught:
        c.token()
    assert 'captcha' in str(caught.value).lower()
    assert 'Do not retry' in str(caught.value)
    assert c.slept == [], 'it must not wait'
    assert len(c._transport.bodies) == 1, 'and must not try again'


def test_a_captcha_after_a_plain_penalty_still_stops(tmp_path):
    c = client([penalty('T1', 5), penalty('T2', 60, captcha=True)], tmp_path)
    with pytest.raises(CaptchaRequired):
        c.token()
    assert c.slept == [5.0], 'the first wait happened, the second did not'
    assert len(c._transport.bodies) == 2


def test_a_captcha_is_not_an_auth_error(tmp_path):
    """It needs a different response from the caller, so it is a different type."""
    c = client([penalty('T1', 1, captcha='abc')], tmp_path)
    with pytest.raises(CaptchaRequired) as caught:
        c.token()
    assert not isinstance(caught.value, AuthError)


# ── the retry budget ───────────────────────────────────────────────────────


def test_endless_penalties_eventually_raise_rather_than_loop(tmp_path):
    c = client([penalty('T%d' % i, 2) for i in range(20)], tmp_path)
    with pytest.raises(TimePenalty) as caught:
        c.token()
    assert caught.value.attempts == MAX_PENALTY_RETRIES
    assert caught.value.ticket is not None
    assert caught.value.seconds == 2.0
    assert len(c.slept) == MAX_PENALTY_RETRIES


def test_the_exhaustion_message_says_to_wait_not_to_retry(tmp_path):
    c = client([penalty('T', 3) for _ in range(20)], tmp_path)
    with pytest.raises(TimePenalty) as caught:
        c.token()
    text = str(caught.value)
    assert 'Wait rather than retrying' in text
    assert 'extends the penalty' in text


def test_a_penalty_is_not_an_auth_failure(tmp_path):
    """Being throttled is not the same as being refused."""
    c = client([penalty('T', 1) for _ in range(20)], tmp_path)
    with pytest.raises(TimePenalty) as caught:
        c.token()
    assert not isinstance(caught.value, AuthError)


# ── the other documented failure fields ────────────────────────────────────


def test_an_error_text_is_surfaced(tmp_path):
    c = client([{'errorText': 'Invalid credentials'}], tmp_path)
    with pytest.raises(AuthError, match='Invalid credentials'):
        c.token()


def test_a_locked_login_says_retrying_will_not_help(tmp_path):
    c = client([{'accessToken': 'x', 'userStatus': 'TemporaryLocked'}], tmp_path)
    with pytest.raises(AuthError, match='TemporaryLocked'):
        c.token()


def test_a_closed_login_is_refused(tmp_path):
    c = client([{'accessToken': 'x', 'userStatus': 'Closed'}], tmp_path)
    with pytest.raises(AuthError, match='Closed'):
        c.token()


def test_an_active_login_is_fine(tmp_path):
    c = client([dict(GOOD, userStatus='Active')], tmp_path)
    assert c.token() == 'tok'


def test_a_reply_with_no_token_and_no_reason_still_raises(tmp_path):
    c = client([{}], tmp_path)
    with pytest.raises(AuthError, match='no token'):
        c.token()


# ── and none of this fires when it should not ──────────────────────────────


def test_a_clean_first_attempt_neither_waits_nor_retries(tmp_path):
    c = client([dict(GOOD)], tmp_path)
    assert c.token() == 'tok'
    assert c.slept == []
    assert len(c._transport.bodies) == 1


def test_a_cached_token_does_not_authenticate_at_all(tmp_path):
    c = client([dict(GOOD)], tmp_path)
    c._tokens.write(GOOD)
    assert c.token() == 'tok'
    assert c._transport.bodies == [], 'no auth request, so no penalty risk'
