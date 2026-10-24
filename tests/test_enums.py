"""The enum values are an external contract. If one of these fails, either
Tradovate changed the API or someone guessed at a value.
"""

from tradovate_client import Action, Env, OrderType, TimeInForce


def test_order_types_match_tradovates_published_set():
    assert {t.value for t in OrderType} == {
        'Limit', 'MIT', 'Market', 'QTS', 'Stop', 'StopLimit',
        'TrailingStop', 'TrailingStopLimit'}


def test_time_in_force_matches_published_set():
    assert {t.value for t in TimeInForce} == {'Day', 'FOK', 'GTC', 'GTD', 'IOC'}


def test_actions():
    assert {a.value for a in Action} == {'Buy', 'Sell'}
    assert Action.BUY.opposite is Action.SELL
    assert Action.SELL.opposite is Action.BUY


def test_environments_are_separate_hosts():
    assert Env.DEMO.rest_url == 'https://demo.tradovateapi.com/v1'
    assert Env.LIVE.rest_url == 'https://live.tradovateapi.com/v1'
    assert Env.DEMO.rest_url != Env.LIVE.rest_url
    assert Env.LIVE.is_live and not Env.DEMO.is_live


def test_sockets_are_separate_hosts():
    assert Env.DEMO.socket_url == 'wss://demo.tradovateapi.com/v1/websocket'
    assert Env.LIVE.socket_url == 'wss://live.tradovateapi.com/v1/websocket'
