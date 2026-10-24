"""The transport layer: nulls dropped, query strings, retries."""

from tradovate_client._http import Transport, _drop_none


def test_none_values_are_dropped_recursively():
    assert _drop_none(
        {'a': 1, 'b': None, 'c': {'d': None, 'e': 2}, 'f': [{'g': None, 'h': 3}]}
    ) == {'a': 1, 'c': {'e': 2}, 'f': [{'h': 3}]}


def test_false_and_zero_survive():
    assert _drop_none({'isAutomated': False, 'price': 0.0, 'admin': False}) == {
        'isAutomated': False,
        'price': 0.0,
        'admin': False,
    }


def test_base_url_trailing_slash_is_normalised():
    assert (
        Transport('https://demo.tradovateapi.com/v1/').base_url
        == 'https://demo.tradovateapi.com/v1'
    )
