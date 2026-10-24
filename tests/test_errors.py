"""Status codes map onto exceptions, and failures always raise."""

import pytest

from tradovate_client.errors import (AuthError, NotFoundError, RequestError,
                                     ServerError, ThrottledError,
                                     raise_for_status)


@pytest.mark.parametrize('status', [200, 201, 204])
def test_success_does_not_raise(status):
    assert raise_for_status(status, '/x', None) is None


@pytest.mark.parametrize('status,exc', [
    (401, AuthError), (403, AuthError), (404, NotFoundError),
    (400, RequestError), (422, RequestError),
    (500, ServerError), (503, ServerError)])
def test_status_maps_to_exception(status, exc):
    with pytest.raises(exc):
        raise_for_status(status, '/order/placeorder', {'errorText': 'nope'})


def test_throttle_keeps_the_servers_advice():
    with pytest.raises(ThrottledError) as caught:
        raise_for_status(429, '/auth/accesstokenrequest', None,
                         {'Retry-After': '30'})
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
