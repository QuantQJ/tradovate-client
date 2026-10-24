"""Status codes map onto exceptions, and failures always raise."""

import pytest

from tradovate_client.errors import (
    AuthError,
    NotFoundError,
    RequestError,
    ServerError,
    ThrottledError,
    raise_for_status,
)


@pytest.mark.parametrize('status', [200, 201, 204])
def test_success_does_not_raise(status):
    assert raise_for_status(status, '/x', None) is None


@pytest.mark.parametrize(
    'status,exc',
    [
        (401, AuthError),
        (403, AuthError),
        (404, NotFoundError),
        (400, RequestError),
        (422, RequestError),
        (500, ServerError),
        (503, ServerError),
    ],
)
def test_status_maps_to_exception(status, exc):
    with pytest.raises(exc):
        raise_for_status(status, '/order/placeorder', {'errorText': 'nope'})


def test_throttle_keeps_the_servers_advice():
    with pytest.raises(ThrottledError) as caught:
        raise_for_status(429, '/auth/accesstokenrequest', None, {'Retry-After': '30'})
    assert caught.value.retry_after == 30.0


def test_throttle_without_advice():
    with pytest.raises(ThrottledError) as caught:
        raise_for_status(429, '/auth/accesstokenrequest', None, {})
    assert caught.value.retry_after is None


def test_error_message_carries_the_endpoint_and_body():
    with pytest.raises(RequestError) as caught:
        raise_for_status(400, '/order/placeorder', {'errorText': 'bad symbol'})
    text = str(caught.value)
    assert '/order/placeorder' in text and 'bad symbol' in text


# ── no exception may carry a token ──────────────────────────────────────────


def test_an_auth_reply_is_redacted_before_it_reaches_the_exception():
    from tradovate_client.errors import AuthError

    exc = AuthError('refused', body={'accessToken': 'SECRET', 'userStatus': 'Closed'})
    assert exc.body['accessToken'] == '[redacted]'
    assert exc.body['userStatus'] == 'Closed', 'the useful part survives'
    assert 'SECRET' not in str(exc)


def test_a_penalty_ticket_is_redacted_in_the_body_but_kept_as_an_attribute():
    from tradovate_client.errors import TimePenalty

    exc = TimePenalty(
        'throttled',
        seconds=15,
        ticket='TICKET-X',
        body={'p-ticket': 'TICKET-X', 'p-time': 15},
    )
    assert 'TICKET-X' not in str(exc)
    assert exc.body['p-ticket'] == '[redacted]'
    assert exc.ticket == 'TICKET-X', 'the caller still needs it'


def test_redaction_reaches_nested_values():
    from tradovate_client.errors import RequestError

    exc = RequestError('x', body={'outer': {'password': 'hunter2', 'keep': 1}})
    assert exc.body['outer']['password'] == '[redacted]'
    assert exc.body['outer']['keep'] == 1


def test_redaction_survives_a_list():
    from tradovate_client.errors import RequestError

    exc = RequestError('x', body=[{'sec': 's'}, {'ok': 2}])
    assert exc.body[0]['sec'] == '[redacted]'
    assert exc.body[1]['ok'] == 2


def test_every_sensitive_key_is_covered():
    from tradovate_client.errors import SENSITIVE_KEYS, RequestError

    body = dict.fromkeys(SENSITIVE_KEYS, 'LEAK')
    exc = RequestError('x', body=body)
    assert 'LEAK' not in str(exc)
    assert set(exc.body.values()) == {'[redacted]'}


def test_a_plain_body_is_untouched():
    from tradovate_client.errors import RequestError

    assert RequestError('x', body={'errorText': 'bad symbol'}).body == {
        'errorText': 'bad symbol'
    }


# ── redaction has to cover raw text and any capitalisation ──────────────────


@pytest.mark.parametrize(
    'key',
    [
        'accessToken',
        'AccessToken',
        'ACCESSTOKEN',
        'Password',
        'P-Ticket',
        'mdAccessToken',
        'MDAccessToken',
        'Sec',
        'API_KEY',
    ],
)
def test_keys_are_matched_whatever_their_case(key):
    """Tradovate's own docs capitalise field names while its JSON does not."""
    from tradovate_client.errors import redact

    assert redact({key: 'LEAK'})[key] == '[redacted]'


@pytest.mark.parametrize(
    'text',
    [
        '{"accessToken":"LEAK","userStatus":"Closed"}',
        "{'p-ticket': 'LEAK', 'p-time': 15}",
        '<html>AccessToken: LEAK</html>',
        'name=u&password=LEAK&cid=1',
        'sec: LEAK',
        'MDAccessToken=LEAK;',
    ],
)
def test_a_raw_text_body_is_redacted_too(text):
    """A response that is not valid JSON is stored as text, and text would
    otherwise pass through untouched."""
    from tradovate_client.errors import redact

    assert 'LEAK' not in redact(text)


def test_a_text_body_keeps_the_part_worth_reading():
    from tradovate_client.errors import redact

    assert redact('errorText: Invalid credentials') == 'errorText: Invalid credentials'


def test_a_non_json_auth_response_cannot_leak_through_the_exception():
    from tradovate_client.errors import AuthError

    exc = AuthError('refused', body='{"accessToken":"LEAK","errorText":"nope"}')
    assert 'LEAK' not in str(exc)
    assert 'nope' in str(exc)
